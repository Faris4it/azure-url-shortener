# Azure URL Shortener

**Serverless URL shortener on Azure — Python Functions, Bicep IaC, and a GitHub Actions pipeline with passwordless OIDC deployment.**

🔗 **Live:** `https://func-azurl-dev-gpj33z72s6dk6.azurewebsites.net`
📁 **Stack:** Python 3.11 · Azure Functions (v2 model) · Table Storage · Bicep · GitHub Actions

---

A production-shaped serverless API that turns long URLs into short codes and
redirects visitors back to the original. The application itself is
deliberately small — the point of the project is everything around it: the
entire Azure footprint is defined as code in a single Bicep template, the
Function App authenticates to Key Vault through a managed identity rather
than a stored credential, GitHub Actions deploys via OIDC federation with no
long-lived secrets in the repo, and unit tests gate every deployment. It was
built to demonstrate that I can take a service from local development
through infrastructure provisioning to a monitored, automatically deployed
cloud environment.

## API

| Method | Route | Auth | Behavior |
|---|---|---|---|
| `POST` | `/api/shorten` | function key | `{"url": "https://..."}` → `201` `{"short_code", "short_url"}` |
| `GET` | `/api/{short_code}` | anonymous | `302` redirect to the original URL, or `404` |

Invalid URLs, malformed JSON, and missing fields return `400` with a JSON error.

Creating links requires a function key; **redirects are public**, so shared
links work for everyone. An open shortener would let anyone mint links on
this domain pointing anywhere — a ready-made phishing vector — so the write
path is gated while the read path stays open.

```bash
curl -X POST "https://func-azurl-dev-gpj33z72s6dk6.azurewebsites.net/api/shorten?code=<FUNCTION_KEY>" \
  -H "Content-Type: application/json" -d '{"url": "https://example.com"}'
```

## Architecture

```
                    ┌─────────────────────────────────────────┐
   git push main    │            GitHub Actions               │
  ────────────────► │  ┌───────────┐      ┌────────────────┐  │
                    │  │  pytest   │─────►│ deploy (OIDC)  │  │
                    │  └───────────┘ gate └────────┬───────┘  │
                    └─────────────────────────────┼───────────┘
                        no stored secrets ────────┤
                        (federated identity)      │ 1. az deployment (Bicep)
                                                  │ 2. publish code package
                                                  ▼
  ┌──────────┐                    ┌───────────────────────────────┐
  │  Client  │  POST /api/shorten │      Azure Function App       │
  │          │ ──────────────────►│   (Linux Consumption, Y1)     │
  │          │ ◄──────────────────│                               │
  │          │   201 short_url    │   shorten()                   │
  │          │                    │   redirect_to_original()      │
  │          │  GET /api/{code}   │                               │
  │          │ ──────────────────►│         │            │        │
  │          │ ◄──────────────────│         │            │        │
  └──────────┘   302 → original   └─────────┼────────────┼────────┘
                                            │            │
                        managed identity    │            │ telemetry
                        (no credentials)    │            │
                              ┌─────────────┘            └──────────┐
                              ▼                                     ▼
                    ┌───────────────────┐              ┌─────────────────────┐
                    │  Table Storage    │              │ Application Insights│
                    │  'shorturls'      │              │   + Log Analytics   │
                    │  PK=url           │              └─────────────────────┘
                    │  RK=short_code    │
                    │  → OriginalUrl    │              ┌─────────────────────┐
                    └───────────────────┘              │      Key Vault      │
                                                       │  (RBAC, Secrets     │
                              managed identity ───────►│   User role)        │
                                                       └─────────────────────┘
```

**Write path:** client `POST`s a URL → validated (`http`/`https`, well-formed)
→ a 7-character code is generated with `secrets.choice` → written to Table
Storage as a new entity, retrying on the rare key collision → short URL
returned, built from the request's own host so it's correct in every
environment.

**Read path:** client `GET`s `/api/{code}` → single point read against Table
Storage by partition + row key → `302` with a `Location` header, or `404`.

## Quick start (local, no Azure account needed)

Runs against [Azurite](https://learn.microsoft.com/azure/storage/common/storage-use-azurite),
the local Storage emulator.

```bash
python -m venv .venv && source .venv/Scripts/activate   # Windows: .venv\Scripts\Activate.ps1
```
```bash
pip install -r requirements-dev.txt
```
```bash
cp local.settings.json.example local.settings.json
```

Start the emulator in one terminal:
```bash
npm install -g azurite && azurite-table --silent --location ~/azurite-data
```

Start the app in another:
```bash
func start
```

Serves on `http://localhost:7071`. Run the test suite any time with:
```bash
pytest -q
```

**16 unit tests** cover URL validation, code generation, both endpoints'
success and error paths, and the collision-retry logic. They mock the Table
Storage client, so they need no emulator and finish in under a second.

## Deployment

Deployment is automatic: **push to `main`** runs the tests, and only if they
pass does it deploy infrastructure and code. To deploy manually instead:

```bash
az login && az group create --name rg-azurl-dev --location uaenorth
```
```bash
az deployment group create --resource-group rg-azurl-dev \
  --template-file infra/main.bicep --parameters infra/main.parameters.json
```
```bash
func azure functionapp publish <functionAppName-from-output>
```

<details>
<summary><b>Setting up OIDC federated deployment (no stored secrets)</b></summary>

Rather than storing a service principal password in GitHub, Entra ID is
configured to trust GitHub's token issuer for one specific repo and branch.
Each run mints a short-lived token proving its identity; nothing durable is
stored, so there is no credential to leak or rotate.

```bash
APP_ID=$(az ad app create --display-name "azure-url-shortener-github-actions" --query appId -o tsv)
APP_OBJECT_ID=$(az ad app show --id "$APP_ID" --query id -o tsv)
az ad sp create --id "$APP_ID"
```

```bash
az ad app federated-credential create --id "$APP_OBJECT_ID" --parameters \
  '{"name":"github-actions-main-branch","issuer":"https://token.actions.githubusercontent.com","subject":"repo:<OWNER>/<REPO>:ref:refs/heads/main","audiences":["api://AzureADTokenExchange"]}'
```

Grant the identity `Contributor` **and** `Role Based Access Control
Administrator`, both scoped to the resource group only — the latter is
required because Contributor deliberately excludes
`Microsoft.Authorization/roleAssignments/write`, and the template creates a
role assignment.

Then add three repository secrets: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`,
`AZURE_SUBSCRIPTION_ID`.

</details>

## Azure services, and why

| Service | Why this one |
|---|---|
| **Functions (Linux Consumption, Y1)** | Scales to zero — you pay only for execution time, ideal for spiky, low-volume traffic. Accepted tradeoff: cold-start latency on the first request after idle. |
| **Table Storage** | The data is a pure key→value lookup with no relations or queries. A NoSQL table costs a fraction of a managed SQL database and needs no schema management. |
| **Bicep** | The whole environment is reproducible from one file and safe to re-run — ARM reconciles declared state instead of re-creating resources. Far more readable than raw ARM JSON. |
| **Application Insights + Log Analytics** | Request traces, failures, and latency without writing logging infrastructure. Uses the current workspace-based model so telemetry lives with other Azure monitoring data. |
| **Key Vault (RBAC)** | Central, access-controlled secret storage. Accessed through the Function App's managed identity with the least-privilege *Key Vault Secrets User* role — read secret values, nothing else. |
| **Managed identity** | Removes the bootstrapping problem of needing a secret in order to fetch secrets. Azure issues and rotates the credential itself. |
| **GitHub Actions + OIDC** | Tests gate deploys, and federated identity means no long-lived cloud credential ever lives in the repository. |

<details>
<summary><b>Design note: why two settings bypass Key Vault</b></summary>

`AzureWebJobsStorage` and `WEBSITE_CONTENTAZUREFILECONNECTIONSTRING` are read
by the Functions host at startup, before Key Vault reference resolution is
available on Consumption plans — Azure does not support Key Vault references
for these two settings. They are therefore set directly, while the vault
holds a copy for governance and stands ready for any secret added later that
isn't part of that bootstrap path.

</details>

## What I'd improve with more time

- **Rate limiting.** The function key gates *who* can create links, but not *how many*. Azure API Management, or a per-IP counter in Table Storage, would cap abuse and runaway cost.
- **Destination allowlist.** Key holders can still point links anywhere. For a multi-tenant service, an allowlist plus a Safe Browsing check on submitted URLs would be the next layer.
- **Link expiry.** Add a TTL column and a timer-triggered function to sweep expired entries, keeping the table small and giving users temporary links.
- **Custom domain + TLS.** A short domain behind Azure Front Door, since `func-azurl-dev-….azurewebsites.net` rather defeats the purpose of a *short* URL.
- **Shorter, collision-free codes.** Swap random generation for a counter encoded in base62, removing the retry loop and producing shorter links.
- **Analytics.** Click counts, referrers, and geography per link — a natural fit for the Application Insights data already being collected.
- **Staging environment.** The template is already parameterized by environment name; adding a `staging` slot with smoke tests before production would make deployments genuinely safe.
- **Partition strategy.** All rows currently share one partition key, which is fine at demo scale but caps write throughput. Sharding on a code prefix would scale horizontally.

## Engineering notes

Getting this deployed surfaced six distinct real-world failures worth
recording — the debugging is arguably more representative of the work than
the application code:

1. **`AADSTS700213`** — GitHub asserted the OIDC subject as `repo:owner@<id>/repo@<id>:ref:...` with numeric IDs appended rather than the documented plain form; Entra ID matches that string exactly, so the credential had to match precisely.
2. **`roleAssignments/write` denied** — Contributor excludes this action by design, so a Contributor cannot escalate itself to Owner. Resolved with *Role Based Access Control Administrator* scoped to the resource group.
3. **`RequestDisallowedByAzure`** — the subscription's region policy allowed only five regions; deployment moved to the nearest permitted one.
4. **`MissingSubscriptionRegistration`** — resource providers must be explicitly registered on a new subscription before use.
5. **`enablePurgeProtection: false`** — Key Vault rejects an explicit `false`, since enabling purge protection is irreversible; the property must be omitted.
6. **A green pipeline deploying a dead app** — the sharpest lesson. On Linux Consumption with RBAC auth, the deploy action uses `WEBSITE_RUN_FROM_PACKAGE`, which runs the uploaded zip as-is with no build step. Dependencies were never installed, so the worker couldn't import `azure.functions` and silently registered zero functions — while CI reported success. Fixed by vendoring dependencies into the package. **A green build is not proof of a working deployment; verify against the live endpoint.**

## Repository layout

```
function_app.py              Both HTTP functions (Python v2 decorator model)
tests/test_function_app.py   16 unit tests, Table Storage mocked
infra/main.bicep             Entire Azure environment as code
infra/main.parameters.json   Environment name + region
.github/workflows/deploy.yml Test → provision → deploy pipeline
host.json                    Functions host configuration
local.settings.json.example  Local config template (real file is gitignored)
```

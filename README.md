# Azure URL Shortener

A serverless URL shortener built to demonstrate core Azure cloud fundamentals:
an Azure Functions API backed by Table Storage, deployed via Infrastructure
as Code (Bicep), with monitoring and secrets management wired in.

- `POST /api/shorten` — creates a short code for a URL
- `GET /api/{short_code}` — redirects to the original URL

See [function_app.py](function_app.py) for the application code and
[tests/test_function_app.py](tests/test_function_app.py) for its unit tests.

## Infrastructure

The [infra/main.bicep](infra/main.bicep) template provisions everything this
app needs to run in Azure. Here's what each resource is for, in plain
language — useful if you're explaining this project in an interview.

**Storage Account**
Does double duty. Every Azure Function App needs a storage account for its
own internal bookkeeping (tracking trigger state, coordinating scale-out,
holding the deployed code package). This same account also hosts the
`shorturls` Table Storage table where the app's actual data lives —
short code → original URL. Using one account for both keeps the demo
simple; a larger production system might split these for isolation.

**Table Storage table (`shorturls`)**
A NoSQL key-value table, declared directly in Bicep rather than left for
the app to create at runtime. Storage is cheap and schema-less — a good
fit for a simple mapping like this, versus paying for a relational database
you don't need.

**App Service Plan (Consumption / Y1 tier)**
Defines *how* the Function App is billed and hosted. The Consumption plan
only charges for actual execution time and scales down to zero instances
(and zero cost) when nothing is happening — the cheapest way to run this
kind of intermittent workload. The tradeoff is "cold starts": the first
request after idle time is slower while an instance spins up.

**Function App**
The compute resource that actually runs the Python code. It's configured
for the Linux Python 3.11 runtime and given a **system-assigned managed
identity** — an identity Azure manages for it automatically, with no
password or secret to store or rotate. That identity is what's granted
access to Key Vault below.

**Log Analytics Workspace + Application Insights**
Application Insights is the monitoring/telemetry service — it captures
every request, exception, and log line the Function App emits, so you can
see latency, error rates, and traces without adding custom logging
infrastructure. It's backed by a Log Analytics workspace, which is the
current recommended pattern ("workspace-based" Application Insights)
instead of the older standalone model — it lets Application Insights data
live alongside other Azure monitoring data in one place.

**Key Vault**
Centralized, access-controlled storage for secrets, so they never end up
in plain text in app settings or source control. This template stores a
copy of the storage connection string in it as a working example; it's
ready to hold real secrets later (a third-party API key, for instance)
without any infrastructure changes. Access uses **RBAC** (role-based
access control) rather than the older Key Vault access-policy model — the
same permissions system used everywhere else in Azure.

**Role Assignment (managed identity → Key Vault)**
Grants the Function App's managed identity the **Key Vault Secrets User**
role, scoped only to this Key Vault. That's least-privilege access: it can
read secret values, and nothing else — it can't list, create, or delete
secrets, and has no access to keys or certificates. This is the
"passwordless" pattern Azure recommends: instead of putting a Key Vault
access key in the Function App's settings (a secret needed to access other
secrets), the Function App proves who it is via its own Azure identity.

> **Why the storage connection string isn't itself pulled from Key Vault
> into the Function App's settings:** `AzureWebJobsStorage` and
> `WEBSITE_CONTENTAZUREFILECONNECTIONSTRING` are read by the Functions
> host at startup, before Key Vault reference resolution is available on
> Consumption/Premium plans — Azure explicitly doesn't support Key Vault
> references for these two settings. So they're set directly from the
> storage account's key, while the Key Vault still holds a copy for
> governance and as a pattern for any secret added later that *isn't*
> part of that bootstrap path.

## Deploying the infrastructure

Requires the [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli)
and an Azure subscription.

```bash
az login
```

```bash
az group create --name rg-azurl-dev --location uaenorth
```

> **Region note:** some subscriptions (Azure for Students, for example)
> carry an "Allowed resource deployment regions" policy that restricts
> where resources can be created. If a deployment fails with
> `RequestDisallowedByAzure`, check which regions yours permits and set
> `location` in `infra/main.parameters.json` accordingly:
>
> ```bash
> az policy assignment show --scope "/subscriptions/$(az account show --query id -o tsv)" --name "sys.regionrestriction" --query "parameters.listOfAllowedLocations.value"
> ```

```bash
az deployment group create --resource-group rg-azurl-dev --template-file infra/main.bicep --parameters infra/main.parameters.json
```

Then deploy the function code itself with the Azure Functions Core Tools
(from the project root, with the venv activated):

```bash
func azure functionapp publish <functionAppName-from-deployment-output>
```

## Local development

See the setup at the top of this repo for running everything locally
against [Azurite](https://learn.microsoft.com/azure/storage/common/storage-use-azurite)
(the Storage emulator) instead of real Azure resources — no Azure account
needed to develop and test.

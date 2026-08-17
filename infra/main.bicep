// =============================================================================
// main.bicep
//
// Provisions the Azure infrastructure for the azure-url-shortener project:
// a Python Function App on a Linux Consumption plan, the Storage Account it
// depends on (Table Storage data + the Functions runtime's own storage),
// Application Insights for monitoring, and a Key Vault for secrets — wired
// together with the Function App's system-assigned managed identity instead
// of any stored credential.
//
// See README.md's "Infrastructure" section for a plain-language walkthrough
// of what each resource below is doing and why.
// =============================================================================

@description('A short name for this environment, e.g. dev, test, prod. Used to build resource names and as a tag.')
@minLength(1)
@maxLength(10)
param environmentName string

@description('Azure region to deploy all resources into.')
param location string = resourceGroup().location

@description('Short project name used as a prefix for resource names. Keep it lowercase/alphanumeric — storage accounts and Key Vault have strict naming rules.')
@minLength(3)
@maxLength(11)
param projectName string = 'azurl'

// A short, deterministic-but-unique suffix so globally-unique resources
// (storage account, key vault, function app hostname) don't collide with
// someone else's deployment of this same template.
var resourceToken = toLower(uniqueString(subscription().id, resourceGroup().id, environmentName))

var tags = {
  project: projectName
  environment: environmentName
  'managed-by': 'bicep'
}

var storageAccountName = take('st${projectName}${resourceToken}', 24)
var keyVaultName = take('kv-${projectName}-${resourceToken}', 24)
var appServicePlanName = 'plan-${projectName}-${environmentName}'
var functionAppName = toLower('func-${projectName}-${environmentName}-${resourceToken}')
var logAnalyticsName = 'log-${projectName}-${environmentName}'
var appInsightsName = 'appi-${projectName}-${environmentName}'
var tableName = 'shorturls'

// -----------------------------------------------------------------------
// Storage Account
// Serves two roles: (1) the storage every Function App requires for its
// own bookkeeping (deployment content, triggers, locks), and (2) the
// Table Storage table this app uses to persist short-code -> URL mappings.
// -----------------------------------------------------------------------
resource storageAccount 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: storageAccountName
  location: location
  tags: tags
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    supportsHttpsTrafficOnly: true
  }
}

resource tableService 'Microsoft.Storage/storageAccounts/tableServices@2023-01-01' = {
  parent: storageAccount
  name: 'default'
}

resource shortUrlsTable 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-01-01' = {
  parent: tableService
  name: tableName
}

// Required for the Function App's own settings below. Consumption-plan
// Function Apps read this at startup, before Key Vault reference resolution
// is available — so, unlike most other settings, it cannot be a Key Vault
// reference (see README.md).
var storageConnectionString = 'DefaultEndpointsProtocol=https;AccountName=${storageAccount.name};AccountKey=${storageAccount.listKeys().keys[0].value};EndpointSuffix=${environment().suffixes.storage}'

// -----------------------------------------------------------------------
// Log Analytics + Application Insights
// Application Insights collects logs, request traces, and failures from
// the Function App. It's backed by a Log Analytics workspace (the modern
// "workspace-based" Application Insights model) rather than its own
// standalone data store.
// -----------------------------------------------------------------------
resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: logAnalyticsName
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
    IngestionMode: 'LogAnalytics'
  }
}

// -----------------------------------------------------------------------
// Key Vault
// Holds secrets for the app so they don't sit in plain text in source
// control. RBAC authorization is used instead of legacy access policies.
// A copy of the storage connection string is stored here as a starting
// example; it's ready to hold real secrets later (e.g. a third-party API
// key) without any infrastructure changes.
// -----------------------------------------------------------------------
resource keyVault 'Microsoft.KeyVault/vaults@2022-07-01' = {
  name: keyVaultName
  location: location
  tags: tags
  properties: {
    sku: {
      family: 'A'
      name: 'standard'
    }
    tenantId: subscription().tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    // enablePurgeProtection is deliberately omitted: Azure only accepts
    // `true` here, since turning purge protection on is irreversible.
    // Leaving it unset keeps the vault fully deletable, which is what we
    // want for a demo environment that gets torn down.
  }
}

resource storageConnectionStringSecret 'Microsoft.KeyVault/vaults/secrets@2022-07-01' = {
  parent: keyVault
  name: 'storage-connection-string'
  properties: {
    value: storageConnectionString
  }
}

// -----------------------------------------------------------------------
// App Service Plan (Consumption)
// Y1/Dynamic on Linux is the "pay only when your code runs, scales to
// zero when idle" tier — the cheapest way to host a Function App.
// -----------------------------------------------------------------------
resource appServicePlan 'Microsoft.Web/serverfarms@2022-09-01' = {
  name: appServicePlanName
  location: location
  tags: tags
  kind: 'functionapp'
  sku: {
    name: 'Y1'
    tier: 'Dynamic'
  }
  properties: {
    reserved: true // required for Linux plans
  }
}

// -----------------------------------------------------------------------
// Function App
// The compute that runs shorten() / redirect_to_original(). Its
// system-assigned managed identity is how it will authenticate to Key
// Vault (and any other Azure resource) without a stored credential.
// -----------------------------------------------------------------------
resource functionApp 'Microsoft.Web/sites@2022-09-01' = {
  name: functionAppName
  location: location
  tags: tags
  kind: 'functionapp,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: appServicePlan.id
    httpsOnly: true
    siteConfig: {
      linuxFxVersion: 'Python|3.11'
      appSettings: [
        {
          name: 'AzureWebJobsStorage'
          value: storageConnectionString
        }
        {
          name: 'WEBSITE_CONTENTAZUREFILECONNECTIONSTRING'
          value: storageConnectionString
        }
        {
          name: 'WEBSITE_CONTENTSHARE'
          value: functionAppName
        }
        {
          name: 'FUNCTIONS_EXTENSION_VERSION'
          value: '~4'
        }
        {
          name: 'FUNCTIONS_WORKER_RUNTIME'
          value: 'python'
        }
        {
          name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
          value: appInsights.properties.ConnectionString
        }
      ]
    }
  }
}

// -----------------------------------------------------------------------
// Role Assignment
// Grants the Function App's managed identity the "Key Vault Secrets User"
// role, scoped to just this Key Vault — least-privilege read access to
// secret values, nothing else (can't list/create/delete secrets, can't
// touch keys or certificates).
// -----------------------------------------------------------------------
var keyVaultSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'

resource functionAppKeyVaultAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, functionApp.id, keyVaultSecretsUserRoleId)
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleId)
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// -----------------------------------------------------------------------
// Outputs
// -----------------------------------------------------------------------
output functionAppName string = functionApp.name
output functionAppHostName string = functionApp.properties.defaultHostName
output storageAccountName string = storageAccount.name
output keyVaultName string = keyVault.name
output appInsightsName string = appInsights.name

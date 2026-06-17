// =============================================================================
// main.bicep  (targetScope = subscription)
// Phase 5 deployment for swing-screener: creates the resource group and wires
// every module in dependency order. ONE UAMI is the sole auth principal across
// ACR pull, Blob, Key Vault and SQL.
//
// Globally-unique names are derived from
//   resourceToken = uniqueString(subscription().id, rgName)
// respecting service name limits (ACR 5-50 alnum, storage 3-24 lowercase alnum).
//
// VALIDATION: this file compiles all modules. Use `az deployment sub what-if`
// at deploy time for the live pre-flight check (no subscription here).
//
// apiVersion verified: Microsoft.Resources/resourceGroups -> 2024-03-01 (stable).
// =============================================================================

targetScope = 'subscription'

@description('Azure region for all resources.')
param location string

@description('Short env/name prefix (lowercase, e.g. "swing", "swingprod"). Used to build resource names.')
@minLength(2)
@maxLength(12)
param namePrefix string = 'swing'

@description('Resource group name. Defaults to rg-<prefix>.')
param resourceGroupName string = 'rg-${namePrefix}'

@description('Image tag to deploy (e.g. "bootstrap").')
param imageTag string = 'bootstrap'

@description('AAD admin display name / login for the SQL server.')
param sqlAadAdminLogin string

@description('AAD admin object ID (user or group) for the SQL server.')
param sqlAadAdminObjectId string

@description('Principal type of the SQL AAD admin.')
@allowed([
  'User'
  'Group'
  'Application'
])
param sqlAadAdminPrincipalType string = 'Group'

@description('Allowed client IPs for the SQL firewall (CA env outbound IP(s) + home IP). Fill in post-deploy once env outbound IPs are known.')
param sqlAllowedIps array = []

@description('Opt-in: GP_S_Gen5 serverless DB instead of Basic DTU.')
param useServerless bool = false

@description('Daily Log Analytics ingestion cap in GB (integer).')
param logDailyQuotaGb int = 1

@description('Log Analytics retention (days).')
param logRetentionInDays int = 30

// --- Secret values: supplied at deploy time, never committed ---
@secure()
@description('Anthropic API key.')
param anthropicApiKey string

@secure()
@description('Digest recipient list.')
param digestTo string

@description('Seed Key Vault secret VALUES from the params above. Set false on re-deploys so an existing/rotated secret is never overwritten.')
param seedSecrets bool = true

// Deterministic, globally-unique-ish suffix bound to this subscription + RG.
var resourceToken = uniqueString(subscription().id, resourceGroupName)

// Name derivations (respecting per-service limits).
//   ACR: alphanumeric only, 5-50 -> strip non-alnum, lowercase.
//   Storage: lowercase alphanumeric, 3-24.
//   KV: 3-24.
var acrName = toLower(take('${namePrefix}acr${resourceToken}', 50))
var storageName = toLower(take('${namePrefix}st${resourceToken}', 24))
var keyVaultName = toLower(take('${namePrefix}kv${resourceToken}', 24))
var sqlServerName = toLower('${namePrefix}-sql-${resourceToken}')
var uamiName = '${namePrefix}-uami-${resourceToken}'
var workspaceName = '${namePrefix}-law-${resourceToken}'
var environmentName = '${namePrefix}-env-${resourceToken}'

var commonTags = {
  app: 'swing-screener'
  phase: 'phase5'
  managedBy: 'bicep'
  env: namePrefix
}

// SWING_DB_URL: mssql via ODBC Driver 18, Entra managed-identity auth. NO
// password -- the UAMI authenticates as a contained DB user (created post-deploy
// via T-SQL). User Id pins the specific UAMI client id.
var swingDbUrl = 'mssql+pyodbc://@${sqlServerName}.database.windows.net:1433/swing?driver=ODBC+Driver+18+for+SQL+Server&Authentication=ActiveDirectoryMSI&User+Id=${identity.outputs.clientId}'

// --- Resource group ---
resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: resourceGroupName
  location: location
  tags: commonTags
}

// --- Identity (sole auth principal) ---
module identity 'modules/identity.bicep' = {
  name: 'identity'
  scope: rg
  params: {
    location: location
    name: uamiName
    tags: commonTags
  }
}

// --- ACR (UAMI gets AcrPull) ---
module acr 'modules/acr.bicep' = {
  name: 'acr'
  scope: rg
  params: {
    location: location
    name: acrName
    uamiPrincipalId: identity.outputs.principalId
    tags: commonTags
  }
}

// --- Log Analytics + Container Apps environment ---
module env 'modules/env.bicep' = {
  name: 'env'
  scope: rg
  params: {
    location: location
    workspaceName: workspaceName
    environmentName: environmentName
    dailyQuotaGb: logDailyQuotaGb
    retentionInDays: logRetentionInDays
    tags: commonTags
  }
}

// --- Storage (UAMI gets Blob Data Contributor) ---
module storage 'modules/storage.bicep' = {
  name: 'storage'
  scope: rg
  params: {
    location: location
    name: storageName
    uamiPrincipalId: identity.outputs.principalId
    tags: commonTags
  }
}

// --- Key Vault (UAMI gets Secrets User) ---
module keyvault 'modules/keyvault.bicep' = {
  name: 'keyvault'
  scope: rg
  params: {
    location: location
    name: keyVaultName
    uamiPrincipalId: identity.outputs.principalId
    anthropicApiKey: anthropicApiKey
    digestTo: digestTo
    seedSecrets: seedSecrets
    tags: commonTags
  }
}

// --- Azure Communication Services Email (managed-identity send; NO secret) ---
module acs 'modules/acs.bicep' = {
  name: 'acs'
  scope: rg
  params: {
    namePrefix: namePrefix
    resourceToken: resourceToken
    uamiPrincipalId: identity.outputs.principalId
    tags: commonTags
  }
}

// --- Azure SQL (Entra-only; UAMI DB user created post-deploy) ---
module sql 'modules/sql.bicep' = {
  name: 'sql'
  scope: rg
  params: {
    location: location
    serverName: sqlServerName
    aadAdminLogin: sqlAadAdminLogin
    aadAdminObjectId: sqlAadAdminObjectId
    aadAdminPrincipalType: sqlAadAdminPrincipalType
    allowedIps: sqlAllowedIps
    useServerless: useServerless
    tags: commonTags
  }
}

// --- The scheduled jobs ---
module jobs 'modules/jobs.bicep' = {
  name: 'jobs'
  scope: rg
  params: {
    location: location
    environmentId: env.outputs.environmentId
    uamiId: identity.outputs.id
    uamiClientId: identity.outputs.clientId
    acrLoginServer: acr.outputs.loginServer
    imageTag: imageTag
    swingDbUrl: swingDbUrl
    blobAccountUrl: storage.outputs.blobAccountUrl
    blobContainer: storage.outputs.containerName
    acsEndpoint: acs.outputs.acsEndpoint
    acsSender: acs.outputs.senderAddress
    keyVaultUrl: keyvault.outputs.vaultUri
    secretNames: keyvault.outputs.secretNames
    tags: commonTags
  }
}

// --- Useful outputs ---
output resourceGroupName string = rg.name
output acrLoginServer string = acr.outputs.loginServer
output keyVaultUri string = keyvault.outputs.vaultUri
output storageBlobAccountUrl string = storage.outputs.blobAccountUrl
output managedEnvironmentId string = env.outputs.environmentId
output sqlServerFqdn string = sql.outputs.sqlServerFqdn
output uamiClientId string = identity.outputs.clientId
output uamiPrincipalId string = identity.outputs.principalId
output jobNames array = jobs.outputs.jobNames
output acsEndpoint string = acs.outputs.acsEndpoint
output acsSender string = acs.outputs.senderAddress

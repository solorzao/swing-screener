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

// Default 'latest' -- CD pushes :latest alongside every immutable :<sha> tag, so a
// re-provision omitting imageTag lands on the current code instead of rolling the
// jobs back to the months-old bootstrap image (the deepAnalysisEnabled lesson:
// template defaults must match the intended prod state).
@description('Image tag to deploy (e.g. "latest" or an immutable git sha).')
param imageTag string = 'latest'

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

// Parallels digestTo but is deliberately NOT @secure(): the address is stored
// readable in the Azure Monitor action group (ops routing metadata, not a
// credential), whereas digestTo is seeded into Key Vault.
@description('Email address for ops alerts (failed job / missing evening screen).')
param alertEmail string

@description('''Account equity in dollars for R-based sizing (SWING_ACCOUNT_EQUITY on
every job). Empty = unconfigured: digests render R-multiples, never a guessed dollar.''')
param accountEquity string = ''

@description('Risk per trade as a fraction of equity (SWING_RISK_PCT). Empty = code default 0.01.')
param riskPct string = ''

@description('Execution adapter (SWING_EXECUTION_MODE): off | manual | paper | live. Empty = code default off.')
param executionMode string = ''

// Both empty by default = the intended prod state: no broker, real money disallowed. A
// re-provision can therefore never arm real money as a side effect. Setting them is the
// deliberate arming ceremony (docs/runbooks/arming-alpaca-live.md), not a routine deploy.
@description('Broker adapter for the live path (SWING_BROKER): "alpaca". Empty = no broker -> live falls back to NoOp.')
param broker string = ''

@description('Explicit real-money flag (SWING_BROKER_ALLOW_REAL_MONEY): "yes"/"true"/"1"/"on". Empty = false. One of the three live locks.')
param allowRealMoney string = ''

@description('Hard cap: dollars of recorded order notional per account-day (SWING_MAX_DAILY_NOTIONAL). Empty = unbounded.')
param maxDailyNotional string = ''

@description('Hard cap: daily realized-loss breaker in R (SWING_MAX_DAILY_LOSS). Empty = unbounded.')
param maxDailyLoss string = ''

@description('Hard cap: max concurrent open positions per account (SWING_MAX_CONCURRENT). Empty = unbounded.')
param maxConcurrent string = ''

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
    accountEquity: accountEquity
    riskPct: riskPct
    executionMode: executionMode
    broker: broker
    allowRealMoney: allowRealMoney
    maxDailyNotional: maxDailyNotional
    maxDailyLoss: maxDailyLoss
    maxConcurrent: maxConcurrent
    tags: commonTags
  }
}

// --- Ops alerting: push job failures / a missing evening screen (2026-07-01
// audit: zero alerting -- a dead job was invisible until an email failed to
// arrive). Depends only on the workspace; the jobs it watches are discovered
// at query time, not deploy time.
module alerts 'modules/alerts.bicep' = {
  name: 'alerts'
  scope: rg
  params: {
    location: location
    namePrefix: namePrefix
    resourceToken: resourceToken
    workspaceId: env.outputs.workspaceId
    alertEmail: alertEmail
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

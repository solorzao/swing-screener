// =============================================================================
// sql.bicep
// Azure SQL logical server + one database, Entra-only auth.
//
// AUTH: azureADOnlyAuthentication:true and an AAD admin -- there is NO SQL
// admin login or password anywhere (administratorLogin/Password are omitted).
// The UAMI authenticates as a contained DB user; Bicep CANNOT create that user
// (it is a T-SQL `CREATE USER ... FROM EXTERNAL PROVIDER` + role grant), so it
// is a documented post-deploy step (see infra notes / README).
//
// SKU: defaults to Basic DTU (~$5/mo flat, no cold start). Flip useServerless
// to true to switch to GP_S_Gen5 serverless (auto-pause) -- a one-line opt-in.
//
// FIREWALL: we deliberately do NOT add the broad AllowAllWindowsAzureIps
// (0.0.0.0) rule. Instead we create a rule per allowed IP (the Container Apps
// env outbound IP(s) + the operator's home IP), passed as a param. Private
// endpoint is the stronger Phase-6 option.
//
// apiVersions verified against current Azure docs (2026-06): 2023-08-01 is the
// latest non-preview Microsoft.Sql/servers, servers/databases and
// servers/firewallRules version.
// =============================================================================

@description('Azure region for the SQL server.')
param location string

@description('Globally-unique SQL logical server name (lowercase).')
param serverName string

@description('Database name.')
param databaseName string = 'swing'

@description('AAD admin display name / login (e.g. a group name or UPN).')
param aadAdminLogin string

@description('AAD admin object ID (user or group).')
param aadAdminObjectId string

@description('Principal type of the AAD admin.')
@allowed([
  'User'
  'Group'
  'Application'
])
param aadAdminPrincipalType string = 'Group'

@description('AAD tenant ID.')
param tenantId string = subscription().tenantId

@description('Allowed client IPs for the SQL firewall (CA env outbound IP(s) + home IP). One firewall rule is created per entry. Empty by default -- fill in post-deploy once the env outbound IPs are known.')
param allowedIps array = []

@description('One-line opt-in: set true for GP_S_Gen5 serverless (auto-pause) instead of Basic DTU.')
param useServerless bool = false

@description('Tags applied to the SQL resources.')
param tags object = {}

// Basic DTU (default) vs GP_S_Gen5 serverless (opt-in). Both are managed-cost.
var basicSku = {
  name: 'Basic'
  tier: 'Basic'
}
var serverlessSku = {
  name: 'GP_S_Gen5'
  tier: 'GeneralPurpose'
  family: 'Gen5'
  capacity: 2
}

resource sqlServer 'Microsoft.Sql/servers@2023-08-01' = {
  name: serverName
  location: location
  tags: tags
  properties: {
    // NO administratorLogin / administratorLoginPassword -- Entra-only.
    minimalTlsVersion: '1.2'
    publicNetworkAccess: 'Enabled'
    administrators: {
      administratorType: 'ActiveDirectory'
      azureADOnlyAuthentication: true
      login: aadAdminLogin
      sid: aadAdminObjectId
      tenantId: tenantId
      principalType: aadAdminPrincipalType
    }
  }
}

resource sqlDatabase 'Microsoft.Sql/servers/databases@2023-08-01' = {
  parent: sqlServer
  name: databaseName
  location: location
  tags: tags
  sku: useServerless ? serverlessSku : basicSku
  properties: useServerless
    ? {
        // Serverless: scale floor 0.5 vCore, auto-pause after 60 min idle.
        minCapacity: json('0.5')
        autoPauseDelay: 60
      }
    : {}
}

// One firewall rule per allowed IP. NO 0.0.0.0 "all Azure" rule.
resource fwRules 'Microsoft.Sql/servers/firewallRules@2023-08-01' = [
  for (ip, i) in allowedIps: {
    parent: sqlServer
    name: 'allow-ip-${i}'
    properties: {
      startIpAddress: ip
      endIpAddress: ip
    }
  }
]

@description('SQL server FQDN, e.g. myserver.database.windows.net -- used to build SWING_DB_URL.')
output sqlServerFqdn string = sqlServer.properties.fullyQualifiedDomainName

@description('SQL server name.')
output serverName string = sqlServer.name

@description('Database name.')
output databaseName string = sqlDatabase.name

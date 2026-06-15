// =============================================================================
// env.bicep
// Log Analytics workspace + Container Apps managed environment (Consumption).
//
// BOUNDED COST: the screen logs every failed ticker and the intraday exit job
// runs ~9x/day on weekdays, so an uncapped workspace is NOT "pennies". We cap
// ingestion with workspaceCapping.dailyQuotaGb and shorten retentionInDays.
//
// NOTE on dailyQuotaGb: the ARM schema types this as an INT, so a fractional
// 0.5 GB cap will NOT compile in Bicep. We therefore expose it as an int param
// (default 1 GB -- the smallest integer cap). If a sub-1 GB cap is truly
// required, set it post-deploy via the portal / `az monitor log-analytics
// workspace update --quota 0.5`, which accepts decimals.
//
// apiVersions verified against current Azure docs (2026-06):
//   Microsoft.OperationalInsights/workspaces  -> 2025-07-01 (latest stable)
//   Microsoft.App/managedEnvironments         -> 2026-01-01 (latest stable)
// =============================================================================

@description('Azure region for the workspace and environment.')
param location string

@description('Log Analytics workspace name.')
param workspaceName string

@description('Container Apps managed environment name.')
param environmentName string

@description('Daily ingestion cap in GB (integer; ARM schema does not accept decimals). Default 1.')
@minValue(1)
param dailyQuotaGb int = 1

@description('Log retention in days. Default 30.')
@minValue(30)
@maxValue(730)
param retentionInDays int = 30

@description('Tags applied to the workspace and environment.')
param tags object = {}

resource workspace 'Microsoft.OperationalInsights/workspaces@2025-07-01' = {
  name: workspaceName
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: retentionInDays
    workspaceCapping: {
      dailyQuotaGb: dailyQuotaGb
    }
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

resource managedEnv 'Microsoft.App/managedEnvironments@2026-01-01' = {
  name: environmentName
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: workspace.properties.customerId
        // listKeys() is evaluated at deploy time; the key is never persisted to
        // the template output and is not a stored app credential.
        sharedKey: workspace.listKeys().primarySharedKey
      }
    }
  }
}

@description('Managed environment resource ID -- consumed by every job.')
output environmentId string = managedEnv.id

@description('Managed environment name.')
output environmentName string = managedEnv.name

@description('Log Analytics workspace resource ID.')
output workspaceId string = workspace.id

// JUDGMENT CALL: a Consumption managed environment does NOT expose a stable
// outbound public IP as a deterministic ARM output. The "staticIp" property on
// the environment is the *inbound* ingress IP, not the egress IP the SQL
// firewall needs. The real outbound IP(s) are only discoverable AFTER the
// environment exists, via:
//   az containerapp env show -n <env> -g <rg> \
//     --query properties.staticIp -o tsv          (inbound -- not what SQL wants)
// For egress, the supported approach is to attach a VNet + NAT Gateway and use
// its public IP, OR to read the environment's outbound IPs post-deploy. So the
// SQL firewall (sql.bicep) is fed an allowedIps PARAM that the operator fills in
// as a documented post-deploy step (see infra notes / README). Phase 6 should
// move SQL behind a private endpoint and drop public firewall rules entirely.

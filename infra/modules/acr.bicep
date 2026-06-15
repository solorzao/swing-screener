// =============================================================================
// acr.bicep
// Azure Container Registry (Basic) holding the single swing-screener image.
// adminUserEnabled:false -- NO registry username/password. The Container Apps
// jobs pull with the UAMI (AcrPull role granted below), not stored creds.
//
// apiVersion verified against current Azure docs (2026-06): 2025-11-01 is the
// latest non-preview Microsoft.ContainerRegistry/registries version;
// Microsoft.Authorization/roleAssignments stable is 2022-04-01.
// =============================================================================

@description('Azure region for the registry.')
param location string

@description('Globally-unique ACR name (alphanumeric, 5-50 chars).')
@minLength(5)
@maxLength(50)
param name string

@description('Object (principal) ID of the UAMI that pulls images.')
param uamiPrincipalId string

@description('Tags applied to the registry.')
param tags object = {}

// Built-in role: AcrPull. GUID is stable, but RE-VERIFY before a real deploy:
//   az role definition list --name AcrPull --query "[0].name" -o tsv
var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

resource registry 'Microsoft.ContainerRegistry/registries@2025-11-01' = {
  name: name
  location: location
  tags: tags
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: false
  }
}

// Grant the UAMI AcrPull, scoped to THIS registry only. Deterministic GUID name
// (scope + principal + role) so re-deploys are idempotent.
resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, uamiPrincipalId, acrPullRoleId)
  scope: registry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
    principalId: uamiPrincipalId
    principalType: 'ServicePrincipal'
  }
}

@description('ACR login server, e.g. myacr.azurecr.io -- used to build the image reference and as the jobs registry server.')
output loginServer string = registry.properties.loginServer

@description('ACR resource name.')
output name string = registry.name

@description('ACR resource ID.')
output id string = registry.id

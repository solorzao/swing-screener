// =============================================================================
// identity.bicep
// ONE user-assigned managed identity (UAMI). This is the SOLE auth principal
// for the whole stack: ACR pull, Blob data, Key Vault secrets and Azure SQL all
// authorize THIS identity. There are no stored credentials anywhere -- every
// downstream module takes this UAMI's principalId / resourceId and grants RBAC.
//
// apiVersion verified against current Azure docs (2026-06): 2024-11-30 is the
// latest non-preview Microsoft.ManagedIdentity/userAssignedIdentities version.
// =============================================================================

@description('Azure region for the identity.')
param location string

@description('Name of the user-assigned managed identity.')
param name string

@description('Tags applied to the identity.')
param tags object = {}

resource uami 'Microsoft.ManagedIdentity/userAssignedIdentities@2024-11-30' = {
  name: name
  location: location
  tags: tags
}

@description('Resource ID of the UAMI (used as the dictionary key in identity blocks and as the registries/secrets identity).')
output id string = uami.id

@description('AAD object (principal) ID -- the value used in every roleAssignment principalId.')
output principalId string = uami.properties.principalId

@description('Client ID -- passed as AZURE_CLIENT_ID so DefaultAzureCredential picks this identity, and embedded in the mssql connection string.')
output clientId string = uami.properties.clientId

@description('The UAMI resource name.')
output name string = uami.name

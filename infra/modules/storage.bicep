// =============================================================================
// storage.bicep
// StorageV2 / Standard_LRS account holding the private "charts" blob container.
// allowBlobPublicAccess:false -- the container is never public; the app reads
// and writes blobs as the UAMI (Storage Blob Data Contributor, granted below).
// No storage account keys are used anywhere.
//
// LIFECYCLE: chart blobs are deleted ~90 days after last modification to bound
// cost. NOTE: Signal.chart_path rows in SQL outlive this 90-day TTL, so an older
// signal's chart will 404 once the blob is gone -- the app handles this by
// degrading to a chartless render (handled in code). This is intentional.
//
// apiVersion verified against current Azure docs (2026-06): 2026-04-01 is the
// latest non-preview Microsoft.Storage version (account + blobServices +
// containers + managementPolicies); roleAssignments stable is 2022-04-01.
// =============================================================================

@description('Azure region for the storage account.')
param location string

@description('Globally-unique storage account name (3-24 chars, lowercase alphanumeric).')
@minLength(3)
@maxLength(24)
param name string

@description('Name of the private blob container.')
param containerName string = 'charts'

@description('Delete chart blobs older than this many days (last-modified).')
param chartRetentionDays int = 90

@description('Object (principal) ID of the UAMI that reads/writes blobs.')
param uamiPrincipalId string

@description('Tags applied to the storage account.')
param tags object = {}

// Built-in role: Storage Blob Data Contributor. RE-VERIFY before a real deploy:
//   az role definition list --name "Storage Blob Data Contributor" --query "[0].name" -o tsv
var blobDataContributorRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'

resource storage 'Microsoft.Storage/storageAccounts@2026-04-01' = {
  name: name
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'
  }
  properties: {
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowSharedKeyAccess: false // force Entra/UAMI auth -- no account-key access
    publicNetworkAccess: 'Enabled'
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2026-04-01' = {
  parent: storage
  name: 'default'
}

resource chartsContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2026-04-01' = {
  parent: blobService
  name: containerName
  properties: {
    publicAccess: 'None'
  }
}

// Lifecycle: hard-delete chart blobs N days after last modification.
resource lifecycle 'Microsoft.Storage/storageAccounts/managementPolicies@2026-04-01' = {
  parent: storage
  name: 'default'
  properties: {
    policy: {
      rules: [
        {
          name: 'expire-old-charts'
          enabled: true
          type: 'Lifecycle'
          definition: {
            filters: {
              blobTypes: [
                'blockBlob'
              ]
              prefixMatch: [
                '${containerName}/'
              ]
            }
            actions: {
              baseBlob: {
                delete: {
                  daysAfterModificationGreaterThan: chartRetentionDays
                }
              }
            }
          }
        }
      ]
    }
  }
}

// Grant the UAMI blob data access, scoped to THIS account.
resource blobRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, uamiPrincipalId, blobDataContributorRoleId)
  scope: storage
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobDataContributorRoleId)
    principalId: uamiPrincipalId
    principalType: 'ServicePrincipal'
  }
}

@description('Primary blob endpoint, e.g. https://acct.blob.core.windows.net/ -- maps to SWING_BLOB_ACCOUNT_URL.')
output blobAccountUrl string = storage.properties.primaryEndpoints.blob

@description('Storage account name.')
output name string = storage.name

@description('Blob container name -- maps to SWING_BLOB_CONTAINER.')
output containerName string = chartsContainer.name

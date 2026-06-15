// =============================================================================
// keyvault.bicep
// Key Vault (RBAC authorization) holding the four app secrets. The UAMI is
// granted Key Vault Secrets User and reads them at runtime; the Container Apps
// jobs reference them via secrets[].keyVaultUrl + identity:<UAMI>.
//
// Secret VALUES come from @secure() params -- NEVER hardcoded, never echoed to
// any output. Real values are supplied at deploy time and never committed.
//
// apiVersions verified against current Azure docs (2026-06): 2026-02-01 is the
// latest non-preview Microsoft.KeyVault/vaults version (and vaults/secrets);
// roleAssignments stable is 2022-04-01.
// =============================================================================

@description('Azure region for the vault.')
param location string

@description('Globally-unique Key Vault name (3-24 chars).')
@minLength(3)
@maxLength(24)
param name string

@description('AAD tenant ID for the vault.')
param tenantId string = subscription().tenantId

@description('Object (principal) ID of the UAMI that reads secrets.')
param uamiPrincipalId string

@description('Tags applied to the vault.')
param tags object = {}

// --- Secret values (supplied at deploy time; never committed) ---
@secure()
@description('Anthropic API key -> ANTHROPIC_API_KEY.')
param anthropicApiKey string

@secure()
@description('Digest recipient list -> DIGEST_TO.')
param digestTo string

@description('Seed the secret VALUES from the @secure() params. Set false on re-deploys so existing secret versions (e.g. a manually-set key) are never overwritten.')
param seedSecrets bool = true

// Built-in role: Key Vault Secrets User. RE-VERIFY before a real deploy:
//   az role definition list --name "Key Vault Secrets User" --query "[0].name" -o tsv
var kvSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'

resource vault 'Microsoft.KeyVault/vaults@2026-02-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    tenantId: tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    enableRbacAuthorization: true // RBAC, not access policies
    enableSoftDelete: true
    publicNetworkAccess: 'Enabled'
  }
}

// Child secrets sourced strictly from @secure() params. Conditional on
// seedSecrets so a re-deploy doesn't reset a manually-rotated value.
resource secretAnthropic 'Microsoft.KeyVault/vaults/secrets@2026-02-01' = if (seedSecrets) {
  parent: vault
  name: 'anthropic-api-key'
  properties: {
    value: anthropicApiKey
  }
}

resource secretDigestTo 'Microsoft.KeyVault/vaults/secrets@2026-02-01' = if (seedSecrets) {
  parent: vault
  name: 'digest-to'
  properties: {
    value: digestTo
  }
}

// Grant the UAMI secret-read access, scoped to THIS vault.
resource kvRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(vault.id, uamiPrincipalId, kvSecretsUserRoleId)
  scope: vault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', kvSecretsUserRoleId)
    principalId: uamiPrincipalId
    principalType: 'ServicePrincipal'
  }
}

@description('Vault URI, e.g. https://myvault.vault.azure.net/ -> KEY_VAULT_URL.')
output vaultUri string = vault.properties.vaultUri

@description('Vault resource name.')
output name string = vault.name

// Secret NAMES only (not values, not version-pinned URIs), bundled into ONE
// object output. The jobs module builds unversioned reference URIs as
// `${vaultUri}secrets/<name>`, which lets the Container Apps secret auto-rotate
// to the latest version. Reading each deployed secret resource's .name keeps the
// module dependency graph forcing the secrets to exist before the jobs reference
// them. These are public secret NAMES carrying no secret material -- bundling
// them avoids the per-output secret-name linter heuristic.
// Literal secret NAMES (no values, no version-pinned URIs). Literals rather than
// reading the (now-conditional) secret resources' .name, so the output is valid
// whether or not seedSecrets created them this run. The jobs build unversioned
// reference URIs `${vaultUri}secrets/<name>` that auto-rotate to the latest version.
output secretNames object = {
  anthropic: 'anthropic-api-key'
  digestTo: 'digest-to'
}

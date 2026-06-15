// =============================================================================
// acs.bicep
// Azure Communication Services Email, authenticated by the shared managed
// identity -- NO email password/secret is stored anywhere. An Azure-managed
// domain gives a free `DoNotReply@<generated>.azurecomm.net` sender (no DNS
// verification). The UAMI is granted "Communication and Email Service Owner"
// on the ACS resource so it can send via Entra (DefaultAzureCredential).
// =============================================================================

@description('Resources are global; this is passed through for tags only.')
param namePrefix string

@description('Deterministic unique suffix from main.bicep.')
param resourceToken string

@description('Principal ID of the shared UAMI (gets the email-send role).')
param uamiPrincipalId string

@description('Where ACS stores data at rest (not the resource location, which is global).')
param dataLocation string = 'United States'

@description('Tags.')
param tags object = {}

// Communication and Email Service Owner (re-verify: az role definition list
// --name "Communication and Email Service Owner").
var emailSenderRoleId = '09976791-48a7-449e-bb21-39d1a415f350'

resource email 'Microsoft.Communication/emailServices@2023-04-01' = {
  name: '${namePrefix}-email-${resourceToken}'
  location: 'global'
  tags: tags
  properties: {
    dataLocation: dataLocation
  }
}

// Azure-managed domain: instant, free, no DNS setup. Name is the fixed literal
// 'AzureManagedDomain'. fromSenderDomain is the generated azurecomm.net domain.
resource domain 'Microsoft.Communication/emailServices/domains@2023-04-01' = {
  parent: email
  name: 'AzureManagedDomain'
  location: 'global'
  tags: tags
  properties: {
    domainManagement: 'AzureManaged'
    userEngagementTracking: 'Disabled'
  }
}

resource acs 'Microsoft.Communication/communicationServices@2023-04-01' = {
  name: '${namePrefix}-acs-${resourceToken}'
  location: 'global'
  tags: tags
  properties: {
    dataLocation: dataLocation
    linkedDomains: [
      domain.id
    ]
  }
}

resource acsRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acs.id, uamiPrincipalId, emailSenderRoleId)
  scope: acs
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', emailSenderRoleId)
    principalId: uamiPrincipalId
    principalType: 'ServicePrincipal'
  }
}

@description('ACS endpoint for the Email SDK (SWING_ACS_ENDPOINT).')
output acsEndpoint string = 'https://${acs.properties.hostName}'

@description('Verified MailFrom address (SWING_ACS_SENDER).')
output senderAddress string = 'DoNotReply@${domain.properties.fromSenderDomain}'

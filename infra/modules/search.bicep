@description('Location for the Azure AI Search service')
param location string = resourceGroup().location

@description('Environment name for naming')
param environmentName string

@description('Unique suffix')
param uniqueSuffix string

@description('Tags')
param tags object

@description('Principal ID of the user-assigned identity to grant access')
param identityPrincipalId string

var searchName = toLower('search-${environmentName}-${uniqueSuffix}')

resource searchService 'Microsoft.Search/searchServices@2024-06-01-preview' = {
  name: searchName
  location: location
  tags: tags
  sku: {
    name: 'free'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    replicaCount: 1
    partitionCount: 1
    hostingMode: 'default'
    authOptions: {
      aadOrApiKey: {
        aadAuthFailureMode: 'http401WithBearerChallenge'
      }
    }
  }
}

// Grant Search Index Data Contributor role to the app identity
var searchIndexDataContributorRoleId = '8ebe5a00-799e-43f5-93ac-243d3dce84a7'
resource searchDataContributorRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: searchService
  name: guid(searchService.id, identityPrincipalId, 'SearchIndexDataContributor')
  properties: {
    principalId: identityPrincipalId
    roleDefinitionId: '/subscriptions/${subscription().subscriptionId}/providers/Microsoft.Authorization/roleDefinitions/${searchIndexDataContributorRoleId}'
    principalType: 'ServicePrincipal'
  }
}

// Grant Search Service Contributor role for index management
var searchServiceContributorRoleId = '7ca78c08-252a-4471-8644-bb5ff32d4ba0'
resource searchServiceContributorRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: searchService
  name: guid(searchService.id, identityPrincipalId, 'SearchServiceContributor')
  properties: {
    principalId: identityPrincipalId
    roleDefinitionId: '/subscriptions/${subscription().subscriptionId}/providers/Microsoft.Authorization/roleDefinitions/${searchServiceContributorRoleId}'
    principalType: 'ServicePrincipal'
  }
}

output searchEndpoint string = 'https://${searchService.name}.search.windows.net'
output searchName string = searchService.name

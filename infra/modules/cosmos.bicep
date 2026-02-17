@description('Location for the Cosmos DB account')
param location string = resourceGroup().location

@description('Environment name for naming')
param environmentName string

@description('Unique suffix')
param uniqueSuffix string

@description('Tags')
param tags object

@description('Principal ID of the user-assigned identity to grant access')
param identityPrincipalId string

var accountName = toLower('cosmos-${environmentName}-${uniqueSuffix}')
var databaseName = 'voiceagent'
var conversationsContainerName = 'conversations'
var summariesContainerName = 'summaries'

resource cosmosAccount 'Microsoft.DocumentDB/databaseAccounts@2024-05-15' = {
  name: accountName
  location: location
  tags: tags
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    consistencyPolicy: {
      defaultConsistencyLevel: 'Session'
    }
    locations: [
      {
        locationName: location
        failoverPriority: 0
        isZoneRedundant: false
      }
    ]
    capabilities: [
      {
        name: 'EnableServerless'
      }
    ]
  }
}

resource database 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-05-15' = {
  parent: cosmosAccount
  name: databaseName
  properties: {
    resource: {
      id: databaseName
    }
  }
}

resource conversationsContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = {
  parent: database
  name: conversationsContainerName
  properties: {
    resource: {
      id: conversationsContainerName
      partitionKey: {
        paths: ['/callerId']
        kind: 'Hash'
      }
      indexingPolicy: {
        indexingMode: 'consistent'
        automatic: true
        includedPaths: [
          { path: '/callerId/?' }
          { path: '/epochMs/?' }
          { path: '/sessionId/?' }
        ]
        excludedPaths: [
          { path: '/*' }
        ]
      }
      defaultTtl: 2592000 // 30 days
    }
  }
}

resource summariesContainer 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = {
  parent: database
  name: summariesContainerName
  properties: {
    resource: {
      id: summariesContainerName
      partitionKey: {
        paths: ['/callerId']
        kind: 'Hash'
      }
    }
  }
}

// Grant the app identity Cosmos DB Built-in Data Contributor role
var cosmosDataContributorRoleId = '00000000-0000-0000-0000-000000000002'
resource cosmosRoleAssignment 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-05-15' = {
  parent: cosmosAccount
  name: guid(cosmosAccount.id, identityPrincipalId, cosmosDataContributorRoleId)
  properties: {
    principalId: identityPrincipalId
    roleDefinitionId: '${cosmosAccount.id}/sqlRoleDefinitions/${cosmosDataContributorRoleId}'
    scope: cosmosAccount.id
  }
}

output cosmosEndpoint string = cosmosAccount.properties.documentEndpoint
output cosmosDatabaseName string = databaseName
output cosmosAccountName string = cosmosAccount.name

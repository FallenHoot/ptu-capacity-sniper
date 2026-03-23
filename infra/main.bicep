// PTU Capacity Sniper — Azure Function App deployment (Flex Consumption)
// Deploys: App Service Plan (FC1 Flex) + Function App + Storage Account + Managed Identity RBAC
// Works with Azure AI Foundry / Cognitive Services accounts for PTU deployments

targetScope = 'resourceGroup'

@description('Location for all resources')
param location string = 'swedencentral'

@description('Unique suffix for resource names')
param uniqueSuffix string = uniqueString(resourceGroup().id)

@description('The resource ID of the target Azure AI Foundry / Cognitive Services account for PTU deployments')
param cognitiveServicesAccountId string

@description('Subscription ID for the target Foundry resource')
param targetSubscriptionId string

@description('Resource group name for the target Foundry resource')
param targetResourceGroup string

@description('Account name for the target Foundry resource')
param targetAccountName string

@description('Target PTU count')
param targetPtus int = 74

@description('Model name')
param modelName string = 'gpt-5.2'

@description('Model version')
param modelVersion string = '2025-12-11'

@description('SKU name for deployment type')
param skuName string = 'DataZoneProvisionedManaged'

@description('Max parallel deployments')
param maxDeployments int = 4

@description('Teams webhook URL for alerts (optional)')
@secure()
param teamsWebhookUrl string = ''

// ---- Storage Account (identity-based auth for Flex Consumption) ----
resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: 'stptusniper${uniqueSuffix}'
  location: location
  sku: { name: 'Standard_LRS' }
  kind: 'StorageV2'
  tags: {
    SecurityControl: 'Ignore'
  }
  properties: {
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: true // Required for Function App deployment
  }
}

// ---- Blob Services + deployment container (required for Flex Consumption) ----
resource blobServices 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storageAccount
  name: 'default'
}

resource deploymentContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobServices
  name: 'app-package-container'
}

// ---- App Service Plan (Flex Consumption FC1) ----
resource appServicePlan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: 'asp-ptu-sniper-${uniqueSuffix}'
  location: location
  sku: {
    name: 'FC1'
    tier: 'FlexConsumption'
  }
  kind: 'functionapp'
  properties: {
    reserved: true // Linux
  }
}

// ---- Function App (Flex Consumption) ----
resource functionApp 'Microsoft.Web/sites@2024-04-01' = {
  name: 'func-ptu-sniper-${uniqueSuffix}'
  location: location
  kind: 'functionapp,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: appServicePlan.id
    httpsOnly: true
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${storageAccount.properties.primaryEndpoints.blob}app-package-container'
          authentication: {
            type: 'StorageAccountConnectionString'
            storageAccountConnectionStringName: 'AzureWebJobsStorage'
          }
        }
      }
      runtime: {
        name: 'python'
        version: '3.12'
      }
      scaleAndConcurrency: {
        maximumInstanceCount: 1 // Only need 1 instance for a sniper
        instanceMemoryMB: 2048
      }
    }
    siteConfig: {
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      appSettings: [
        {
          name: 'AzureWebJobsStorage'
          value: 'DefaultEndpointsProtocol=https;AccountName=${storageAccount.name};EndpointSuffix=${environment().suffixes.storage};AccountKey=${storageAccount.listKeys().keys[0].value}'
        }
        {
          name: 'AZURE_SUBSCRIPTION_ID'
          value: targetSubscriptionId
        }
        {
          name: 'AZURE_RESOURCE_GROUP'
          value: targetResourceGroup
        }
        {
          name: 'AZURE_ACCOUNT_NAME'
          value: targetAccountName
        }
        {
          name: 'PTU_TARGET'
          value: string(targetPtus)
        }
        {
          name: 'PTU_MODEL_NAME'
          value: modelName
        }
        {
          name: 'PTU_MODEL_VERSION'
          value: modelVersion
        }
        {
          name: 'PTU_SKU_NAME'
          value: skuName
        }
        {
          name: 'PTU_MAX_DEPLOYMENTS'
          value: string(maxDeployments)
        }
        {
          name: 'TEAMS_WEBHOOK_URL'
          value: teamsWebhookUrl
        }
        {
          name: 'AZURE_FUNCTION_APP_NAME'
          value: 'func-ptu-sniper-${uniqueSuffix}'
        }
        {
          name: 'CROSS_SKU_FALLBACK'
          value: 'true'
        }
      ]
    }
  }
}

// ---- RBAC: Give Function App's managed identity Cognitive Services Contributor on target account ----
// Role: Cognitive Services Contributor = 25fbc0a9-bd7c-42a3-aa1a-3b75d497ee68
resource roleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(functionApp.id, cognitiveServicesAccountId, '25fbc0a9-bd7c-42a3-aa1a-3b75d497ee68')
  scope: cognitiveServicesAccount
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      '25fbc0a9-bd7c-42a3-aa1a-3b75d497ee68'
    )
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// Reference the existing Cognitive Services account for scoping the role assignment
// NOTE: The Cognitive Services account must be in the same resource group as this deployment.
// For cross-RG scenarios, grant Cognitive Services Contributor manually:
//   az role assignment create --assignee <principalId> --role "Cognitive Services Contributor" \
//     --scope <cognitiveServicesAccountId>
resource cognitiveServicesAccount 'Microsoft.CognitiveServices/accounts@2024-10-01' existing = {
  name: targetAccountName
}

// ---- RBAC: Give Function App Website Contributor on itself (for config save from dashboard) ----
// Role: Website Contributor = de139f84-1756-47ae-9be6-808fbbe84772
resource selfRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(functionApp.id, functionApp.id, 'de139f84-1756-47ae-9be6-808fbbe84772')
  scope: functionApp
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      'de139f84-1756-47ae-9be6-808fbbe84772'
    )
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// ---- Outputs ----
output functionAppName string = functionApp.name
output functionAppDefaultHostName string = functionApp.properties.defaultHostName
output functionAppPrincipalId string = functionApp.identity.principalId
output storageAccountName string = storageAccount.name

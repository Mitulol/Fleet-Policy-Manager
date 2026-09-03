// Fleet Policy Manager — Azure Container Apps deployment
// =====================================================
//
// Deploys the platform that runs locally under docker-compose onto Azure
// Container Apps, with managed Azure Cache for Redis for the event bus and
// Azure Database for PostgreSQL for the Compliance store. The application
// images are unchanged — every difference from the local stack is expressed
// here as configuration.
//
// Mapping from docker-compose to this file:
//
//   compose service          ->  Azure resource
//   ----------------------------------------------------------------------
//   registry                 ->  Container App, internal ingress, 1 replica
//   policy                   ->  Container App, internal ingress, 1 replica
//   compliance (x3)          ->  Container App, internal ingress, 1..N replicas
//   compliance-lb (nginx)    ->  dropped — the platform ingress load-balances
//                                across compliance replicas
//   gateway                  ->  Container App, external ingress (public)
//   dashboard                ->  Container App, external ingress (public)
//   redis                    ->  Azure Cache for Redis (Basic C0)
//   compliance-db (postgres) ->  Azure Database for PostgreSQL Flexible Server
//
// This template expects the container images to already exist in the target
// registry — infra/deploy.sh builds and pushes them with `az acr build`
// before deploying this template.

targetScope = 'resourceGroup'

// ---------------------------------------------------------------- parameters

@description('Short prefix for resource names. Lowercase letters and numbers only.')
@minLength(3)
@maxLength(11)
param namePrefix string = 'fleetpm'

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Name of an existing Azure Container Registry that already holds the images.')
param acrName string

@description('Image tag to deploy (deploy.sh sets this to a short git SHA).')
param imageTag string = 'latest'

@description('PostgreSQL administrator login.')
param pgAdminUser string = 'fleetadmin'

@description('PostgreSQL administrator password.')
@secure()
param pgAdminPassword string

@description('Comma-separated admin API keys for the gateway.')
@secure()
param gatewayAdminKeys string

@description('Comma-separated device API keys for the gateway.')
@secure()
param gatewayDeviceKeys string

@description('Maximum number of Compliance replicas the autoscaler may create.')
@minValue(1)
@maxValue(20)
param complianceMaxReplicas int = 5

@description('HTTP concurrent-request count per replica that triggers a Compliance scale-out. Lower values scale out sooner; ~12-20 makes the autoscaling visible under a few hundred simulated devices.')
param complianceConcurrency int = 15

// ---------------------------------------------------------------- naming

var redisName = '${namePrefix}-redis'
var pgName = '${namePrefix}-pg'
var envName = '${namePrefix}-env'
var lawName = '${namePrefix}-logs'
var pgDatabaseName = 'compliance'

// ---------------------------------------------------------------- existing registry

resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' existing = {
  name: acrName
}

// ---------------------------------------------------------------- observability

resource law 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: lawName
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    // Keep retention short — this is a demo environment, not a system of record.
    retentionInDays: 30
  }
}

// ---------------------------------------------------------------- event bus

resource redis 'Microsoft.Cache/redis@2024-03-01' = {
  name: redisName
  location: location
  properties: {
    sku: {
      name: 'Basic'
      family: 'C'
      capacity: 0
    }
    redisVersion: '6'
    // TLS only. The application connects with the rediss:// scheme.
    enableNonSslPort: false
    minimumTlsVersion: '1.2'
  }
}

// Redis Streams and consumer groups (XADD / XREADGROUP / XAUTOCLAIM / XACK)
// are supported on Basic tier. Note: Basic has no persistence and no replica —
// if the cache is rebooted by the platform, undelivered events in the stream
// are lost. Standard tier adds a replica; Premium adds persistence. For a demo
// the event volume is low (policy rollouts are infrequent) and the Compliance
// consumer reclaims in-flight work on reconnect, so Basic is the pragmatic
// choice. This is called out in the deployment section of the README.

var redisHost = redis.properties.hostName
var redisKey = redis.listKeys().primaryKey
var redisUrl = 'rediss://:${redisKey}@${redisHost}:${redis.properties.sslPort}/0'

// ---------------------------------------------------------------- compliance store

resource pg 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: pgName
  location: location
  sku: {
    name: 'Standard_B1ms'
    tier: 'Burstable'
  }
  properties: {
    version: '16'
    administratorLogin: pgAdminUser
    administratorLoginPassword: pgAdminPassword
    storage: {
      storageSizeGB: 32
    }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: {
      mode: 'Disabled'
    }
  }

  // "Allow public access from any Azure service within Azure to this server."
  // Container Apps outbound traffic originates from Azure, so this lets the
  // Compliance replicas reach the database without VNet integration. A
  // production deployment would put both behind a private VNet instead.
  resource allowAzure 'firewallRules@2024-08-01' = {
    name: 'AllowAllAzureServices'
    properties: {
      startIpAddress: '0.0.0.0'
      endIpAddress: '0.0.0.0'
    }
  }

  resource database 'databases@2024-08-01' = {
    name: pgDatabaseName
    properties: {
      charset: 'UTF8'
      collation: 'en_US.utf8'
    }
  }
}

var pgHost = pg.properties.fullyQualifiedDomainName
// asyncpg reads libpq-style sslmode from the DSN. 'require' encrypts without
// verifying the server certificate chain, which avoids shipping the Azure CA
// bundle into the image.
var complianceDsn = 'postgresql://${pgAdminUser}:${pgAdminPassword}@${pgHost}:5432/${pgDatabaseName}?sslmode=require'

// ---------------------------------------------------------------- container apps environment

resource containerEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: envName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: law.properties.customerId
        sharedKey: law.listKeys().primarySharedKey
      }
    }
  }
}

// Internal apps are addressed as <app>.internal.<defaultDomain>; the public
// gateway and dashboard are addressed as <app>.<defaultDomain>.
//
// East-west calls (gateway -> backends) use http:// on port 80. The traffic
// stays inside the Container Apps environment, and plain HTTP avoids depending
// on TLS certificate coverage for the *.internal subdomain. The public
// endpoints keep https://.
var envDomain = containerEnv.properties.defaultDomain
var registryUrl = 'http://registry.internal.${envDomain}'
var policyUrl = 'http://policy.internal.${envDomain}'
var complianceUrl = 'http://compliance.internal.${envDomain}'
var gatewayPublicUrl = 'https://gateway.${envDomain}'
var dashboardPublicUrl = 'https://dashboard.${envDomain}'

var acrLoginServer = acr.properties.loginServer
var acrCreds = acr.listCredentials()

// Registry block reused by every container app.
var registryConfig = [
  {
    server: acrLoginServer
    username: acrCreds.username
    passwordSecretRef: 'acr-password'
  }
]
var acrPasswordSecret = {
  name: 'acr-password'
  value: acrCreds.passwords[0].value
}

// ---------------------------------------------------------------- Device Registry Service

resource registryApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'registry'
  location: location
  properties: {
    managedEnvironmentId: containerEnv.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: false
        // Internal ingress 301-redirects HTTP to HTTPS by default. The gateway
        // calls these backends over http:// on the environment's private
        // network, so allow plain HTTP and skip the redirect.
        allowInsecure: true
        targetPort: 8001
        transport: 'http'
      }
      registries: registryConfig
      secrets: [ acrPasswordSecret ]
    }
    template: {
      containers: [
        {
          name: 'registry'
          image: '${acrLoginServer}/fleet/registry:${imageTag}'
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            { name: 'REGISTRY_DB_PATH', value: '/tmp/registry.db' }
            { name: 'HEARTBEAT_TTL_SECONDS', value: '45' }
            { name: 'HEARTBEAT_INTERVAL_SECONDS', value: '15' }
          ]
        }
      ]
      scale: {
        // The registry owns a single SQLite file. It must never run more than
        // one replica, or concurrent writers corrupt the database.
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
}

// ---------------------------------------------------------------- Policy Service

resource policyApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'policy'
  location: location
  properties: {
    managedEnvironmentId: containerEnv.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: false
        allowInsecure: true
        targetPort: 8002
        transport: 'http'
      }
      registries: registryConfig
      secrets: [
        acrPasswordSecret
        { name: 'redis-url', value: redisUrl }
      ]
    }
    template: {
      containers: [
        {
          name: 'policy'
          image: '${acrLoginServer}/fleet/policy:${imageTag}'
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            { name: 'POLICY_DB_PATH', value: '/tmp/policy.db' }
            { name: 'REDIS_URL', secretRef: 'redis-url' }
          ]
        }
      ]
      scale: {
        // Single SQLite writer, and the sole event publisher. One replica.
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
}

// ---------------------------------------------------------------- Compliance Service (scaled)

resource complianceApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'compliance'
  location: location
  properties: {
    managedEnvironmentId: containerEnv.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: false
        allowInsecure: true
        targetPort: 8003
        transport: 'http'
      }
      registries: registryConfig
      secrets: [
        acrPasswordSecret
        { name: 'redis-url', value: redisUrl }
        { name: 'redis-password', value: redisKey }
        { name: 'compliance-dsn', value: complianceDsn }
      ]
    }
    template: {
      containers: [
        {
          name: 'compliance'
          image: '${acrLoginServer}/fleet/compliance:${imageTag}'
          resources: {
            cpu: json('0.75')
            memory: '1.5Gi'
          }
          env: [
            { name: 'COMPLIANCE_DSN', secretRef: 'compliance-dsn' }
            { name: 'REDIS_URL', secretRef: 'redis-url' }
            { name: 'REDIS_HOST', value: redisHost }
            { name: 'COMPLIANCE_CONSUMER_GROUP', value: 'compliance-workers' }
            { name: 'COMPLIANCE_STALE_SECONDS', value: '90' }
            // INSTANCE_ID is intentionally unset: the app falls back to the
            // container hostname, which Container Apps makes unique per
            // replica, so per-replica load is visible without extra config.
          ]
        }
      ]
      scale: {
        // Always at least one replica so the event-bus consumer group always
        // has a live member — that keeps exactly-once processing clean across
        // scale events.
        minReplicas: 1
        maxReplicas: complianceMaxReplicas
        rules: [
          {
            // Primary, always-reliable trigger. Matches the load-test story:
            // sustained device traffic drives replica count up.
            name: 'http-concurrency'
            http: {
              metadata: {
                concurrentRequests: string(complianceConcurrency)
              }
            }
          }
          {
            // Secondary trigger: scale out when the event-bus consumer group
            // falls behind. This is the more accurate signal for the async
            // workload, but KEDA's redis-streams scaler metadata keys have
            // changed across versions — verify 'lagCount' vs
            // 'pendingEntriesCount' against the KEDA version in your Container
            // Apps environment if this rule does not trigger.
            name: 'event-stream-lag'
            custom: {
              type: 'redis-streams'
              metadata: {
                host: redisHost
                port: '6380'
                enableTLS: 'true'
                stream: 'fleet.events'
                consumerGroup: 'compliance-workers'
                lagCount: '20'
                databaseIndex: '0'
              }
              auth: [
                {
                  secretRef: 'redis-password'
                  triggerParameter: 'password'
                }
              ]
            }
          }
        ]
      }
    }
  }
}

// ---------------------------------------------------------------- API Gateway (public)

resource gatewayApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'gateway'
  location: location
  properties: {
    managedEnvironmentId: containerEnv.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'http'
      }
      registries: registryConfig
      secrets: [
        acrPasswordSecret
        { name: 'redis-url', value: redisUrl }
        { name: 'gateway-admin-keys', value: gatewayAdminKeys }
        { name: 'gateway-device-keys', value: gatewayDeviceKeys }
      ]
    }
    template: {
      containers: [
        {
          name: 'gateway'
          image: '${acrLoginServer}/fleet/gateway:${imageTag}'
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: [
            { name: 'REGISTRY_URL', value: registryUrl }
            { name: 'POLICY_URL', value: policyUrl }
            { name: 'COMPLIANCE_URL', value: complianceUrl }
            { name: 'REDIS_URL', secretRef: 'redis-url' }
            { name: 'GATEWAY_ADMIN_KEYS', secretRef: 'gateway-admin-keys' }
            { name: 'GATEWAY_DEVICE_KEYS', secretRef: 'gateway-device-keys' }
            { name: 'DASHBOARD_URL', value: dashboardPublicUrl }
            // Headroom for the cloud load test.
            { name: 'RATE_LIMIT_DEVICE_BURST', value: '4000' }
            { name: 'RATE_LIMIT_DEVICE_PER_SECOND', value: '2000' }
            { name: 'RATE_LIMIT_ADMIN_BURST', value: '600' }
            { name: 'RATE_LIMIT_ADMIN_PER_SECOND', value: '300' }
          ]
        }
      ]
      scale: {
        // Stateless — the rate-limit buckets live in Redis, shared across
        // replicas — so this scales safely.
        minReplicas: 1
        maxReplicas: 3
        rules: [
          {
            name: 'http-concurrency'
            http: {
              metadata: {
                concurrentRequests: '80'
              }
            }
          }
        ]
      }
    }
  }
}

// ---------------------------------------------------------------- Dashboard (public)

resource dashboardApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'dashboard'
  location: location
  properties: {
    managedEnvironmentId: containerEnv.id
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8090
        transport: 'http'
      }
      registries: registryConfig
      secrets: [
        acrPasswordSecret
        // The dashboard authenticates to the gateway with an admin key,
        // server-side, and never exposes it to the browser. deploy.sh
        // generates a single admin key, so passing the whole value works; if
        // you supply a comma-separated list, put the dashboard's key first.
        { name: 'gateway-api-key', value: gatewayAdminKeys }
      ]
    }
    template: {
      containers: [
        {
          name: 'dashboard'
          image: '${acrLoginServer}/fleet/dashboard:${imageTag}'
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
          env: [
            { name: 'GATEWAY_URL', value: gatewayPublicUrl }
            { name: 'GATEWAY_API_KEY', secretRef: 'gateway-api-key' }
            { name: 'DASHBOARD_REFRESH_SECONDS', value: '2' }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
}

// ---------------------------------------------------------------- outputs

output gatewayUrl string = gatewayPublicUrl
output dashboardUrl string = dashboardPublicUrl
output registryLoginServer string = acrLoginServer
output redisHostName string = redisHost
output postgresHostName string = pgHost
output containerAppsEnvironment string = containerEnv.name

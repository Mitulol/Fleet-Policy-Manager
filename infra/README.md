# infra/ — Azure Container Apps deployment

Infrastructure-as-code and scripts to run Fleet Policy Manager on Azure. The
full walkthrough, cloud architecture diagram and cost breakdown are in the
[Deployment to Azure](../README.md#deployment-to-azure) section of the main
README.

| File | Purpose |
|---|---|
| `main.bicep` | Every Azure resource: Container Apps environment, 5 apps, Azure Cache for Redis, PostgreSQL Flexible Server, Log Analytics. |
| `main.parameters.json` | Non-secret parameter values. Secrets are passed by `deploy.sh` on the command line. |
| `deploy.sh` | Creates the resource group and registry, builds/pushes images with `az acr build`, deploys the template, writes `.deploy-state`. |
| `loadtest-cloud.sh` | Seeds policies, runs the fleet simulator against the public gateway, watches Compliance replicas scale. |
| `teardown.sh` | `--pause` stops the hourly-billed resources; no argument deletes the resource group. |
| `.deploy-state` | Written by `deploy.sh` — URLs and generated keys. Gitignored (contains secrets). |

## Prerequisites

```bash
az login
az account set --subscription "<name or id>"
az extension add --name containerapp --upgrade
az provider register --namespace Microsoft.App
az provider register --namespace Microsoft.Cache
az provider register --namespace Microsoft.DBforPostgreSQL
az provider register --namespace Microsoft.OperationalInsights
```

## Quick start

```bash
./infra/deploy.sh                  # ~10-15 min
./infra/loadtest-cloud.sh 500 240
./infra/teardown.sh                # when finished — Redis bills until deleted
```

Override defaults with environment variables:

```bash
RESOURCE_GROUP=my-rg LOCATION=westus2 NAME_PREFIX=fleetdemo ./infra/deploy.sh
```

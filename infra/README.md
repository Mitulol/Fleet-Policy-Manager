# infra/ — Azure Container Apps deployment

Infrastructure-as-code and scripts to run Fleet Policy Manager on Azure. The
full walkthrough, cloud architecture diagram and cost breakdown are in the
[Deployment to Azure](../README.md#deployment-to-azure) section of the main
README.

| File | Purpose |
|---|---|
| `main.bicep` | Every Azure resource: Container Apps environment, 5 apps, Azure Cache for Redis, PostgreSQL Flexible Server, Log Analytics. |
| `main.parameters.json` | Non-secret parameter values. Secrets are passed by `deploy.sh` on the command line. |
| `deploy.sh` | Creates the resource group and registry, builds/pushes the six images with local Docker, deploys the template, writes `.deploy-state`. `--what-if` previews without creating the apps / Redis / Postgres. |
| `loadtest-cloud.sh` | Seeds policies, runs the fleet simulator against the public gateway, watches Compliance replicas scale. |
| `teardown.sh` | `--pause` stops the hourly-billed resources; no argument deletes the resource group. |
| `.deploy-state` | Written by `deploy.sh` — URLs and generated keys. Gitignored (contains secrets). |

## Prerequisites

- Azure CLI, Docker running locally, an Azure subscription.

```bash
az login
az account set --subscription "<name or id>"
az extension add --name containerapp --upgrade
for ns in Microsoft.App Microsoft.Cache Microsoft.DBforPostgreSQL \
          Microsoft.OperationalInsights Microsoft.ContainerRegistry; do
  az provider register --namespace "$ns"
done
```

If your subscription restricts deployment regions (Azure for Students does),
check the allowed list and pass one as `LOCATION`:

```bash
az policy assignment list --query "[?displayName=='Allowed resource deployment regions'].parameters"
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

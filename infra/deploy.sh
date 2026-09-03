#!/usr/bin/env bash
#
# Deploy Fleet Policy Manager to Azure Container Apps.
#
# Prerequisites (run once, on your own machine — this cannot be done from a
# non-interactive session):
#   - Azure CLI installed:   https://learn.microsoft.com/cli/azure/install-azure-cli
#   - Signed in:             az login
#   - A subscription set:    az account set --subscription "<name or id>"
#   - The containerapp extension:  az extension add --name containerapp --upgrade
#   - Providers registered:
#       az provider register --namespace Microsoft.App
#       az provider register --namespace Microsoft.OperationalInsights
#       az provider register --namespace Microsoft.Cache
#       az provider register --namespace Microsoft.DBforPostgreSQL
#
# What this does:
#   1. Creates a resource group
#   2. Creates an Azure Container Registry
#   3. Builds and pushes all six images with `az acr build` (builds run in Azure)
#   4. Deploys everything else from infra/main.bicep
#   5. Writes connection details to infra/.deploy-state for the other scripts
#
# Configuration (override by exporting before running):
#   RESOURCE_GROUP   default: fleet-policy-manager
#   LOCATION         default: eastus
#   NAME_PREFIX      default: fleetpm   (3-11 lowercase alphanumerics)
#
# Usage:
#   ./infra/deploy.sh

set -euo pipefail

RESOURCE_GROUP="${RESOURCE_GROUP:-fleet-policy-manager}"
LOCATION="${LOCATION:-eastus}"
NAME_PREFIX="${NAME_PREFIX:-fleetpm}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_FILE="${REPO_ROOT}/infra/.deploy-state"
cd "${REPO_ROOT}"

# ---------------------------------------------------------------- preflight

command -v az >/dev/null || { echo "ERROR: Azure CLI ('az') not found. See the header of this script." >&2; exit 1; }
az account show >/dev/null 2>&1 || { echo "ERROR: not signed in. Run 'az login' first." >&2; exit 1; }

# Fail fast on a Bicep error before creating any resources.
az bicep build --file infra/main.bicep --stdout >/dev/null

SUBSCRIPTION="$(az account show --query name -o tsv)"
IMAGE_TAG="$(git rev-parse --short HEAD 2>/dev/null || echo manual)"

echo "Subscription:    ${SUBSCRIPTION}"
echo "Resource group:  ${RESOURCE_GROUP}  (${LOCATION})"
echo "Name prefix:     ${NAME_PREFIX}"
echo "Image tag:       ${IMAGE_TAG}"
echo

# ---------------------------------------------------------------- secrets

# Reuse the values from a previous run if present, so re-deploys are stable.
if [[ -f "${STATE_FILE}" ]]; then
  # shellcheck disable=SC1090
  source "${STATE_FILE}"
fi

# Alphanumeric only: avoids URL-encoding issues in the Postgres DSN, and
# upper+lower+digit already satisfies Azure's complexity rule (3 of 4 classes).
PG_ADMIN_PASSWORD="${PG_ADMIN_PASSWORD:-$(openssl rand -base64 30 | tr -dc 'A-Za-z0-9' | cut -c1-28)Aa1}"
GATEWAY_ADMIN_KEY="${GATEWAY_ADMIN_KEY:-adm_$(openssl rand -hex 20)}"
GATEWAY_DEVICE_KEY="${GATEWAY_DEVICE_KEY:-dev_$(openssl rand -hex 20)}"
ACR_NAME="${ACR_NAME:-${NAME_PREFIX}acr$(openssl rand -hex 3)}"

# ---------------------------------------------------------------- resource group

echo "==> Resource group"
az group create --name "${RESOURCE_GROUP}" --location "${LOCATION}" --output none

# ---------------------------------------------------------------- container registry

echo "==> Container registry: ${ACR_NAME}"
az acr create \
  --resource-group "${RESOURCE_GROUP}" \
  --name "${ACR_NAME}" \
  --sku Basic \
  --admin-enabled true \
  --output none

# ---------------------------------------------------------------- build images

# Each Dockerfile builds from the repo root so it can pull in shared/fleetcommon.
# .dockerignore keeps the uploaded context small.
build_image() {
  local name="$1" dockerfile="$2"
  echo "    building fleet/${name}:${IMAGE_TAG}"
  az acr build \
    --registry "${ACR_NAME}" \
    --image "fleet/${name}:${IMAGE_TAG}" \
    --image "fleet/${name}:latest" \
    --file "${dockerfile}" \
    --output none \
    .
}

echo "==> Building and pushing images (this runs in Azure, ~5-8 min total)"
build_image registry   services/registry/Dockerfile
build_image policy     services/policy/Dockerfile
build_image compliance services/compliance/Dockerfile
build_image gateway    services/gateway/Dockerfile
build_image dashboard  dashboard/Dockerfile
build_image simulator  tools/Dockerfile

# ---------------------------------------------------------------- deploy

echo "==> Deploying infrastructure and container apps"
DEPLOY_OUTPUTS="$(az deployment group create \
  --resource-group "${RESOURCE_GROUP}" \
  --template-file infra/main.bicep \
  --parameters infra/main.parameters.json \
  --parameters \
      namePrefix="${NAME_PREFIX}" \
      location="${LOCATION}" \
      acrName="${ACR_NAME}" \
      imageTag="${IMAGE_TAG}" \
      pgAdminPassword="${PG_ADMIN_PASSWORD}" \
      gatewayAdminKeys="${GATEWAY_ADMIN_KEY}" \
      gatewayDeviceKeys="${GATEWAY_DEVICE_KEY}" \
  --query properties.outputs \
  --output json)"

GATEWAY_URL="$(echo "${DEPLOY_OUTPUTS}"   | python3 -c 'import sys,json;print(json.load(sys.stdin)["gatewayUrl"]["value"])')"
DASHBOARD_URL="$(echo "${DEPLOY_OUTPUTS}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["dashboardUrl"]["value"])')"

# ---------------------------------------------------------------- persist state

cat > "${STATE_FILE}" <<EOF
# Written by infra/deploy.sh — consumed by loadtest-cloud.sh and teardown.sh.
RESOURCE_GROUP=${RESOURCE_GROUP}
LOCATION=${LOCATION}
NAME_PREFIX=${NAME_PREFIX}
ACR_NAME=${ACR_NAME}
GATEWAY_URL=${GATEWAY_URL}
DASHBOARD_URL=${DASHBOARD_URL}
GATEWAY_ADMIN_KEY=${GATEWAY_ADMIN_KEY}
GATEWAY_DEVICE_KEY=${GATEWAY_DEVICE_KEY}
PG_ADMIN_PASSWORD=${PG_ADMIN_PASSWORD}
EOF
chmod 600 "${STATE_FILE}"

# ---------------------------------------------------------------- done

cat <<EOF

============================================================================
DEPLOYED
============================================================================
  Dashboard     ${DASHBOARD_URL}
  API gateway   ${GATEWAY_URL}
  API docs      ${GATEWAY_URL}/docs

  Admin API key   ${GATEWAY_ADMIN_KEY}
  Device API key  ${GATEWAY_DEVICE_KEY}
  (also saved to infra/.deploy-state — gitignored)

Next:
  1. Seed policies:
       python3 tools/seed_policies.py --gateway ${GATEWAY_URL} --api-key ${GATEWAY_ADMIN_KEY}
  2. Run the cloud load test:
       ./infra/loadtest-cloud.sh
  3. When finished, tear everything down to stop billing:
       ./infra/teardown.sh

Cost note: Azure Cache for Redis (Basic) bills ~\$16/mo and cannot be paused,
only deleted. PostgreSQL can be stopped between demos. See the README.
============================================================================
EOF

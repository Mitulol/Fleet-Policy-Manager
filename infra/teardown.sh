#!/usr/bin/env bash
#
# Tear down the Azure deployment.
#
# Default: delete the whole resource group — the cleanest way to stop all
# billing. Everything deploy.sh created lives in that one group.
#
# Alternative: `./infra/teardown.sh --pause` stops only the paid always-on
# pieces (deletes Redis, stops PostgreSQL) and leaves the rest, so a later
# `deploy.sh` re-creates Redis and restarts Postgres without rebuilding
# everything. Container Apps scale down on their own when idle.
#
# Usage:
#   ./infra/teardown.sh            # delete the resource group
#   ./infra/teardown.sh --pause    # just stop the hourly-billed resources

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_FILE="${REPO_ROOT}/infra/.deploy-state"

[[ -f "${STATE_FILE}" ]] || { echo "ERROR: ${STATE_FILE} not found — nothing recorded to tear down." >&2; exit 1; }
# shellcheck disable=SC1090
source "${STATE_FILE}"

command -v az >/dev/null || { echo "ERROR: Azure CLI not found." >&2; exit 1; }

MODE="${1:-delete}"

if [[ "${MODE}" == "--pause" ]]; then
  echo "Pausing hourly-billed resources in ${RESOURCE_GROUP}:"
  echo "  - deleting Azure Cache for Redis (${NAME_PREFIX}-redis)"
  az redis delete --name "${NAME_PREFIX}-redis" --resource-group "${RESOURCE_GROUP}" --yes --output none || true
  echo "  - stopping PostgreSQL flexible server (${NAME_PREFIX}-pg)"
  az postgres flexible-server stop --name "${NAME_PREFIX}-pg" --resource-group "${RESOURCE_GROUP}" --output none || true
  cat <<EOF

Paused. Standing cost is now just the container registry (~\$5/mo) and
PostgreSQL storage (~\$4/mo). Container Apps bill near zero while idle.

Redis was DELETED (Basic tier cannot be stopped). The next ./infra/deploy.sh
re-creates it and redeploys the apps with the new connection string.
EOF
  exit 0
fi

echo "This will DELETE the entire resource group '${RESOURCE_GROUP}' and everything in it:"
echo "  - Container Apps environment and all apps"
echo "  - Azure Cache for Redis"
echo "  - Azure Database for PostgreSQL (including its data)"
echo "  - Container Registry and all images"
echo "  - Log Analytics workspace"
echo
read -r -p "Type the resource group name to confirm: " CONFIRM
if [[ "${CONFIRM}" != "${RESOURCE_GROUP}" ]]; then
  echo "Did not match. Aborted."
  exit 1
fi

az group delete --name "${RESOURCE_GROUP}" --yes --no-wait
rm -f "${STATE_FILE}"

echo
echo "Deletion started (running in the background on Azure). Verify with:"
echo "  az group exists --name ${RESOURCE_GROUP}"

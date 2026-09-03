#!/usr/bin/env bash
#
# Run the fleet simulator against the deployed Azure environment and watch the
# Compliance service autoscale under load.
#
# Reads connection details from infra/.deploy-state (written by deploy.sh).
#
# Usage:
#   ./infra/loadtest-cloud.sh [DEVICES] [DURATION_SECONDS]
#   ./infra/loadtest-cloud.sh 500 240

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_FILE="${REPO_ROOT}/infra/.deploy-state"
cd "${REPO_ROOT}"

[[ -f "${STATE_FILE}" ]] || { echo "ERROR: ${STATE_FILE} not found. Run ./infra/deploy.sh first." >&2; exit 1; }
# shellcheck disable=SC1090
source "${STATE_FILE}"

DEVICES="${1:-500}"
DURATION="${2:-240}"

command -v az >/dev/null || { echo "ERROR: Azure CLI not found." >&2; exit 1; }

# The simulator needs httpx. Use the repo virtualenv if present, else a temp one.
PYTHON="python3"
if [[ -x "${REPO_ROOT}/.venv/bin/python" ]]; then
  PYTHON="${REPO_ROOT}/.venv/bin/python"
fi
"${PYTHON}" -c 'import httpx' 2>/dev/null || {
  echo "==> Installing httpx into .venv"
  python3 -m venv "${REPO_ROOT}/.venv"
  "${REPO_ROOT}/.venv/bin/pip" install -q -r tools/requirements.txt
  PYTHON="${REPO_ROOT}/.venv/bin/python"
}

echo "==> Seeding policies"
"${PYTHON}" tools/seed_policies.py --gateway "${GATEWAY_URL}" --api-key "${GATEWAY_ADMIN_KEY}"

echo
echo "==> Watching Compliance replica count in the background"
(
  while true; do
    count="$(az containerapp replica list \
      --name compliance \
      --resource-group "${RESOURCE_GROUP}" \
      --query 'length(@)' -o tsv 2>/dev/null || echo '?')"
    printf '    [%s]  compliance replicas: %s\n' "$(date +%H:%M:%S)" "${count}"
    sleep 15
  done
) &
WATCH_PID=$!
trap 'kill "${WATCH_PID}" 2>/dev/null || true' EXIT

echo
echo "==> Running simulator: ${DEVICES} devices for ${DURATION}s against ${GATEWAY_URL}"
"${PYTHON}" tools/simulator.py \
  --gateway "${GATEWAY_URL}" \
  --api-key "${GATEWAY_DEVICE_KEY}" \
  --devices "${DEVICES}" \
  --duration "${DURATION}" \
  --ramp-seconds 20 \
  --processes 4

kill "${WATCH_PID}" 2>/dev/null || true

echo
echo "==> Final replica state"
az containerapp replica list --name compliance --resource-group "${RESOURCE_GROUP}" \
  --query '[].{replica:name, created:properties.createdTime}' -o table || true

echo
echo "==> Compliance instance load distribution (from the service's own view)"
curl -s -H "X-API-Key: ${GATEWAY_ADMIN_KEY}" "${GATEWAY_URL}/api/compliance/stats" \
  | python3 -c 'import sys,json;d=json.load(sys.stdin);[print(f"    {i[\"instance\"]:<45} {i[\"reports_handled\"]:>7} reports") for i in d["instances"]]' || true

cat <<EOF

To demonstrate failover under load, in another shell while the simulator runs:
  az containerapp revision restart --name compliance --resource-group ${RESOURCE_GROUP} \\
    --revision \$(az containerapp revision list --name compliance --resource-group ${RESOURCE_GROUP} --query '[0].name' -o tsv)
The report count keeps climbing and no requests fail — the platform ingress
routes around the restarting replicas.
EOF

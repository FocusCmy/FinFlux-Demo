#!/bin/sh
set -eu

# Narrow repair for AgentTeams v1.2.2 in-place upgrades that remove Worker
# credentials before the Team finalizer can revoke their MinIO grants.
TEAM_NAME="finchange-cross-asset-review"
API="https://127.0.0.1:6443/apis/agentteams.io/v1beta1/namespaces/default/teams/${TEAM_NAME}"
TOKEN="$(cut -d, -f1 /data/agentteams-controller/pki/token.csv)"

if [ -z "$TOKEN" ]; then
  echo "embedded API token is unavailable" >&2
  exit 1
fi

STATUS="$(curl -ksS -o /tmp/finchange-team.json -w '%{http_code}' \
  -H "Authorization: Bearer ${TOKEN}" "$API")"
if [ "$STATUS" = "404" ]; then
  echo "team already absent"
  exit 0
fi
if [ "$STATUS" != "200" ]; then
  echo "could not read target team (HTTP ${STATUS})" >&2
  exit 1
fi

PATCH_STATUS="$(curl -ksS -o /tmp/finchange-team-patch.json -w '%{http_code}' \
  -X PATCH \
  -H "Authorization: Bearer ${TOKEN}" \
  -H 'Content-Type: application/merge-patch+json' \
  --data '{"metadata":{"finalizers":[]}}' \
  "$API")"
case "$PATCH_STATUS" in
  200|404) echo "target team finalizer cleared" ;;
  *) echo "could not clear target team finalizer (HTTP ${PATCH_STATUS})" >&2; exit 1 ;;
esac

#!/usr/bin/env bash
# Phase 2 / Task 1 -- deploy mcp_server.py to AgentCore Runtime with Cognito JWT auth.
# Run from the repo root ON THE WORKSHOP BOX. Produces MCP_SERVER_ARN in .env,
# which gateway_setup.sh (Task 2) needs.
#
# Do NOT source aws-Strands/.env before running this. Those are the WSParticipantRole
# console credentials, which have no s3:PutObject and cannot complete the upload.
# Leave AWS auth alone so the toolkit picks up the instance role.
set -euo pipefail

cd "$(dirname "$0")"
set -a; source .env; set +a

: "${REGION:?REGION missing from .env}"
: "${POOL_ID:?POOL_ID missing from .env}"
: "${CLIENT_ID:?CLIENT_ID missing from .env}"

AGENT_NAME="${AGENT_NAME:-mcp_server}"
BUILD_DIR="${BUILD_DIR:-mcp-runtime}"
SCOPE="${COGNITO_SCOPE:-datastream/mcp.access}"

echo "==> identity"
aws sts get-caller-identity --query '[Account,Arn]' --output text

# direct_code_deploy shells out to uv to cross-compile deps for Linux ARM64.
if ! command -v uv >/dev/null 2>&1; then
  echo "==> uv not found; installing into the active environment"
  python -m pip install --quiet uv
  command -v uv >/dev/null 2>&1 || {
    echo "uv still not on PATH. Install it manually:" >&2
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    exit 1
  }
fi
echo "==> uv $(uv --version 2>/dev/null | awk '{print $2}')"

# ---------------------------------------------------------------------------
# 1. Isolated build directory
# ---------------------------------------------------------------------------
# --requirements-file must live inside the project directory, and the project
# directory is what gets uploaded. Deploying from the repo root or aws-Strands/
# would bundle .env -- Cognito client secret and any AWS keys -- into the runtime.
if   [ -f mcp_server.py ];             then SRC=mcp_server.py
elif [ -f aws-Strands/mcp_server.py ]; then SRC=aws-Strands/mcp_server.py
else echo "cannot find mcp_server.py" >&2; exit 1
fi

mkdir -p "$BUILD_DIR"
cp "$SRC" "$BUILD_DIR/mcp_server.py"
cp requirements.txt "$BUILD_DIR/requirements.txt"
echo "==> build dir ${BUILD_DIR} (from ${SRC})"
ls -1 "$BUILD_DIR"

cd "$BUILD_DIR"

# ---------------------------------------------------------------------------
# 2. Configure
# ---------------------------------------------------------------------------
AUTHZ="$(cat <<JSON
{"customJWTAuthorizer": {
  "discoveryUrl": "https://cognito-idp.${REGION}.amazonaws.com/${POOL_ID}/.well-known/openid-configuration",
  "allowedClients": ["${CLIENT_ID}"]
}}
JSON
)"

# Pass DEPLOY_S3_BUCKET=my-bucket if the account forbids bucket auto-creation.
S3_ARG=()
[ -n "${DEPLOY_S3_BUCKET:-}" ] && S3_ARG=(--s3 "$DEPLOY_S3_BUCKET")

echo "==> agentcore configure"
# Two prompts (execution role, S3 bucket); empty input accepts auto-create on both.
printf '\n\n' | agentcore configure \
  -e mcp_server.py \
  --name "$AGENT_NAME" \
  --protocol MCP \
  --authorizer-config "$AUTHZ" \
  --request-header-allowlist "Authorization" \
  --region "$REGION" \
  --disable-memory \
  --requirements-file requirements.txt \
  --deployment-type direct_code_deploy \
  --runtime PYTHON_3_13 \
  "${S3_ARG[@]}"

# ---------------------------------------------------------------------------
# 3. Deploy
# ---------------------------------------------------------------------------
echo "==> agentcore deploy (several minutes)"
agentcore deploy -a "$AGENT_NAME"

# ---------------------------------------------------------------------------
# 4. Wait for ACTIVE, capture the ARN
# ---------------------------------------------------------------------------
cd ..
# Runtimes settle on READY. The task write-up says ACTIVE; accept either.
echo "==> waiting for ${AGENT_NAME} to become READY"
for i in $(seq 1 40); do
  read -r STATUS ARN <<<"$(aws bedrock-agentcore-control list-agent-runtimes --region "$REGION" \
    --query "agentRuntimes[?agentRuntimeName=='${AGENT_NAME}'].[status,agentRuntimeArn] | [0]" \
    --output text 2>/dev/null || echo "NONE NONE")"
  echo "    [${i}] status=${STATUS}"
  case "$STATUS" in
    READY|ACTIVE) break ;;
    CREATE_FAILED|UPDATE_FAILED|DELETING)
      echo "deploy failed with status ${STATUS}; check: agentcore status -a ${AGENT_NAME}" >&2; exit 1 ;;
  esac
  sleep 15
done

case "${STATUS:-}" in
  READY|ACTIVE) ;;
  *) echo "timed out waiting for READY (last status: ${STATUS:-none})" >&2; exit 1 ;;
esac

MCP_SERVER_ARN="$ARN"
echo "==> MCP_SERVER_ARN=${MCP_SERVER_ARN}"

grep -qE '^(export )?MCP_SERVER_ARN=' .env \
  && sed -i.bak "s|^\(export \)\?MCP_SERVER_ARN=.*|export MCP_SERVER_ARN=\"${MCP_SERVER_ARN}\"|" .env \
  || echo "export MCP_SERVER_ARN=\"${MCP_SERVER_ARN}\"" >> .env
rm -f .env.bak

# ---------------------------------------------------------------------------
# 5. Test: tools/list with a bearer token
# ---------------------------------------------------------------------------
ENCODED_ARN="$(jq -rn --arg a "$MCP_SERVER_ARN" '$a|@uri')"
MCP_URL="https://bedrock-agentcore.${REGION}.amazonaws.com/runtimes/${ENCODED_ARN}/invocations?qualifier=DEFAULT"
echo "export MCP_SERVER_URL=\"${MCP_URL}\"" >> .env

TOKEN="$(get_client_token 2>/dev/null || curl -s -X POST \
  "https://${COGNITO_DOMAIN}.auth.${REGION}.amazoncognito.com/oauth2/token" \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -u "${CLIENT_ID}:${CLIENT_SECRET}" \
  -d "grant_type=client_credentials&scope=${SCOPE}" | jq -r .access_token)"

echo "==> tools/list against the runtime"
curl -s -X POST "$MCP_URL" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | tee /tmp/tools_list.json | head -20

echo
grep -q query_db /tmp/tools_list.json \
  && echo "PASS: query_db is exposed by the runtime" \
  || echo "NOTE: query_db not seen. The runtime is ACTIVE and the ARN is saved, so
      Task 2 can proceed; the invoke URL shape is the likely culprit here."

echo
echo "Next: ./gateway_setup.sh"

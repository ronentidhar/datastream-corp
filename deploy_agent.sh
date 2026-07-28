#!/usr/bin/env bash
# Phase 2 / Task 4 -- deploy the orchestrator agent to AgentCore Runtime, wired to
# the Gateway (Task 2) for tools and AgentCore Memory (Task 3) for cross-session
# recall, behind a Cognito JWT authorizer.
#
# Run from the repo root. Needs GATEWAY_ID, MEMORY_ID and
# LONG_TERM_MEMORY_STRATEGY_ID in .env.
set -euo pipefail

cd "$(dirname "$0")"
set -a; source .env; set +a

: "${REGION:?REGION missing from .env}"
: "${POOL_ID:?POOL_ID missing from .env}"
: "${CLIENT_ID:?CLIENT_ID missing from .env}"
: "${CLIENT_SECRET:?CLIENT_SECRET missing from .env}"
: "${GATEWAY_ID:?GATEWAY_ID missing -- run ./gateway_setup.sh}"
: "${MEMORY_ID:?MEMORY_ID missing -- run ./memory_setup.sh}"
: "${LONG_TERM_MEMORY_STRATEGY_ID:?LONG_TERM_MEMORY_STRATEGY_ID missing -- run ./memory_setup.sh}"

AGENT_NAME="${AGENT_NAME:-datastream_agent}"
BUILD_DIR="${BUILD_DIR:-agent-runtime}"
SCOPE="${COGNITO_SCOPE:-datastream/mcp.access}"
PROVIDER_NAME="${PROVIDER_NAME:-datastream-cognito-oauth}"

echo "==> identity"
aws sts get-caller-identity --query '[Account,Arn]' --output text

if ! command -v uv >/dev/null 2>&1; then
  echo "==> installing uv (needed by direct_code_deploy)"
  python -m pip install --quiet uv
fi

# The credential provider must already exist -- @requires_access_token resolves it
# by name at runtime, and a wrong name fails only once the agent is invoked.
echo "==> checking credential provider ${PROVIDER_NAME}"
aws bedrock-agentcore-control get-oauth2-credential-provider \
  --region "$REGION" --name "$PROVIDER_NAME" \
  --query 'name' --output text >/dev/null || {
    echo "credential provider '${PROVIDER_NAME}' not found. Existing providers:" >&2
    aws bedrock-agentcore-control list-oauth2-credential-providers --region "$REGION" \
      --query 'credentialProviders[].name' --output text >&2
    exit 1
  }

# ---------------------------------------------------------------------------
# Build directory -- keeps .env out of the uploaded package
# ---------------------------------------------------------------------------
mkdir -p "$BUILD_DIR"
cp requirements.txt "$BUILD_DIR/requirements.txt"
[ -f "$BUILD_DIR/agent.py" ] || { echo "${BUILD_DIR}/agent.py is missing" >&2; exit 1; }
echo "==> build dir ${BUILD_DIR}"
ls -1 "$BUILD_DIR"

pushd "$BUILD_DIR" >/dev/null

# ---------------------------------------------------------------------------
# Configure
# ---------------------------------------------------------------------------
# The actor header must be allowlisted or per-actor memory isolation silently
# collapses to "default_user" for every caller.
AUTHZ="$(cat <<JSON
{"customJWTAuthorizer": {
  "discoveryUrl": "https://cognito-idp.${REGION}.amazonaws.com/${POOL_ID}/.well-known/openid-configuration",
  "allowedClients": ["${CLIENT_ID}"]
}}
JSON
)"

echo "==> agentcore configure ${AGENT_NAME}"
printf '\n\n' | agentcore configure \
  -e agent.py \
  --name "$AGENT_NAME" \
  --authorizer-config "$AUTHZ" \
  --request-header-allowlist "Authorization,${ACTOR_HEADER:-X-Amzn-Bedrock-AgentCore-Runtime-Custom-Actor-Id}" \
  --region "$REGION" \
  --disable-memory \
  --requirements-file requirements.txt \
  --deployment-type direct_code_deploy \
  --runtime PYTHON_3_13

# ---------------------------------------------------------------------------
# Deploy, passing config as runtime environment variables
# ---------------------------------------------------------------------------
# --disable-memory above turns off runtime-managed memory; the agent drives
# AgentCore Memory itself through the session manager, using MEMORY_ID below.
echo "==> agentcore deploy (several minutes)"
agentcore deploy -a "$AGENT_NAME" \
  --env "REGION=${REGION}" \
  --env "MEMORY_ID=${MEMORY_ID}" \
  --env "LONG_TERM_MEMORY_STRATEGY_ID=${LONG_TERM_MEMORY_STRATEGY_ID}" \
  --env "GATEWAY_ID=${GATEWAY_ID}" \
  --env "CREDENTIAL_PROVIDER_NAME=${PROVIDER_NAME}" \
  --env "COGNITO_SCOPE=${SCOPE}"

popd >/dev/null

# ---------------------------------------------------------------------------
# Grant the execution role Memory + Identity access
# ---------------------------------------------------------------------------
AGENT_ROLE_ARN="$(python3 -c "
import yaml
cfg = yaml.safe_load(open('${BUILD_DIR}/.bedrock_agentcore.yaml'))
print(cfg['agents']['${AGENT_NAME}']['aws']['execution_role'])
")"
AGENT_ROLE_NAME="${AGENT_ROLE_ARN##*/}"
echo "==> granting policies to ${AGENT_ROLE_NAME}"

aws iam put-role-policy \
  --role-name "$AGENT_ROLE_NAME" \
  --policy-name AgentCoreMemoryAccess \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Action": [
        "bedrock-agentcore:CreateEvent",
        "bedrock-agentcore:RetrieveMemoryRecords",
        "bedrock-agentcore:ListEvents",
        "bedrock-agentcore:GetMemory",
        "bedrock-agentcore:ListLongTermMemoryRecords",
        "bedrock-agentcore:SearchLongTermMemories"
      ],
      "Resource": "*"
    }]
  }'

aws iam put-role-policy \
  --role-name "$AGENT_ROLE_NAME" \
  --policy-name AgentCoreIdentityAccess \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Action": [
        "bedrock-agentcore:GetResourceOauth2Token",
        "bedrock-agentcore:GetWorkloadAccessToken",
        "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
        "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
        "bedrock-agentcore:GetResourceApiKey",
        "secretsmanager:GetSecretValue"
      ],
      "Resource": "*"
    }]
  }'
echo "    policies attached"

# ---------------------------------------------------------------------------
# Wait for READY and record the ARN
# ---------------------------------------------------------------------------
echo "==> waiting for ${AGENT_NAME} to become READY"
for i in $(seq 1 40); do
  read -r STATUS ARN <<<"$(aws bedrock-agentcore-control list-agent-runtimes --region "$REGION" \
    --query "agentRuntimes[?agentRuntimeName=='${AGENT_NAME}'].[status,agentRuntimeArn] | [0]" \
    --output text 2>/dev/null || echo "NONE NONE")"
  echo "    [${i}] status=${STATUS}"
  case "$STATUS" in
    READY|ACTIVE) break ;;
    CREATE_FAILED|UPDATE_FAILED|DELETING)
      echo "deploy failed (${STATUS}); agentcore status -a ${AGENT_NAME}" >&2; exit 1 ;;
  esac
  sleep 15
done

grep -qE '^(export )?AGENT_RUNTIME_ARN=' .env \
  && sed -i.bak "s|^\(export \)\?AGENT_RUNTIME_ARN=.*|export AGENT_RUNTIME_ARN=\"${ARN}\"|" .env \
  || echo "export AGENT_RUNTIME_ARN=\"${ARN}\"" >> .env
rm -f .env.bak
echo "    AGENT_RUNTIME_ARN=${ARN}"

# ---------------------------------------------------------------------------
# Cross-session memory test
# ---------------------------------------------------------------------------
CLIENT_TOKEN="$(curl -s -X POST \
  "https://${COGNITO_DOMAIN}.auth.${REGION}.amazoncognito.com/oauth2/token" \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -u "${CLIENT_ID}:${CLIENT_SECRET}" \
  -d "grant_type=client_credentials&scope=${SCOPE}" | jq -r .access_token)"

[ -n "$CLIENT_TOKEN" ] && [ "$CLIENT_TOKEN" != "null" ] || {
  echo "could not mint a Cognito token" >&2; exit 1; }

cd "$BUILD_DIR"

echo
echo "==> session 1: state a preference"
agentcore invoke -a "$AGENT_NAME" \
  '{"prompt": "I prefer Python and JSON format reports"}' \
  --headers "Actor-Id:alice-chen" \
  --session-id session_0000000000000000000000001 \
  --bearer-token "$CLIENT_TOKEN" || true

echo
echo "==> session 2 (new session): does it recall the preference?"
agentcore invoke -a "$AGENT_NAME" \
  '{"prompt": "Query the database for employee count and generate a report"}' \
  --headers "Actor-Id:alice-chen" \
  --session-id session_0000000000000000000000002 \
  --bearer-token "$CLIENT_TOKEN" || true

echo
echo "Semantic extraction is asynchronous. If session 2 shows no recall, wait a
minute and re-run the second invoke -- the UserFacts strategy has to consolidate
session 1 before it is retrievable."

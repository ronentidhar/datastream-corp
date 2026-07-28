#!/usr/bin/env bash
# AgentCore Gateway setup -- Phase 2 / Task 2 ("Tool Discovery").
#
# Inbound  (agents -> Gateway):      CUSTOM_JWT, validated against Cognito.
# Outbound (Gateway -> MCP Runtime): OAuth2 client_credentials, same Cognito pool.
#
# Prereqs: Task 1 completed, so MCP_SERVER_ARN is in .env. Run from the repo root.
set -euo pipefail

cd "$(dirname "$0")"
set -a; source .env; set +a

: "${REGION:?REGION missing from .env}"
: "${POOL_ID:?POOL_ID missing from .env}"
: "${CLIENT_ID:?CLIENT_ID missing from .env}"
: "${CLIENT_SECRET:?CLIENT_SECRET missing from .env}"
: "${MCP_SERVER_ARN:?MCP_SERVER_ARN missing from .env -- finish Task 1 first}"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
DISCOVERY_URL="https://cognito-idp.${REGION}.amazonaws.com/${POOL_ID}/.well-known/openid-configuration"

GATEWAY_NAME="${GATEWAY_NAME:-datastream-gateway}"
TARGET_NAME="${TARGET_NAME:-datastream-mcp-target}"
ROLE_NAME="${ROLE_NAME:-DataStreamGatewayRole}"
PROVIDER_NAME="${PROVIDER_NAME:-datastream-cognito-oauth}"
SCOPE="${COGNITO_SCOPE:-datastream/mcp.access}"

echo "account=${ACCOUNT_ID} region=${REGION}"

# ---------------------------------------------------------------------------
# 1. Gateway IAM role
# ---------------------------------------------------------------------------
# The Gateway assumes this role to reach the MCP Runtime and to read the client
# secret that the credential provider stores in Secrets Manager. The SourceAccount
# condition is the confused-deputy guard -- without it any account's Gateway could
# assume this role.
echo "==> IAM role ${ROLE_NAME}"
if ! ROLE_ARN="$(aws iam get-role --role-name "$ROLE_NAME" --query Role.Arn --output text 2>/dev/null)"; then
  ROLE_ARN="$(aws iam create-role \
    --role-name "$ROLE_NAME" \
    --assume-role-policy-document "$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
    "Action": "sts:AssumeRole",
    "Condition": {
      "StringEquals": {"aws:SourceAccount": "${ACCOUNT_ID}"},
      "ArnLike": {"aws:SourceArn": "arn:aws:bedrock-agentcore:${REGION}:${ACCOUNT_ID}:*"}
    }
  }]
}
JSON
)" \
    --query Role.Arn --output text)"

  aws iam put-role-policy \
    --role-name "$ROLE_NAME" \
    --policy-name DataStreamGatewayAccess \
    --policy-document "$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "InvokeMcpRuntime",
      "Effect": "Allow",
      "Action": ["bedrock-agentcore:InvokeAgentRuntime"],
      "Resource": ["${MCP_SERVER_ARN}", "${MCP_SERVER_ARN}/*"]
    },
    {
      "Sid": "ReadOauthClientSecret",
      "Effect": "Allow",
      "Action": ["secretsmanager:GetSecretValue"],
      "Resource": "arn:aws:secretsmanager:${REGION}:${ACCOUNT_ID}:secret:bedrock-agentcore-identity*"
    },
    {
      "Sid": "GatewayTokenVending",
      "Effect": "Allow",
      "Action": ["bedrock-agentcore:GetResourceOauth2Token", "bedrock-agentcore:GetWorkloadAccessToken"],
      "Resource": "*"
    }
  ]
}
JSON
)"
  echo "    created; waiting 10s for IAM propagation"
  sleep 10
fi
echo "    ROLE_ARN=${ROLE_ARN}"

# ---------------------------------------------------------------------------
# 2. OAuth2 credential provider (outbound: Gateway -> MCP Runtime)
# ---------------------------------------------------------------------------
# CognitoOauth2 exists as a vendor, but CustomOauth2 + a discoveryUrl is what the
# task calls for and works against any OIDC issuer.
echo "==> OAuth2 credential provider ${PROVIDER_NAME}"
if ! CREDENTIAL_PROVIDER_ARN="$(aws bedrock-agentcore-control get-oauth2-credential-provider \
      --region "$REGION" --name "$PROVIDER_NAME" \
      --query credentialProviderArn --output text 2>/dev/null)"; then
  CREDENTIAL_PROVIDER_ARN="$(aws bedrock-agentcore-control create-oauth2-credential-provider \
    --region "$REGION" \
    --name "$PROVIDER_NAME" \
    --credential-provider-vendor CustomOauth2 \
    --oauth2-provider-config-input "$(cat <<JSON
{
  "customOauth2ProviderConfig": {
    "oauthDiscovery": {"discoveryUrl": "${DISCOVERY_URL}"},
    "clientId": "${CLIENT_ID}",
    "clientSecret": "${CLIENT_SECRET}",
    "clientAuthenticationMethod": "CLIENT_SECRET_BASIC"
  }
}
JSON
)" \
    --query credentialProviderArn --output text)"
fi
echo "    CREDENTIAL_PROVIDER_ARN=${CREDENTIAL_PROVIDER_ARN}"

# ---------------------------------------------------------------------------
# 3. Gateway with JWT authorizer (inbound: agents -> Gateway)
# ---------------------------------------------------------------------------
# allowedClients pins the Cognito app client, so a token minted for a different
# client is rejected even though it comes from the same pool.
echo "==> Gateway ${GATEWAY_NAME}"
GATEWAY_ID="$(aws bedrock-agentcore-control list-gateways --region "$REGION" \
  --query "items[?name=='${GATEWAY_NAME}'].gatewayId | [0]" --output text 2>/dev/null || echo None)"

if [ "$GATEWAY_ID" = "None" ] || [ -z "$GATEWAY_ID" ]; then
  GATEWAY_JSON="$(aws bedrock-agentcore-control create-gateway \
    --region "$REGION" \
    --name "$GATEWAY_NAME" \
    --role-arn "$ROLE_ARN" \
    --protocol-type MCP \
    --authorizer-type CUSTOM_JWT \
    --authorizer-configuration "$(cat <<JSON
{
  "customJWTAuthorizer": {
    "discoveryUrl": "${DISCOVERY_URL}",
    "allowedClients": ["${CLIENT_ID}"]
  }
}
JSON
)" \
    --output json)"
  GATEWAY_ID="$(jq -r .gatewayId <<<"$GATEWAY_JSON")"
fi

GATEWAY_JSON="$(aws bedrock-agentcore-control get-gateway --region "$REGION" \
  --gateway-identifier "$GATEWAY_ID" --output json)"
GATEWAY_ARN="$(jq -r .gatewayArn <<<"$GATEWAY_JSON")"
GATEWAY_URL="$(jq -r .gatewayUrl <<<"$GATEWAY_JSON")"
echo "    GATEWAY_ID=${GATEWAY_ID}"
echo "    GATEWAY_URL=${GATEWAY_URL}"

# ---------------------------------------------------------------------------
# 4. MCP Runtime as gateway target
# ---------------------------------------------------------------------------
# The endpoint embeds the runtime ARN URL-encoded. If Task 1 printed an explicit
# MCP URL, set MCP_SERVER_URL in .env and it is used verbatim instead.
ENCODED_ARN="$(jq -rn --arg a "$MCP_SERVER_ARN" '$a|@uri')"
MCP_ENDPOINT="${MCP_SERVER_URL:-https://bedrock-agentcore.${REGION}.amazonaws.com/runtimes/${ENCODED_ARN}/invocations?qualifier=DEFAULT}"

echo "==> Gateway target ${TARGET_NAME}"
echo "    endpoint=${MCP_ENDPOINT}"
if ! aws bedrock-agentcore-control list-gateway-targets --region "$REGION" \
      --gateway-identifier "$GATEWAY_ID" \
      --query "items[?name=='${TARGET_NAME}'] | [0]" --output text 2>/dev/null | grep -q .; then
  aws bedrock-agentcore-control create-gateway-target \
    --region "$REGION" \
    --gateway-identifier "$GATEWAY_ID" \
    --name "$TARGET_NAME" \
    --target-configuration "$(cat <<JSON
{
  "mcp": {
    "mcpServer": {
      "endpoint": "${MCP_ENDPOINT}"
    }
  }
}
JSON
)" \
    --credential-provider-configurations "$(cat <<JSON
[{
  "credentialProviderType": "OAUTH",
  "credentialProvider": {
    "oauthCredentialProvider": {
      "providerArn": "${CREDENTIAL_PROVIDER_ARN}",
      "scopes": ["${SCOPE}"],
      "grantType": "CLIENT_CREDENTIALS"
    }
  }
}]
JSON
)" \
    --output json | jq -r '"    targetId=\(.targetId) status=\(.status)"'
  echo "    waiting 15s for target to become READY"
  sleep 15
fi

# ---------------------------------------------------------------------------
# 5. Test tools/list through the Gateway
# ---------------------------------------------------------------------------
echo "==> tools/list via Gateway"
TOKEN="$(curl -s -X POST "https://${COGNITO_DOMAIN}.auth.${REGION}.amazoncognito.com/oauth2/token" \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -u "${CLIENT_ID}:${CLIENT_SECRET}" \
  -d "grant_type=client_credentials&scope=${SCOPE}" | jq -r .access_token)"

curl -s -X POST "$GATEWAY_URL" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' \
  | tee /tmp/tools_list.json \
  | grep -q query_db \
  && echo "    PASS: query_db is discoverable" \
  || { echo "    FAIL: query_db not found -- response:"; cat /tmp/tools_list.json; exit 1; }

# ---------------------------------------------------------------------------
# Persist outputs
# ---------------------------------------------------------------------------
for kv in "GATEWAY_ID=${GATEWAY_ID}" "GATEWAY_ARN=${GATEWAY_ARN}" \
          "GATEWAY_URL=${GATEWAY_URL}" "CREDENTIAL_PROVIDER_ARN=${CREDENTIAL_PROVIDER_ARN}" \
          "GATEWAY_ROLE_ARN=${ROLE_ARN}"; do
  key="${kv%%=*}"
  grep -qE "^export ${key}=" .env && sed -i.bak "s|^export ${key}=.*|export ${kv%%=*}=\"${kv#*=}\"|" .env \
    || echo "export ${key}=\"${kv#*=}\"" >> .env
done
rm -f .env.bak

echo
echo "Done. Point the agent at the Gateway with:"
echo "  export MCP_URL=\"${GATEWAY_URL}\""
echo "  export MCP_BEARER_TOKEN=\$(get_client_token)"

# Cognito client-credentials token helper.
#
# Add this line to ~/.bashrc (short enough to paste safely):
#   source /workshop/datastream-corp/scripts/token.sh
#
# The workshop's original get_client_token ran `source .env`, which only resolved
# when $PWD happened to be the repo root -- calling it from a subdirectory printed
# ".env: No such file or directory" and returned an empty token, which the runtime
# rejects as an authorization-method mismatch. Here .env is resolved relative to
# this file, so the function works from anywhere.

_DATASTREAM_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

get_client_token() {
    local env_file="${_DATASTREAM_ROOT}/.env"
    if [ ! -f "$env_file" ]; then
        echo "get_client_token: no .env at ${env_file}" >&2
        return 1
    fi

    # Subshell: keeps .env out of the caller's environment.
    (
        set -a
        # shellcheck disable=SC1090
        source "$env_file"
        set +a

        : "${CLIENT_ID:?not set in .env}"
        : "${CLIENT_SECRET:?not set in .env}"
        : "${COGNITO_DOMAIN:?not set in .env}"
        : "${REGION:?not set in .env}"

        local scope="${COGNITO_SCOPE:-datastream/mcp.access}"
        local auth_string
        auth_string="$(printf '%s:%s' "$CLIENT_ID" "$CLIENT_SECRET" | base64 | tr -d '\n')"

        local token
        token="$(curl -sS -X POST \
            "https://${COGNITO_DOMAIN}.auth.${REGION}.amazoncognito.com/oauth2/token" \
            -H 'Content-Type: application/x-www-form-urlencoded' \
            -H "Authorization: Basic ${auth_string}" \
            -d "grant_type=client_credentials&scope=${scope}" \
            | jq -r '.access_token')"

        if [ -z "$token" ] || [ "$token" = "null" ]; then
            echo "get_client_token: Cognito returned no access_token" >&2
            return 1
        fi
        printf '%s\n' "$token"
    )
}

# Fresh session id for `agentcore invoke`. AgentCore requires >= 33 characters, and
# a session id stays bound to the agent version that first used it -- reusing one
# after a redeploy silently routes to the old code.
new_session_id() {
    printf 'session_%s\n' "$(openssl rand -hex 13)"
}

# ask_http "<prompt>" [actor-id]
# Same invoke over plain HTTPS: needs only a token, no agentcore CLI and no
# .bedrock_agentcore.yaml, so it works from any directory or another machine.
ask_http() {
    local prompt="$1" actor="${2:-alice-chen}" token
    token="$(get_client_token)" || return 1
    (
        set -a
        # shellcheck disable=SC1091
        source "${_DATASTREAM_ROOT}/.env"
        set +a
        : "${AGENT_RUNTIME_ARN:?not in .env -- run ./deploy_agent.sh first}"

        local enc
        enc="$(jq -rn --arg a "$AGENT_RUNTIME_ARN" '$a|@uri')"

        curl -sS -X POST \
            "https://bedrock-agentcore.${REGION}.amazonaws.com/runtimes/${enc}/invocations?qualifier=DEFAULT" \
            -H "Authorization: Bearer ${token}" \
            -H 'Content-Type: application/json' \
            -H "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: $(new_session_id)" \
            -H "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Actor-Id: ${actor}" \
            -d "{\"prompt\": $(jq -Rn --arg p "$prompt" '$p')}" \
            | jq -r 'if .response.content then .response.content[0].text else . end'
    )
}

# ask "<prompt>" [actor-id]
ask() {
    local prompt="$1" actor="${2:-alice-chen}" token
    token="$(get_client_token)" || return 1
    (
        cd "${_DATASTREAM_ROOT}/agent-runtime" || return 1
        agentcore invoke -a "${AGENT_NAME:-datastream_agent}" \
            "{\"prompt\": $(jq -Rn --arg p "$prompt" '$p')}" \
            --headers "Actor-Id:${actor}" \
            --session-id "$(new_session_id)" \
            --bearer-token "$token"
    )
}

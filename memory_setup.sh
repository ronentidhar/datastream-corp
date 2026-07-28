#!/usr/bin/env bash
# Phase 2 / Task 3 -- create the AgentCore Memory resource with a semantic
# extraction strategy, then record MEMORY_ID and LONG_TERM_MEMORY_STRATEGY_ID
# in .env for Task 4 (agent deployment + memory integration).
#
# Run from the repo root. Idempotent: re-running finds the existing memory by
# name instead of creating a second one.
set -euo pipefail

cd "$(dirname "$0")"
set -a; source .env; set +a

: "${REGION:?REGION missing from .env}"

MEMORY_NAME="${MEMORY_NAME:-DataStreamCorpMemory}"
STRATEGY_NAME="${STRATEGY_NAME:-UserFacts}"
EXPIRY_DAYS="${EXPIRY_DAYS:-90}"

echo "==> identity"
aws sts get-caller-identity --query '[Account,Arn]' --output text

# ---------------------------------------------------------------------------
# Find an existing memory with this name
# ---------------------------------------------------------------------------
# ListMemories returns only id/arn/status -- no name -- so resolve names via
# GetMemory on each id.
find_memory_id() {
  local id
  for id in $(aws bedrock-agentcore-control list-memories --region "$REGION" \
                --query 'memories[].id' --output text 2>/dev/null); do
    if [ "$(aws bedrock-agentcore-control get-memory --memory-id "$id" --region "$REGION" \
              --query 'memory.name' --output text 2>/dev/null)" = "$MEMORY_NAME" ]; then
      echo "$id"; return 0
    fi
  done
  return 1
}

if MEMORY_ID="$(find_memory_id)"; then
  echo "==> reusing existing memory ${MEMORY_NAME} (${MEMORY_ID})"
else
  echo "==> creating memory ${MEMORY_NAME}"
  agentcore memory create "$MEMORY_NAME" \
    --region "$REGION" \
    --description "Enterprise memory for DataStream Corp agents" \
    --event-expiry-days "$EXPIRY_DAYS" \
    --strategies "[{\"semanticMemoryStrategy\": {\"name\": \"${STRATEGY_NAME}\"}}]" \
    --wait

  MEMORY_ID="$(find_memory_id)" || {
    echo "created the memory but could not resolve its id by name" >&2
    aws bedrock-agentcore-control list-memories --region "$REGION" --output json >&2
    exit 1
  }
fi
echo "    MEMORY_ID=${MEMORY_ID}"

# ---------------------------------------------------------------------------
# Ensure the semantic strategy exists
# ---------------------------------------------------------------------------
# `agentcore memory create <name>` without --strategies produces an STM-only
# memory with strategies: []. Rather than recreate, add the strategy in place.
HAVE_STRATEGY="$(aws bedrock-agentcore-control get-memory \
  --memory-id "$MEMORY_ID" --region "$REGION" \
  --query "memory.strategies[?name=='${STRATEGY_NAME}'].strategyId | [0]" \
  --output text 2>/dev/null || echo None)"

if [ "$HAVE_STRATEGY" = "None" ] || [ -z "$HAVE_STRATEGY" ]; then
  echo "==> no ${STRATEGY_NAME} strategy on this memory; adding it"
  aws bedrock-agentcore-control update-memory \
    --memory-id "$MEMORY_ID" \
    --region "$REGION" \
    --description "Enterprise memory for DataStream Corp agents" \
    --memory-strategies "{\"addMemoryStrategies\":[{\"semanticMemoryStrategy\":{\"name\":\"${STRATEGY_NAME}\"}}]}" \
    --query 'memory.{status:status}' --output text
else
  echo "    strategy ${STRATEGY_NAME} already present (${HAVE_STRATEGY})"
fi

# ---------------------------------------------------------------------------
# Wait for the memory AND its strategy to go ACTIVE
# ---------------------------------------------------------------------------
# --wait covers the memory itself; the semantic strategy provisions its index
# separately and can still be CREATING afterwards.
echo "==> waiting for memory + strategy to become ACTIVE"
for i in $(seq 1 40); do
  read -r MSTATUS SSTATUS <<<"$(aws bedrock-agentcore-control get-memory \
    --memory-id "$MEMORY_ID" --region "$REGION" \
    --query "[memory.status, memory.strategies[?name=='${STRATEGY_NAME}'].status | [0]]" \
    --output text 2>/dev/null || echo "NONE NONE")"
  echo "    [${i}] memory=${MSTATUS} strategy=${SSTATUS}"
  [ "$MSTATUS" = "ACTIVE" ] && [ "$SSTATUS" = "ACTIVE" ] && break
  if [ "$MSTATUS" = "FAILED" ] || [ "$SSTATUS" = "FAILED" ]; then
    aws bedrock-agentcore-control get-memory --memory-id "$MEMORY_ID" --region "$REGION" \
      --query 'memory.{status:status,reason:failureReason,strategies:strategies[].{name:name,status:status}}'
    echo "memory provisioning failed" >&2; exit 1
  fi
  sleep 15
done

[ "${MSTATUS:-}" = "ACTIVE" ] || { echo "timed out waiting for ACTIVE" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Strategy id
# ---------------------------------------------------------------------------
LONG_TERM_MEMORY_STRATEGY_ID="$(aws bedrock-agentcore-control get-memory \
  --memory-id "$MEMORY_ID" --region "$REGION" \
  --query "memory.strategies[?name=='${STRATEGY_NAME}'].strategyId | [0]" --output text)"

[ -n "$LONG_TERM_MEMORY_STRATEGY_ID" ] && [ "$LONG_TERM_MEMORY_STRATEGY_ID" != "None" ] || {
  echo "could not resolve strategyId for ${STRATEGY_NAME}" >&2; exit 1
}
echo "    LONG_TERM_MEMORY_STRATEGY_ID=${LONG_TERM_MEMORY_STRATEGY_ID}"

# ---------------------------------------------------------------------------
# Persist
# ---------------------------------------------------------------------------
for kv in "MEMORY_ID=${MEMORY_ID}" \
          "LONG_TERM_MEMORY_STRATEGY_ID=${LONG_TERM_MEMORY_STRATEGY_ID}"; do
  key="${kv%%=*}"
  grep -qE "^(export )?${key}=" .env \
    && sed -i.bak "s|^\(export \)\?${key}=.*|export ${key}=\"${kv#*=}\"|" .env \
    || echo "export ${key}=\"${kv#*=}\"" >> .env
done
rm -f .env.bak

echo
echo "==> agentcore memory status"
agentcore memory status "$MEMORY_ID" || true

echo
echo "Recorded in .env:"
grep -E '^export (MEMORY_ID|LONG_TERM_MEMORY_STRATEGY_ID)=' .env

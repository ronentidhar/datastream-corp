"""Unattended monitor built on the Claude Agent SDK.

One invocation == one task. Everything that can be configuration lives on disk
under .claude/ and .mcp.json; this file only wires up the parts that must be
code (the Python hook, the output schema, the audit sink).

Verified against claude-agent-sdk 0.2.128 / bundled CLI.

Reads datastream_corp.db through mcp_server.py -- both byte-identical copies of
the Strands variants' files, vendored here so a workshop edit next door cannot
change this service's behaviour. Read-only: the PreToolUse hook rejects any
statement that is not a plain SELECT.

Run:
    .venv/bin/python service.py "Check for overdue projects."
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, ValidationError

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
    CLINotFoundError,
    HookMatcher,
    ProcessError,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    query,
)

PROJECT_DIR = Path(__file__).parent.resolve()
LOG_DIR = PROJECT_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    # DEBUG additionally surfaces the CLI subprocess's own stderr.
    level=os.environ.get("MONITOR_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("monitor")

# Every tool call the model makes is appended here as one JSON object per line,
# so a run can be reconstructed after the fact. The shell hook in
# .claude/settings.json writes its own parallel log (logs/audit-shell.jsonl) --
# keeping both is how you can see that filesystem hooks and Python hooks are
# genuinely independent and both fire.
AUDIT_LOG = LOG_DIR / "audit.jsonl"


def audit(event: str, **fields: Any) -> None:
    """Append one structured audit record. Never raises."""
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **fields,
    }
    try:
        with AUDIT_LOG.open("a") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except OSError:
        log.exception("failed to write audit record")


# ---------------------------------------------------------------------------
# 1. Structured result
#
# SDK-native: ClaudeAgentOptions.output_format takes a JSON Schema and the SDK
# handles instructing the model, validating, and retrying on mismatch. The
# validated object arrives on ResultMessage.structured_output. We re-validate
# through Pydantic at the boundary only to get a typed Python object back --
# that is a convenience, not a second implementation of the retry loop.
# ---------------------------------------------------------------------------
class IncidentReport(BaseModel):
    severity: Literal["critical", "warning", "info"]
    incident_found: bool
    summary: str = Field(description="Two sentences at most, plain English.")
    affected_ids: list[str] = Field(
        default_factory=list,
        description="Concrete identifiers behind the finding -- project ids/names, "
        "department names.",
    )
    recommended_action: str = Field(
        description="Proposed remediation. Proposed only -- never executed by this agent."
    )
    requires_human_signoff: bool = Field(
        description="True if the recommended action mutates production state."
    )


# ---------------------------------------------------------------------------
# 2. The code-enforced rule
#
# A PreToolUse callback hook. Hooks run *before* permission rules and cannot be
# bypassed by permission_mode, which is exactly why the "never write to prod"
# rule lives here rather than only in a deny list. A deny rule is configuration
# someone can edit; this is code, and it also produces the audit trail.
#
# The signature is (input_data, tool_use_id, context) and blocking is done by
# returning permissionDecision "deny" inside hookSpecificOutput.
#
# Note the shape of the problem here. The shared mcp_server.py exposes ONE tool,
# query_db, that runs reads and writes alike ("Supports reads (SELECT) and
# writes (INSERT, UPDATE, DELETE, DROP)"). So the rule cannot be expressed as a
# tool-name allow/deny list at all -- not in settings.json, not in
# disallowed_tools. It has to inspect the argument. That makes this hook the
# only enforcement point, and it is the direct counterpart to the Strands
# BeforeToolCallEvent hook + is_destructive() check in aws-Strands/agent.py.
# ---------------------------------------------------------------------------
SQL_TOOLS = {"mcp__datastream__query_db"}

# The weather subagent reaches the public internet through WebFetch. WebFetch is
# one tool over every host on the web, so -- exactly like query_db above -- the
# rule cannot be written as a tool-name allow/deny and has to inspect the
# argument. settings.json carries matching WebFetch(domain:...) entries, but
# those only govern *prompting*; this is the part that actually blocks.
WEB_TOOLS = {"WebFetch"}
ALLOWED_WEB_HOSTS = {"geocoding-api.open-meteo.com", "api.open-meteo.com"}

# Same keyword set the Strands variant uses, kept identical on purpose so the
# two implementations are comparable.
DESTRUCTIVE_KEYWORDS = {
    "drop", "delete", "update", "insert", "alter",
    "truncate", "replace", "create", "attach", "detach", "vacuum",
}

# Human sign-off is an out-of-band environment variable, deliberately awkward to
# set: an unattended cron run simply will not have it.
SIGNOFF_TOKEN = os.environ.get("OPS_WRITE_SIGNOFF")


def is_destructive(sql: str) -> bool:
    """True if the statement is anything other than a plain read.

    Comments are stripped first so `SELECT 1 -- DROP TABLE x` is not a false
    positive and `/* */DROP` is not a false negative. Fails closed: anything
    unparseable counts as destructive.
    """
    cleaned = re.sub(r"--[^\n]*", " ", sql)
    cleaned = re.sub(r"/\*.*?\*/", " ", cleaned, flags=re.S)
    tokens = set(re.findall(r"[a-zA-Z_]+", cleaned.lower()))
    return bool(tokens & DESTRUCTIVE_KEYWORDS)


async def block_production_writes(
    input_data: dict[str, Any],
    tool_use_id: str | None,
    context: Any,
) -> dict[str, Any]:
    tool_name = input_data.get("tool_name", "")
    tool_input = input_data.get("tool_input", {})

    if tool_name not in SQL_TOOLS:
        return {}  # empty dict == "no opinion", let the normal rules decide

    sql = str(tool_input.get("sql", ""))
    if not is_destructive(sql):
        return {}  # a plain read -- nothing to say

    if SIGNOFF_TOKEN:
        audit(
            "write_allowed_with_signoff",
            tool=tool_name,
            tool_use_id=tool_use_id,
            tool_input=tool_input,
        )
        return {}

    reason = (
        "Blocked by policy: this agent may not write to the DataStream Corp "
        "database without human sign-off, and this statement is not a plain "
        "read. Report the required change in recommended_action instead and "
        "set requires_human_signoff=true."
    )
    audit(
        "write_blocked",
        tool=tool_name,
        tool_use_id=tool_use_id,
        tool_input=tool_input,
        reason=reason,
    )
    log.warning("BLOCKED %s: %s", tool_name, json.dumps(tool_input, default=str))

    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


async def block_unapproved_hosts(
    input_data: dict[str, Any],
    tool_use_id: str | None,
    context: Any,
) -> dict[str, Any]:
    """Confine WebFetch to the Open-Meteo hosts the weather subagent needs.

    Fails closed: an unparseable URL, or one with no hostname, is denied.
    """
    tool_name = input_data.get("tool_name", "")
    tool_input = input_data.get("tool_input", {})

    if tool_name not in WEB_TOOLS:
        return {}

    url = str(tool_input.get("url", ""))
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        host = ""

    if host in ALLOWED_WEB_HOSTS:
        return {}

    reason = (
        f"Blocked by policy: this agent may only fetch "
        f"{', '.join(sorted(ALLOWED_WEB_HOSTS))}. Refused {host or url!r}."
    )
    audit(
        "fetch_blocked",
        tool=tool_name,
        tool_use_id=tool_use_id,
        tool_input=tool_input,
        reason=reason,
    )
    log.warning("BLOCKED %s: %s", tool_name, json.dumps(tool_input, default=str))

    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


# ---------------------------------------------------------------------------
# 3. Provider routing -- configuration, not code
#
# The SDK spawns the Claude Code CLI as a subprocess; anything in options.env is
# forwarded to it. Selecting a provider is therefore purely a matter of which
# env vars we forward. Set MONITOR_PROVIDER=anthropic|bedrock|vertex.
# ---------------------------------------------------------------------------
def provider_env() -> dict[str, str]:
    provider = os.environ.get("MONITOR_PROVIDER", "anthropic").lower()

    def passthrough(*names: str) -> dict[str, str]:
        return {n: os.environ[n] for n in names if os.environ.get(n)}

    if provider == "bedrock":
        return {
            "CLAUDE_CODE_USE_BEDROCK": "1",
            **passthrough(
                "AWS_REGION",
                "AWS_PROFILE",
                "AWS_ACCESS_KEY_ID",
                "AWS_SECRET_ACCESS_KEY",
                "AWS_SESSION_TOKEN",
                "ANTHROPIC_MODEL",
            ),
        }
    if provider == "vertex":
        return {
            "CLAUDE_CODE_USE_VERTEX": "1",
            **passthrough(
                "CLOUD_ML_REGION",
                "ANTHROPIC_VERTEX_PROJECT_ID",
                "GOOGLE_APPLICATION_CREDENTIALS",
                "ANTHROPIC_MODEL",
            ),
        }
    if provider == "anthropic":
        # ANTHROPIC_API_KEY is inherited from the ambient environment; it is
        # listed here so a missing key fails loudly rather than silently
        # falling back to interactive credentials.
        if not os.environ.get("ANTHROPIC_API_KEY"):
            log.warning(
                "ANTHROPIC_API_KEY is not set; falling back to CLI credentials"
            )
        return passthrough("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL")

    raise ValueError(f"unknown MONITOR_PROVIDER {provider!r}")


# ---------------------------------------------------------------------------
# 4. Options
# ---------------------------------------------------------------------------
def build_options(structured: bool = True) -> ClaudeAgentOptions:
    """Build the run options.

    structured=False drops the IncidentReport schema so the agent answers in
    plain prose. Used for ad-hoc questions that are not incident reports -- see
    the --freeform flag. Every guard rail stays in force either way.
    """
    return ClaudeAgentOptions(
        # cwd anchors every filesystem lookup: CLAUDE.md, .claude/settings.json,
        # .claude/skills/, .claude/agents/, .mcp.json are all resolved from here.
        cwd=str(PROJECT_DIR),
        # Hermetic: this directory and nothing else. The 0.2.x default is None,
        # which loads user + project + local -- so an unattended service would
        # silently inherit whatever is in the operator's ~/.claude (global MCP
        # servers, personal CLAUDE.md, skills) and differ between machines.
        # "project" is what pulls in CLAUDE.md; drop it and the domain rules go
        # too. "local" is dropped so an untracked settings.local.json cannot
        # change production behaviour.
        setting_sources=["project"],
        # setting_sources does NOT govern .mcp.json -- the CLI loads it, plus
        # user/global and plugin-provided servers, regardless. strict_mcp_config
        # is the switch that stops that, and it is all-or-nothing: it ignores the
        # project .mcp.json as well, so the file has to be named explicitly here.
        # Result: the agent sees exactly the servers in this repo's .mcp.json.
        mcp_servers=str(PROJECT_DIR / ".mcp.json"),
        strict_mcp_config=True,
        # Without this the run gets a bare system prompt -- Claude Code's own
        # preset (tool guidance, CLAUDE.md handling) is NOT the default in the
        # SDK. CLAUDE.md is appended on top of whatever we choose here.
        system_prompt={"type": "preset", "preset": "claude_code"},
        # Enable filesystem-discovered skills. Setting this also adds "Skill"
        # to the effective allowed_tools, so we do not list it below.
        skills=["data-integrity-triage"],
        # Pre-approval allow-list. Combined with permission_mode="dontAsk",
        # anything not on this list is denied outright instead of prompting --
        # which is what makes the run safe to leave unattended.
        #
        # query_db is read AND write in one tool, so listing it here pre-approves
        # writes too. There is no tool-name-shaped way to say "reads only"; the
        # hook is what draws that line.
        #
        # WebFetch is the weather subagent's only tool. Same shape of problem as
        # query_db -- one tool name covering every host -- so the host allow-list
        # is enforced by block_unapproved_hosts, not by this list.
        allowed_tools=[
            "mcp__datastream__query_db",
            "mcp__datastream__list_schema",
            "WebFetch",
            "Task",  # lets the model delegate to the db-analyst / weather subagents
        ],
        # Bash is removed so the agent cannot reach the .db file around the MCP
        # server. Write/Edit keep it off the filesystem generally.
        disallowed_tools=["Bash", "Write", "Edit"],
        permission_mode="dontAsk",
        hooks={
            "PreToolUse": [
                # matcher is a regex over the tool name; None would match all.
                HookMatcher(
                    matcher="mcp__datastream__.*", hooks=[block_production_writes]
                ),
                HookMatcher(matcher="WebFetch", hooks=[block_unapproved_hosts]),
            ]
        },
        output_format=(
            {"type": "json_schema", "schema": IncidentReport.model_json_schema()}
            if structured
            else None
        ),
        # Unset means "whatever the CLI is configured to use". Pin it for a
        # service so a config change elsewhere can't silently move your costs.
        model=os.environ.get("MONITOR_MODEL") or None,
        # SDK-native guard rails instead of a hand-rolled runaway-loop check.
        max_turns=20,
        max_budget_usd=float(os.environ.get("MONITOR_MAX_USD", "1.00")),
        env=provider_env(),
        # Surface CLI subprocess stderr into our own logs rather than /dev/null.
        stderr=lambda line: log.debug("cli: %s", line.rstrip()),
    )


# ---------------------------------------------------------------------------
# 5. One task per invocation
# ---------------------------------------------------------------------------
class TaskOutcome(BaseModel):
    ok: bool
    session_id: str | None = None
    cost_usd: float | None = None
    num_turns: int | None = None
    report: IncidentReport | None = None
    # Populated instead of `report` on a --freeform run.
    answer: str | None = None
    error: str | None = None


async def run_task(prompt: str, structured: bool = True) -> TaskOutcome:
    """Run a single monitoring task and return a validated result.

    Uses query() rather than ClaudeSDKClient: this is a one-shot, stateless
    task, and query() supports hooks, MCP servers and structured output just as
    ClaudeSDKClient does. See README for when to switch.

    No retry loop here on purpose -- the SDK already retries transient API
    errors internally, and re-running a whole monitoring pass on failure is a
    scheduler concern, not an in-process one.
    """
    audit("task_start", prompt=prompt, structured=structured)

    try:
        options = build_options(structured=structured)
    except ValueError as exc:
        audit("task_config_error", error=str(exc))
        return TaskOutcome(ok=False, error=str(exc))

    result: ResultMessage | None = None

    try:
        async for message in query(prompt=prompt, options=options):
            match message:
                case SystemMessage() if message.subtype == "init":
                    data = message.data
                    log.info(
                        "session %s | model=%s | tools=%d | mcp=%s",
                        data.get("session_id"),
                        data.get("model"),
                        len(data.get("tools", [])),
                        [s.get("name") for s in data.get("mcp_servers", [])],
                    )
                    audit(
                        "session_init",
                        session_id=data.get("session_id"),
                        model=data.get("model"),
                        slash_commands=data.get("slash_commands"),
                        agents=data.get("agents"),
                        mcp_servers=data.get("mcp_servers"),
                    )

                case AssistantMessage():
                    for block in message.content:
                        if isinstance(block, ToolUseBlock):
                            log.info("tool | %s %s", block.name, block.input)
                            audit(
                                "tool_use",
                                tool=block.name,
                                tool_use_id=block.id,
                                tool_input=block.input,
                            )
                        elif isinstance(block, TextBlock) and block.text.strip():
                            # The model's own narration. Logged in full at INFO
                            # so a run reads like a transcript rather than a
                            # list of opaque tool calls -- this is the bit that
                            # tells you *why* it ran the query it ran.
                            log.info("say  | %s", block.text.strip())

                case ResultMessage():
                    result = message

    # The SDK owns the agent loop; these are the failure modes it hands back.
    except CLINotFoundError as exc:
        audit("task_error", kind="cli_not_found", error=str(exc))
        return TaskOutcome(ok=False, error=f"Claude Code CLI not found: {exc}")
    except ProcessError as exc:
        audit("task_error", kind="process", exit_code=exc.exit_code, error=str(exc))
        return TaskOutcome(ok=False, error=f"CLI process failed: {exc}")
    except ClaudeSDKError as exc:
        audit("task_error", kind="sdk", error=str(exc))
        return TaskOutcome(ok=False, error=f"SDK error: {exc}")
    except Exception as exc:  # last-resort net: the service must not die
        log.exception("unexpected failure")
        audit("task_error", kind="unexpected", error=str(exc))
        return TaskOutcome(ok=False, error=f"Unexpected error: {exc}")

    if result is None:
        audit("task_error", kind="no_result")
        return TaskOutcome(ok=False, error="run ended without a ResultMessage")

    base = TaskOutcome(
        ok=not result.is_error,
        session_id=result.session_id,
        cost_usd=result.total_cost_usd,
        num_turns=result.num_turns,
    )
    audit(
        "task_end",
        subtype=result.subtype,
        is_error=result.is_error,
        terminal_reason=result.terminal_reason,
        session_id=result.session_id,
        cost_usd=result.total_cost_usd,
        num_turns=result.num_turns,
        usage=result.usage,
        model_usage=result.model_usage,
        permission_denials=result.permission_denials,
    )

    if result.is_error:
        base.error = result.result or f"run failed ({result.subtype})"
        return base

    if not structured:
        base.answer = result.result
        return base

    if result.structured_output is None:
        base.ok = False
        base.error = f"no structured output (subtype={result.subtype})"
        return base

    try:
        base.report = IncidentReport.model_validate(result.structured_output)
    except ValidationError as exc:
        base.ok = False
        base.error = f"structured output failed validation: {exc}"
        audit("schema_validation_failed", error=str(exc), raw=result.structured_output)

    return base


DEFAULT_PROMPT = (
    "Run the data-integrity monitor. Check the database for overdue projects, "
    "orphaned project assignments, and payroll figures that do not reconcile "
    "against department budgets, then report what you found."
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Unattended operations monitor.")
    parser.add_argument("prompt", nargs="?", default=DEFAULT_PROMPT)
    parser.add_argument(
        "--freeform",
        action="store_true",
        help="Answer in prose instead of forcing the IncidentReport schema. "
        "For ad-hoc questions that are not incident reports.",
    )
    args = parser.parse_args()

    outcome = asyncio.run(run_task(args.prompt, structured=not args.freeform))

    # stdout is the machine-readable channel; all logging goes to stderr.
    print(outcome.model_dump_json(indent=2))

    if outcome.ok:
        log.info("done | cost=$%.4f | turns=%s", outcome.cost_usd or 0.0, outcome.num_turns)
        return 0
    log.error("failed: %s", outcome.error)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

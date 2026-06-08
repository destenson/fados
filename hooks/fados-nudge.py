#!/usr/bin/env python3
"""PreToolUse nudge toward the fados skill.

Fires on search-shaped tool calls (Grep/Glob, or Bash running
grep/rg/find/fd/ack/ag) and, only when a fados index is actually
discoverable by walking up from CWD git-style, injects a reminder to
prefer fados. The walk-up guard mirrors fados's own index discovery, so
the nudge never appears where fados couldn't serve the query.

Reads the PreToolUse payload on stdin, emits a hookSpecificOutput JSON
object on stdout when it wants to inject context, and is otherwise
silent. Never fails loudly: any error exits 0 with no output so a broken
nudge can't block a tool call.
"""

import json
import os
import re
import sys

# The plugin bundles the fados script under scripts/. CLAUDE_PLUGIN_ROOT is
# injected by Claude Code when the hook runs from an installed plugin; the
# dirname fallback keeps the hook runnable standalone (e.g. direct testing).
_PLUGIN_ROOT = os.environ.get(
    "CLAUDE_PLUGIN_ROOT",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)
FADOS_SCRIPT = os.path.join(_PLUGIN_ROOT, "scripts", "fados.py")

# Bash commands that mean "the model is searching the tree the slow way".
# Word-boundary match keeps 'find'/'ag' from tripping on substrings.
SEARCH_CMD_RE = re.compile(r"\b(grep|rg|ripgrep|find|fd|ack|ag)\b")

NUDGE = (
    "fados: a .fados index is discoverable from CWD (walk-up). Prefer it over "
    "Grep/Glob/Bash search. Conceptual search: "
    f"`uv run {FADOS_SCRIPT} semantic \"<query>\"`. Keyword (bm25): "
    f"`uv run {FADOS_SCRIPT} search \"<terms>\"`. Intent-scoped code search: "
    f"`uv run {FADOS_SCRIPT} definition|implementation|documentation|tests <term>`. "
    "Stdout is NDJSON (jq-safe)."
)


def _index_discoverable(start: str) -> bool:
    """True if any ancestor of `start` (inclusive) holds .fados/index.db."""
    cur = os.path.abspath(start)
    while True:
        if os.path.isfile(os.path.join(cur, ".fados", "index.db")):
            return True
        parent = os.path.dirname(cur)
        if parent == cur:
            return False
        cur = parent


def _is_search_call(payload: dict) -> bool:
    tool = payload.get("tool_name", "")
    if tool in ("Grep", "Glob"):
        return True
    if tool == "Bash":
        cmd = payload.get("tool_input", {}).get("command", "")
        return bool(SEARCH_CMD_RE.search(cmd))
    # Some harness versions don't pass tool_name to a matcher-scoped hook;
    # fall back to sniffing the command if one is present.
    cmd = payload.get("tool_input", {}).get("command", "")
    return bool(cmd) and bool(SEARCH_CMD_RE.search(cmd))


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    if not _is_search_call(payload):
        return
    if not _index_discoverable(os.getcwd()):
        return
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": NUDGE,
        }
    }))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Never let a nudge failure block the tool call.
        pass

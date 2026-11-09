#!/usr/bin/env python3
"""Claude Code PreToolUse hook used when the agent's other tools are enabled.

Claude Code allows some calls without asking (commands it classifies as
read-only, reads inside the working directory…). The FreeCAD Addon Index
rules for AI agents require that the user sees and approves every command
and every file access at a path the model chooses, so this hook answers
"ask" for every tool call, which routes it to the FreeCAD approval dialog
(--permission-prompt-tool mcp__freecad__approve_tool).

Exceptions, answered with no decision (the CLI's normal rules then apply):
  * the addon's own FreeCAD tools (exec_python is approved inside FreeCAD);
  * TodoWrite and ToolSearch, which only update the agent's task list or
    load tool definitions, without touching files, commands or the network;
  * Read of a file inside the attachments directory given as argv[1], which
    the user attached and which the CLI allowlist already permits.

Any unexpected input fails closed ("ask"). Dependencies: stdlib only.
"""

import json
import os
import sys

NO_SIDE_EFFECT_TOOLS = {"TodoWrite", "ToolSearch"}


def _inside(path, directory):
    try:
        path = os.path.realpath(path)
        directory = os.path.realpath(directory)
        return os.path.commonpath([path, directory]) == directory
    except (TypeError, ValueError):
        return False


def decision(event, attachments_dir=""):
    """Return "ask", or None to leave the call to the CLI's own rules."""
    if not isinstance(event, dict):
        return "ask"
    name = event.get("tool_name")
    if not isinstance(name, str):
        return "ask"
    if name.startswith("mcp__freecad__") or name in NO_SIDE_EFFECT_TOOLS:
        return None
    tool_input = event.get("tool_input")
    if (name == "Read" and attachments_dir and isinstance(tool_input, dict)
            and isinstance(tool_input.get("file_path"), str)
            and _inside(tool_input["file_path"], attachments_dir)):
        return None
    return "ask"


def main():
    attachments_dir = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        event = json.load(sys.stdin)
    except (ValueError, RecursionError):
        event = None
    if decision(event, attachments_dir) == "ask":
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": "Every agent tool call is approved in FreeCAD.",
            }
        }))


if __name__ == "__main__":
    main()

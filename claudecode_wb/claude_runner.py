"""Launches the `claude` CLI (Claude Code, authenticated via the user's
subscription) in headless mode and streams stream-json events.

One conversation turn = one `claude -p` process; continuity is provided by
--resume <session_id>. The FreeCAD tools are supplied via --mcp-config
(mcp_bridge.py). The MCP tools are pre-authorized at the CLI level because
the approval step happens inside FreeCAD: rpc_server.py shows every
exec_python run to the user before executing it.
"""

import json
import os
import shutil
import sys

from PySide import QtCore

from .runner_base import (
    ATTACHMENTS_SUBDIR,
    SYSTEM_PROMPT,
    AgentRunner,
    bridge_command,
    format_prompt_with_attachments,
)

__all__ = ["ClaudeRunner", "SYSTEM_PROMPT", "find_claude"]

IS_WINDOWS = sys.platform == "win32"

FREECAD_TOOLS = "mcp__freecad__exec_python,mcp__freecad__get_scene,mcp__freecad__screenshot"

# Give the user time to review code in the approval dialog before the CLI
# gives up on the MCP tool call (milliseconds).
MCP_TOOL_TIMEOUT_MS = str(60 * 60 * 1000)


def find_claude():
    """Find the claude binary, including outside FreeCAD's restricted PATH."""
    names = ("claude.exe",) if IS_WINDOWS else ("claude",)
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    if IS_WINDOWS:
        candidates = (
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "claude", "claude.exe"),
            os.path.expanduser(r"~\.local\bin\claude.exe"),
        )
    else:
        candidates = (
            os.path.expanduser("~/.local/bin/claude"),
            os.path.expanduser("~/.claude/local/claude"),
            "/usr/local/bin/claude",
        )
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def _claude_launch_command(claude_path):
    """Return (program, prepend_args) usable with QProcess.

    Shell shims cannot safely preserve arbitrary arguments and permission
    flags through cmd.exe. Require the native Windows executable instead.
    """
    if IS_WINDOWS and claude_path.lower().endswith((".cmd", ".bat")):
        raise ValueError("Windows requires the native claude.exe; .cmd/.bat shims are not supported.")
    return claude_path, []


class ClaudeRunner(AgentRunner):
    """Un tour de conversation Claude Code dans un QProcess."""

    display_name = "claude"
    workdir_name = "freecad_claude_agent"

    def __init__(self, rpc_port=0, rpc_token="", parent=None):
        super().__init__(rpc_port, rpc_token, parent=parent)
        self._mcp_config_path = os.path.join(self._workdir, "mcp-config.json")

    def send_prompt(self, prompt, model="", skip_permissions=False, attachments=None):
        if self.running:
            self.error.emit("A turn is already running.")
            return False
        if not self.rpc_port or not self.rpc_token:
            self.error.emit("FreeCAD RPC server is not running.")
            return False
        claude = find_claude()
        if not claude:
            self.error.emit(
                "`claude` CLI not found (Windows requires native claude.exe). Install Claude Code and log in "
                "(run `claude` then /login) with your subscription."
            )
            return False
        self._write_mcp_config()
        attachments = list(attachments or [])

        program, prepend = _claude_launch_command(claude)
        args = prepend + [
            "-p",
            "--verbose",
            "--output-format", "stream-json",
            "--mcp-config", self._mcp_config_path,
            "--strict-mcp-config",
            "--setting-sources=",
            "--append-system-prompt", SYSTEM_PROMPT,
        ]
        self.unrestricted = bool(skip_permissions)
        if skip_permissions:
            args += ["--tools", "default", "--dangerously-skip-permissions"]
        else:
            args += ["--tools", "Read" if attachments else "", "--permission-mode", "default"]
            allowed = [FREECAD_TOOLS]
            if attachments:
                # Let the agent read the attached files, and nothing else.
                allowed.append("Read(./%s/**)" % ATTACHMENTS_SUBDIR)
                if not IS_WINDOWS:
                    allowed.append("Read(/%s/**)" % self.attachments_dir.lstrip("/"))
            args += ["--allowedTools", ",".join(allowed)]
        if model:
            args += ["--model", model]
        if self.session_id:
            args += ["--resume", self.session_id]

        self._start_process(
            program, args, env_extra={"MCP_TOOL_TIMEOUT": MCP_TOOL_TIMEOUT_MS}
        )
        full_prompt = format_prompt_with_attachments(prompt, attachments)
        self._process.write(full_prompt.encode("utf-8"))
        self._process.closeWriteChannel()
        return self.running

    # --- internal ---------------------------------------------------------

    def _write_mcp_config(self):
        """Write the MCP server declaration (carries the RPC token) as a
        file readable only by the current user."""
        bridge = os.path.join(os.path.dirname(__file__), "mcp_bridge.py")
        python_cmd, python_prepend = bridge_command()
        config = {
            "mcpServers": {
                "freecad": {
                    "command": python_cmd,
                    "args": python_prepend + [bridge],
                    "env": {
                        "FREECAD_RPC_PORT": str(self.rpc_port),
                        "FREECAD_RPC_TOKEN": self.rpc_token,
                    },
                }
            }
        }
        self._write_private_json(self._mcp_config_path, config)

    def _handle_event(self, event):
        etype = event.get("type")
        if etype == "system" and event.get("subtype") == "init":
            self.session_id = event.get("session_id", self.session_id)
        elif etype == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "text" and block.get("text"):
                    self.text_received.emit(block["text"])
                elif block.get("type") == "tool_use":
                    preview = json.dumps(block.get("input", {}), ensure_ascii=False)
                    if len(preview) > 300:
                        preview = preview[:300] + "…"
                    self.tool_used.emit(block.get("name", "?"), preview)
        elif etype == "result":
            self.session_id = event.get("session_id", self.session_id)
            if event.get("is_error"):
                self.error.emit(str(event.get("result", "unknown error")))

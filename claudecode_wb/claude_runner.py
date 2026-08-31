"""Launches the `claude` CLI (Claude Code, authenticated via the user's
subscription) in headless mode and streams stream-json events.

One conversation turn = one `claude -p` process; continuity is provided by
--resume <session_id>. The FreeCAD tools are supplied via --mcp-config
(mcp_bridge.py) and pre-authorized via --allowedTools.
"""

import json
import os
import shutil
import sys
import tempfile

from PySide import QtCore

SYSTEM_PROMPT = (
    "You are an agent that drives FreeCAD for the user, via the MCP tools "
    "of server 'freecad': exec_python (run Python in the FreeCAD session), "
    "get_scene (inspect documents/objects) and screenshot (view the 3D view). "
    "Method: inspect the scene first, act in small code steps, call "
    "App.ActiveDocument.recompute() after each modification, then verify "
    "visually with screenshot when relevant. "
    "If no document is open, create one with App.newDocument(). "
    "Reply to the user concisely, in the language the user writes in."
)

IS_WINDOWS = sys.platform == "win32"


def find_claude():
    """Find the claude binary, including outside FreeCAD's restricted PATH."""
    names = ("claude.cmd", "claude.exe") if IS_WINDOWS else ("claude",)
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    if IS_WINDOWS:
        candidates = (
            os.path.join(os.environ.get("APPDATA", ""), "npm", "claude.cmd"),
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

    QProcess cannot start .cmd/.bat shims directly on Windows; those must be
    run through cmd.exe.
    """
    if IS_WINDOWS and claude_path.lower().endswith((".cmd", ".bat")):
        return "cmd.exe", ["/c", claude_path]
    return claude_path, []


def _bridge_interpreter():
    """Return (command, prepend_args) of a Python able to run mcp_bridge.py."""
    if IS_WINDOWS:
        for name in ("python", "python3", "py"):
            path = shutil.which(name)
            if path:
                return path, []
        return "python", []
    return shutil.which("python3") or "python3", []


class ClaudeRunner(QtCore.QObject):
    """Un tour de conversation Claude Code dans un QProcess."""

    text_received = QtCore.Signal(str)          # assistant text
    tool_used = QtCore.Signal(str, str)         # (tool name, argument preview)
    turn_finished = QtCore.Signal(bool, str)    # (success, session_id)
    error = QtCore.Signal(str)

    def __init__(self, rpc_port, rpc_token, parent=None):
        super().__init__(parent)
        self.rpc_port = rpc_port
        self.rpc_token = rpc_token
        self.session_id = ""
        self._process = None
        self._stdout_buffer = b""
        self._workdir = os.path.join(tempfile.gettempdir(), "freecad_claude_agent")
        os.makedirs(self._workdir, exist_ok=True)
        self._mcp_config_path = os.path.join(self._workdir, "mcp-config.json")

    @property
    def running(self):
        return (
            self._process is not None
            and self._process.state() != QtCore.QProcess.NotRunning
        )

    def reset_session(self):
        self.session_id = ""

    def send_prompt(self, prompt, model="", skip_permissions=False):
        if self.running:
            self.error.emit("A turn is already running.")
            return
        claude = find_claude()
        if not claude:
            self.error.emit(
                "`claude` CLI not found. Install Claude Code and log in "
                "(run `claude` then /login) with your subscription."
            )
            return
        self._write_mcp_config()

        program, prepend = _claude_launch_command(claude)
        args = prepend + [
            "-p",
            "--verbose",
            "--output-format", "stream-json",
            "--mcp-config", self._mcp_config_path,
            "--append-system-prompt", SYSTEM_PROMPT,
        ]
        if skip_permissions:
            args.append("--dangerously-skip-permissions")
        else:
            args += [
                "--allowedTools",
                "mcp__freecad__exec_python,mcp__freecad__get_scene,mcp__freecad__screenshot",
            ]
        if model:
            args += ["--model", model]
        if self.session_id:
            args += ["--resume", self.session_id]

        self._stdout_buffer = b""
        self._process = QtCore.QProcess(self)
        self._process.setWorkingDirectory(self._workdir)
        self._process.readyReadStandardOutput.connect(self._on_stdout)
        self._process.finished.connect(self._on_finished)
        self._process.errorOccurred.connect(
            lambda _err: self.error.emit(
                "Failed to launch claude: " + self._process.errorString()
            )
        )
        self._process.start(program, args)
        self._process.write(prompt.encode("utf-8"))
        self._process.closeWriteChannel()

    def stop(self):
        if self.running:
            self._process.kill()

    # --- internal ---------------------------------------------------------

    def _write_mcp_config(self):
        bridge = os.path.join(os.path.dirname(__file__), "mcp_bridge.py")
        python_cmd, python_prepend = _bridge_interpreter()
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
        with open(self._mcp_config_path, "w", encoding="utf-8") as f:
            json.dump(config, f)

    def _on_stdout(self):
        self._stdout_buffer += bytes(self._process.readAllStandardOutput())
        while b"\n" in self._stdout_buffer:
            line, self._stdout_buffer = self._stdout_buffer.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line.decode("utf-8"))
            except ValueError:
                continue
            self._handle_event(event)

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

    def _on_finished(self, exit_code, _exit_status):
        stderr = bytes(self._process.readAllStandardError()).decode("utf-8", "replace")
        ok = exit_code == 0
        if not ok and stderr.strip():
            self.error.emit(stderr.strip()[-2000:])
        self.turn_finished.emit(ok, self.session_id)

"""Common machinery for agent CLI runners (Claude Code, ZCode…).

One conversation turn = one headless CLI process; continuity is provided by
each agent's resume flag. Subclasses live in claude_runner.py / zcode_runner.py
and only provide binary detection, argument building and event parsing; the
QProcess lifecycle and the incremental JSON stream parsing live here.

Every file written for the agent (working directory, MCP configuration
carrying the RPC token, attachments) lives in a per-user private directory
(see paths.py), never in a shared temporary directory.

The stdout parsers must tolerate both newline-delimited JSON (Claude Code) and
space-delimited JSON (ZCode writes JSON.stringify(e) + " "), so events are
extracted with raw_decode() instead of line splitting.
"""

import codecs
import json
import mimetypes
import os
import re
import shutil
import signal
import sys
import tempfile

from PySide import QtCore

from . import paths

SYSTEM_PROMPT = (
    "You are an agent that drives FreeCAD for the user, via the MCP tools "
    "of server 'freecad': exec_python (run Python in the FreeCAD session), "
    "get_scene (inspect documents/objects) and screenshot (view the 3D view). "
    "Method: inspect the scene first, act in small code steps, call "
    "App.ActiveDocument.recompute() after each modification, then verify "
    "visually with screenshot when relevant. "
    "If no document is open, create one with App.newDocument(). "
    "Each exec_python run is shown to the user for approval before it "
    "executes; if the user denies a run, do not resubmit the same code: "
    "explain what you intended or propose a different approach. "
    "Reply to the user concisely, in the language the user writes in."
)

ATTACHMENTS_SUBDIR = "attachments"
MAX_EVENT_BYTES = 10 * 1024 * 1024


def bridge_command():
    """Return (command, args) of a Python invocation able to run mcp_bridge.py."""
    if sys.platform == "win32":
        for name in ("python", "python3", "py"):
            path = shutil.which(name)
            if path:
                return path, []
        return "python", []
    return shutil.which("python3") or "python3", []


def format_prompt_with_attachments(prompt, attachment_paths):
    """Append the list of attached files to the user's prompt."""
    if not attachment_paths:
        return prompt
    lines = [
        prompt,
        "",
        "Attached files (the user attached them to this message). Read them "
        "with your Read tool when useful; FreeCAD documents (.FCStd) and "
        "CAD files can instead be opened inside FreeCAD with exec_python:",
    ]
    for path in attachment_paths:
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        lines.append("- %s (%s)" % (path, mime))
    return "\n".join(lines)


class AgentRunner(QtCore.QObject):
    """Base class: runs one agent CLI turn in a QProcess."""

    display_name = "agent"
    workdir_name = "freecad_agent"

    text_received = QtCore.Signal(str)          # assistant text
    tool_used = QtCore.Signal(str, str)         # (tool name, argument preview)
    turn_finished = QtCore.Signal(bool, str)    # (success, session_id)
    error = QtCore.Signal(str)

    def __init__(self, rpc_port=0, rpc_token="", parent=None):
        super().__init__(parent)
        self.rpc_port = rpc_port
        self.rpc_token = rpc_token
        self.session_id = ""
        self._process = None
        self._turn_active = False
        self._reset_after_turn = False
        self._stdout_buffer = b""
        self._process_pid = 0
        self._rpc_revoker = None
        self._stopping = False
        self.unrestricted = False
        self._workdir = paths.new_private_dir(self.workdir_name + "-")
        app = QtCore.QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.close)

    # --- public API --------------------------------------------------------

    @property
    def running(self):
        return self._turn_active

    def set_rpc(self, port, token, revoke=None):
        """Point the runner at the (lazily started) FreeCAD RPC server."""
        self.rpc_port = port
        self.rpc_token = token
        self._rpc_revoker = revoke

    def _revoke_rpc(self):
        revoke, self._rpc_revoker = self._rpc_revoker, None
        if revoke is not None:
            revoke()

    def reset_session(self):
        self.session_id = ""
        # A stopped process may still deliver its last session event.
        self._reset_after_turn = self.running
        self.clear_attachments()

    def stop(self):
        self._stopping = True
        self._revoke_rpc()
        self._kill_process_tree()
        if self._process is not None and self._process.state() != QtCore.QProcess.NotRunning:
            self._process.kill()

    def _kill_process_tree(self):
        if os.name == "posix" and self._process_pid:
            try:
                os.killpg(self._process_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass  # The launcher has not called setsid yet, or already exited.

    def close(self):
        """Revoke access, reap the agent, then clean only this runner's files."""
        self.stop()
        if self._process is not None and self._process.state() != QtCore.QProcess.NotRunning:
            self._process.waitForFinished(2000)
        shutil.rmtree(self._workdir, ignore_errors=True)

    # --- attachments -------------------------------------------------------

    @property
    def attachments_dir(self):
        return paths.private_dir(self._workdir, ATTACHMENTS_SUBDIR)

    def stage_attachment(self, name, data=None, source_path=None):
        """Copy a file (or raw bytes) into the private attachments directory.

        Returns the absolute path of the staged copy. File names are
        sanitised; each copy is created exclusively with a random prefix.
        """
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.basename(name)) or "file"
        fd, target = tempfile.mkstemp(suffix="_" + safe[:180], dir=self.attachments_dir)
        try:
            with os.fdopen(fd, "wb") as output:
                if data is not None:
                    output.write(bytes(data))
                else:
                    with open(source_path, "rb") as source:
                        shutil.copyfileobj(source, output)
        except Exception:
            os.unlink(target)
            raise
        return target

    def clear_attachments(self):
        shutil.rmtree(self.attachments_dir, ignore_errors=True)

    # --- helpers for subclasses --------------------------------------------

    def _write_private_json(self, path, obj):
        paths.write_private_file(path, json.dumps(obj, indent=2))

    def _start_process(self, program, args, env_extra=None):
        """Start the CLI process and wire the common signal handling."""
        self._stdout_buffer = b""
        self._turn_active = True
        self._stopping = False
        self._process_pid = 0
        if self._process is not None:
            self._process.deleteLater()
        self._process = QtCore.QProcess(self)
        process = self._process
        process.started.connect(lambda: self._remember_pid(process))
        self._process.setWorkingDirectory(self._workdir)
        if env_extra:
            env = QtCore.QProcessEnvironment.systemEnvironment()
            for key, value in env_extra.items():
                env.insert(key, value)
            self._process.setProcessEnvironment(env)
        # Ignore late signals from a previous process when a new turn starts.
        process.readyReadStandardOutput.connect(
            lambda: self._on_stdout() if self._process is process else None
        )
        process.finished.connect(
            lambda code, status: self._on_finished(code, status)
            if self._process is process else None
        )
        process.errorOccurred.connect(
            lambda error: self._on_process_error(error)
            if self._process is process else None
        )
        python, prepend = bridge_command()
        host = os.path.join(os.path.dirname(__file__), "process_host.py")
        self._process.start(python, prepend + [host, program] + args)
        return self.running

    def _remember_pid(self, process):
        if self._process is process:
            self._process_pid = int(process.processId())
            if self._stopping:
                self.stop()

    def _on_process_error(self, error):
        self.error.emit(
            "%s process error: %s" % (self.display_name, self._process.errorString())
        )
        # FailedToStart has no corresponding finished signal in QProcess.
        if error == QtCore.QProcess.FailedToStart:
            self._complete_turn(False)

    def _complete_turn(self, ok):
        if not self._turn_active:
            return
        self._revoke_rpc()
        self._turn_active = False
        self.unrestricted = False
        if self._reset_after_turn:
            self.session_id = ""
            self._reset_after_turn = False
        self.turn_finished.emit(ok, self.session_id)

    # --- internal ----------------------------------------------------------

    def _on_stdout(self):
        if self._stopping:
            self._process.readAllStandardOutput()
            self._stdout_buffer = b""
            return
        self._stdout_buffer += bytes(self._process.readAllStandardOutput())
        self._stdout_buffer = self._drain_events(self._stdout_buffer)

    def _drain_events(self, buffer, final=False):
        """Extract complete JSON objects from the buffer, keep the rest.

        raw_decode handles any delimiter between objects (newline, space).
        When the buffer starts with non-JSON noise (CLI banners, warnings),
        drop everything up to the next opening brace.
        """
        decoder = json.JSONDecoder()
        utf8 = codecs.getincrementaldecoder("utf-8")("replace")
        text = utf8.decode(buffer, final=False)
        # Preserve a split multibyte character as bytes until the next read.
        trailing_bytes = utf8.getstate()[0]
        pos = 0
        while True:
            while pos < len(text) and text[pos] in " \t\r\n":
                pos += 1
            if pos >= len(text):
                return trailing_bytes
            if text[pos] != "{":
                next_brace = text.find("{", pos)
                if next_brace < 0:
                    return trailing_bytes
                pos = next_brace
            try:
                event, end = decoder.raw_decode(text, pos)
            except (ValueError, RecursionError):
                first_key = pos + 1
                while first_key < len(text) and text[first_key].isspace():
                    first_key += 1
                if first_key < len(text) and text[first_key] not in ('"', "}"):
                    # This cannot be an incomplete JSON object: keys must be
                    # quoted. Recover even if a banner's brace never closes.
                    next_brace = text.find("{", first_key)
                    if next_brace < 0:
                        return trailing_bytes
                    pos = next_brace
                    continue
                end = self._object_end(text, pos)
                newline = text.find("\n", pos)
                if end is not None:
                    # Balanced but invalid object, e.g. a banner {version}.
                    pos = end
                    continue
                if newline >= 0:
                    # CLI events are minified JSON; a completed invalid line
                    # cannot be an unfinished event. Resume on the next line.
                    pos = newline + 1
                    continue
                retained = text[pos:].encode("utf-8") + trailing_bytes
                if final or len(retained) > MAX_EVENT_BYTES:
                    self.error.emit("Discarded incomplete or oversized agent JSON event.")
                    return b""
                return retained
            if len(text[pos:end].encode("utf-8")) > MAX_EVENT_BYTES:
                self.error.emit("Discarded oversized agent JSON event.")
                pos = end
                continue
            try:
                self._handle_event(event)
            except Exception as exc:  # noqa: BLE001 — one bad event must not kill the stream
                self.error.emit("Event handling failed: %r" % exc)
            pos = end

    @staticmethod
    def _object_end(text, start):
        """Locate a balanced object's end without mistaking quoted braces."""
        depth, quoted, escaped = 0, False, False
        for index in range(start, len(text)):
            char = text[index]
            if quoted:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quoted = False
            elif char == '"':
                quoted = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return index + 1
        return None

    def _on_finished(self, exit_code, _exit_status):
        self._on_stdout()
        if not self._stopping:
            self._stdout_buffer = self._drain_events(self._stdout_buffer, final=True)
            self._finish_output()
        self._kill_process_tree()
        self._process_pid = 0
        stderr = bytes(self._process.readAllStandardError()).decode("utf-8", "replace")
        ok = exit_code == 0 and _exit_status == QtCore.QProcess.NormalExit
        if not ok and stderr.strip():
            self.error.emit(stderr.strip()[-2000:])
        self._complete_turn(ok)

    def _finish_output(self):
        """Subclasses can flush text buffered while decoding the last events."""

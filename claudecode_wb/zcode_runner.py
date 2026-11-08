"""Launches the ZCode CLI (bundled with ZCode Desktop) in headless mode and
streams its JSON events.

One conversation turn = one `--prompt` process; continuity is provided by
--resume <sessionId>. The ZCode CLI (0.16.x) has no --mcp-config nor
--append-system-prompt flags, so the FreeCAD tools are declared in workspace
config files written inside the turn's working directory, and the system
prompt is provided as that workspace's AGENTS.md. Headless prompts always run
in "yolo" mode (the CLI default), which cannot be narrowed per-tool from the
command line: the panel therefore only lets the user send to ZCode after
opting in to "all tools without approval". exec_python itself is still
approved inside FreeCAD by rpc_server.py.

The ZCode login found on the system (~/.zcode) is only touched from
send_prompt(), i.e. after the panel has obtained the user's consent for the
Z.AI service.

The CLI ships inside the ZCode Desktop AppImage. Shared /tmp mount names are
never searched automatically. Set ZCODE_APPIMAGE to an AppImage or explicitly
trusted mount directory, or place the AppImage in a known application folder.
Executables and their ancestors must be owned by the user/root and protected
against modification by other users. Extracted bundles use a private cache.
"""

import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

from PySide import QtCore

from . import paths
from .runner_base import (
    SYSTEM_PROMPT,
    AgentRunner,
    bridge_command,
    format_prompt_with_attachments,
)

__all__ = ["ZCodeRunner", "find_zcode"]

_CLI_INNER_PATH = os.path.join("resources", "glm", "zcode.cjs")
_APPIMAGE_DIRS = (
    "~/Applications",
    "~/Downloads",
    "~/.local/bin",
    "/opt",
)

_ZCODE_HOME = os.path.expanduser("~/.zcode")
_CLI_CONFIG = os.path.join(_ZCODE_HOME, "cli", "config.json")
_DESKTOP_CONFIG = os.path.join(_ZCODE_HOME, "v2", "config.json")
_DESKTOP_SETTINGS = os.path.join(_ZCODE_HOME, "v2", "setting.json")


def find_zcode(cancelled=None):
    """Return (program, script_path) to run the ZCode CLI headless, or None."""
    if not sys.platform.startswith("linux"):
        return None
    explicit = os.environ.get("ZCODE_APPIMAGE")
    if explicit:
        # An invalid explicit selection must not silently select another CLI.
        return _resolve_explicit(os.path.expanduser(explicit), cancelled)
    for appimage in _candidate_appimages():
        if cancelled and cancelled():
            return None
        script = _extract_cli(appimage, cancelled)
        if script:
            return appimage, script
    return None


def _resolve_explicit(path, cancelled=None):
    """Resolve a user-provided ZCODE_APPIMAGE value (AppImage file or mount dir)."""
    if not paths.trusted_path(path):
        return None
    if os.path.isdir(path):
        script = os.path.join(path, _CLI_INNER_PATH)
        binary = os.path.join(path, "zcode")
        if (os.path.isfile(script) and os.path.isfile(binary)
                and os.access(binary, os.X_OK)
                and paths.trusted_path(script) and paths.trusted_path(binary)):
            return binary, script
        return None
    if os.path.isfile(path) and os.access(path, os.X_OK):
        script = _extract_cli(path, cancelled)
        if script:
            return path, script
    return None


def _candidate_appimages():
    """Known ZCode AppImage locations, newest first."""
    patterns = []
    for directory in _APPIMAGE_DIRS:
        base = os.path.expanduser(directory)
        patterns.append(os.path.join(base, "ZCode*.AppImage"))
        patterns.append(os.path.join(base, "ZCode*", "ZCode*.AppImage"))
    found = []
    for pattern in patterns:
        found.extend(glob.glob(pattern))
    return [
        path for path in _sorted_existing(*found)
        if os.path.isfile(path) and os.access(path, os.X_OK) and paths.trusted_path(path)
    ]


def _sorted_existing(*paths_or_globs):
    """Expand globs, drop missing entries, return paths sorted newest first."""
    paths = []
    for pattern in paths_or_globs:
        paths.extend(glob.glob(pattern))
    return sorted(
        (p for p in paths if os.path.exists(p)),
        key=os.path.getmtime,
        reverse=True,
    )


def _extract_cli(appimage, cancelled=None):
    """Extract the zcode.cjs CLI bundle from an AppImage into the cache.

    Returns the cached script path, or None if extraction failed. Re-extracts
    when the AppImage is newer than the cached copy.
    """
    if not paths.trusted_path(appimage):
        return None
    info = os.stat(appimage)
    identity = "%s:%d:%d" % (os.path.realpath(appimage), info.st_mtime_ns, info.st_size)
    cache_dir = paths.private_dir("zcode-cli")
    target = os.path.join(cache_dir, hashlib.sha256(identity.encode()).hexdigest() + ".cjs")
    if os.path.isfile(target) and paths.trusted_path(target):
        return target
    extraction = tempfile.mkdtemp(prefix="extract-", dir=cache_dir)
    try:
        with subprocess.Popen(
            [appimage, "--appimage-extract", "resources/glm/zcode.cjs"],
            cwd=extraction, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ) as process:
            deadline = time.monotonic() + 120
            while process.poll() is None:
                if (cancelled and cancelled()) or time.monotonic() >= deadline:
                    process.kill()
                    process.wait()
                    return None
                try:
                    process.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    continue
            if process.returncode != 0:
                return None
        extracted = os.path.join(extraction, "squashfs-root", "resources", "glm", "zcode.cjs")
        with open(extracted, encoding="utf-8") as source:
            paths.write_private_file(target, source.read())
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        shutil.rmtree(extraction, ignore_errors=True)
    return target if os.path.isfile(target) else None


def _sync_cli_model_config():
    """Make sure ~/.zcode/cli/config.json selects a working model provider.

    The ZCode CLI refuses headless prompts without a model config of its own,
    even when ZCode Desktop is logged in (the desktop keeps its provider
    settings elsewhere). Copy the desktop's selected provider entry —
    refreshing an existing copy too, so key rotation is picked up — and return
    None on success or an explanatory error message.
    """
    desktop = _read_json(_DESKTOP_CONFIG)
    providers = (desktop or {}).get("provider") or {}
    provider_id = _selected_desktop_provider(providers)
    entry = providers.get(provider_id) if provider_id else None
    if not entry or not entry.get("options", {}).get("apiKey"):
        return (
            "ZCode has no model provider configured. Log in once in ZCode "
            "Desktop (or run the ZCode CLI login), then retry."
        )

    config = _read_json(_CLI_CONFIG) or {}
    config.setdefault("provider", {})
    entry = dict(entry)
    entry.pop("systemDisabledReason", None)
    config["provider"][provider_id] = entry
    if not config.get("model"):
        config["model"] = {"main": "%s/%s" % (provider_id, _default_model(entry))}
    try:
        directory = paths.private_dir(os.path.dirname(_CLI_CONFIG))
        if not paths.trusted_path(directory):
            raise OSError("ZCode configuration directory is writable by another user")
        paths.write_private_file(_CLI_CONFIG, json.dumps(config, indent=2))
    except OSError as exc:
        return "Cannot write %s: %s" % (_CLI_CONFIG, exc)
    return None


def _selected_desktop_provider(providers):
    """Provider id chosen in ZCode Desktop, falling back to any enabled one."""
    settings = _read_json(_DESKTOP_SETTINGS) or {}
    for key in (settings.get("modelProviderFamilySelectedKeys") or {}).values():
        provider_id = str(key).partition(":")[2]
        if provider_id in providers:
            return provider_id
    for provider_id, entry in providers.items():
        if entry.get("enabled") and not entry.get("systemDisabledReason"):
            return provider_id
    return None


def _default_model(entry):
    """Default model of a desktop provider entry (lowest priority number)."""
    models = entry.get("models") or {}
    if models:
        return min(
            models,
            key=lambda mid: (models[mid].get("zcode") or {}).get("priority", 1 << 30),
        ).lower()
    return "glm-5.3"


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


class _ZCodePreparation(QtCore.QThread):
    """Locate/extract the CLI without blocking FreeCAD's GUI event loop."""

    def __init__(self, prompt, attachments, parent=None):
        super().__init__(parent)
        self.prompt = prompt
        self.attachments = list(attachments or [])
        self.command = None
        self.error_message = ""

    def run(self):
        try:
            self.command = find_zcode(cancelled=self.isInterruptionRequested)
            if self.isInterruptionRequested():
                return
            if not self.command:
                self.error_message = (
                    "ZCode CLI not found. Install ZCode Desktop (and launch it "
                    "once so its AppImage is available), or set ZCODE_APPIMAGE "
                    "to the ZCode AppImage path."
                )
            else:
                self.error_message = _sync_cli_model_config() or ""
        except Exception as exc:  # noqa: BLE001 — report worker failures in the panel
            self.error_message = "Cannot prepare ZCode: %s" % exc


class ZCodeRunner(AgentRunner):
    """Un tour de conversation ZCode dans un QProcess."""

    display_name = "ZCode"
    workdir_name = "freecad_zcode_agent"

    def __init__(self, rpc_port=0, rpc_token="", parent=None):
        super().__init__(rpc_port, rpc_token, parent=parent)
        self._message_texts = {}   # message id -> text already streamed
        self._streamed_any_text = False
        self._pending_text = ""    # model.streaming deltas, flushed per paragraph
        self._preparation = None
        self._preparation_cancelled = False

    def close(self):
        self._shutdown_preparation()
        super().close()

    def _shutdown_preparation(self):
        if self._preparation is not None:
            self._preparation_cancelled = True
            self._preparation.requestInterruption()
            # Extraction polls interruption, so shutdown also reaps its child.
            self._preparation.wait()

    def send_prompt(self, prompt, model="", skip_permissions=False, attachments=None):
        # `model` is ignored: the ZCode CLI (0.16.x) has no --model flag.
        # `skip_permissions` is ignored: headless prompts always run in yolo
        # mode, the CLI default (there is no --allowedTools equivalent); the
        # panel refuses to send unless the user opted in to that mode.
        if self.running:
            self.error.emit("A turn is already running.")
            return False
        if not self.rpc_port or not self.rpc_token:
            self.error.emit("FreeCAD RPC server is not running.")
            return False
        self._turn_active = True
        self._stopping = False
        self.unrestricted = True
        self._preparation_cancelled = False
        self._preparation = _ZCodePreparation(prompt, attachments, parent=self)
        self._preparation.finished.connect(self._on_prepared)
        self._preparation.start()
        return True

    def stop(self):
        super().stop()
        if self._preparation is not None:
            # Let extraction finish cleaning up, but never launch a cancelled turn.
            self._preparation_cancelled = True
            self._preparation.requestInterruption()

    @QtCore.Slot()
    def _on_prepared(self):
        preparation = self._preparation
        self._preparation = None
        try:
            # QThread clears its interruption flag when run() returns.
            if self._preparation_cancelled:
                self._complete_turn(False)
                return
            if preparation.error_message:
                self.error.emit(preparation.error_message)
                self._complete_turn(False)
                return
            self._launch_prepared(preparation)
        except Exception as exc:  # noqa: BLE001 — restore controls after configuration errors
            self.error.emit("Cannot launch ZCode: %s" % exc)
            self._complete_turn(False)
        finally:
            preparation.deleteLater()

    def _launch_prepared(self, preparation):
        program, script = preparation.command
        self._write_workspace_config()

        args = [
            script,
            "--prompt", format_prompt_with_attachments(
                preparation.prompt, preparation.attachments
            ),
            "--output-format", "stream-json",
            "--cwd", self._workdir,
            "--mode", "yolo",
        ]
        if self.session_id:
            args += ["--resume", self.session_id]

        self._message_texts = {}
        self._streamed_any_text = False
        self._pending_text = ""
        self._start_process(
            program, args, env_extra={"ELECTRON_RUN_AS_NODE": "1"}
        )

    # --- internal ---------------------------------------------------------

    def _write_workspace_config(self):
        """Declare the FreeCAD MCP server and instructions for this turn.

        ZCode reads MCP servers from the workspace config and instructions
        from the workspace AGENTS.md; writing them in our temp workdir keeps
        the user's own configuration untouched. Both documented workspace
        formats are provided (.zcode/config.json and .agents/mcp.json).
        """
        paths.write_private_file(
            os.path.join(self._workdir, "AGENTS.md"), SYSTEM_PROMPT + "\n"
        )

        bridge = os.path.join(os.path.dirname(__file__), "mcp_bridge.py")
        python_cmd, python_prepend = bridge_command()
        server = {
            "command": python_cmd,
            "args": python_prepend + [bridge],
            "env": {
                "FREECAD_RPC_PORT": str(self.rpc_port),
                "FREECAD_RPC_TOKEN": self.rpc_token,
            },
        }
        for directory, config in (
            (".zcode", {"mcp": {"servers": {"freecad": server}}}),
            (".agents", {"mcpServers": {"freecad": server}}),
        ):
            path = os.path.join(self._workdir, directory)
            os.makedirs(path, mode=0o700, exist_ok=True)
            filename = "config.json" if directory == ".zcode" else "mcp.json"
            self._write_private_json(os.path.join(path, filename), config)

    def _handle_event(self, event):
        etype = event.get("type")
        if etype in ("session.created", "session.resumed"):
            self._capture_session(event.get("sessionId"))
        elif etype == "model.streaming":
            payload = event.get("payload") or {}
            if payload.get("kind") == "text_delta" and payload.get("delta"):
                # Deltas arrive token by token; the panel displays one
                # paragraph per text_received, so buffer and flush on
                # paragraph boundaries only.
                self._pending_text += payload["delta"]
                self._streamed_any_text = True
                while "\n\n" in self._pending_text:
                    paragraph, self._pending_text = self._pending_text.split("\n\n", 1)
                    if paragraph.strip():
                        self.text_received.emit(paragraph + "\n\n")
        elif etype == "message.upserted":
            message = (event.get("payload") or {}).get("message") or {}
            if message.get("role") == "assistant":
                self._stream_message_text(event.get("payload") or {}, message)
        elif etype == "tool.updated":
            payload = event.get("payload") or {}
            if payload.get("kind") == "scheduled":  # carries the input preview
                self._emit_tool(payload)
        elif etype == "turn.failed":
            self.error.emit(_error_text(event.get("payload") or {}))
        elif etype == "result":
            self._capture_session(event.get("sessionId"))
            if self._pending_text.strip():
                self.text_received.emit(self._pending_text)
                self._pending_text = ""
            elif not self._streamed_any_text:
                text = _extract_plain_text(event.get("response"))
                if text:
                    self.text_received.emit(text)

    def _finish_output(self):
        if self._pending_text.strip():
            self.text_received.emit(self._pending_text)
            self._pending_text = ""

    def _capture_session(self, session_id):
        if isinstance(session_id, str) and session_id:
            self.session_id = session_id

    def _stream_message_text(self, payload, message):
        """Stream assistant text as the message grows across upserts.

        Upserts repeat the same message id with growing content, so only the
        delta since the previous upsert is emitted.
        """
        message_id = payload.get("id") or payload.get("messageId") or id(message)
        text = _extract_plain_text(message)
        if not text:
            return
        previous = self._message_texts.get(message_id, "")
        if text.startswith(previous) and len(text) > len(previous):
            self.text_received.emit(text[len(previous):])
            self._streamed_any_text = True
        self._message_texts[message_id] = text

    def _emit_tool(self, payload):
        tool = payload.get("tool") if isinstance(payload.get("tool"), dict) else {}
        name = (
            payload.get("name") or payload.get("toolName") or tool.get("name") or "?"
        )
        preview_source = (
            payload.get("input")
            or payload.get("arguments")
            or payload.get("parameters")
            or tool.get("input")
            or {}
        )
        preview = json.dumps(preview_source, ensure_ascii=False, default=str)
        if len(preview) > 300:
            preview = preview[:300] + "…"
        self.tool_used.emit(name, preview)


def _extract_plain_text(value):
    """Best-effort plain text from a message/content/response value."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        content = value.get("content", value.get("text"))
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif isinstance(block, str):
                    parts.append(block)
            return "".join(parts).strip()
    return ""


def _error_text(payload):
    for source in (payload.get("error"), payload):
        if isinstance(source, dict):
            message = source.get("message") or source.get("error")
            if isinstance(message, str) and message:
                return message
        elif isinstance(source, str) and source:
            return source
    return json.dumps(payload, ensure_ascii=False)[:500]

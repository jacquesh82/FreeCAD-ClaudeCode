"""Lance le CLI `claude` (Claude Code, authentifié via l'abonnement de
l'utilisateur) en mode headless et diffuse les événements stream-json.

Un tour de conversation = un process `claude -p` ; la continuité est assurée
par --resume <session_id>. Les outils FreeCAD sont fournis via --mcp-config
(pont mcp_bridge.py) et pré-autorisés via --allowedTools.
"""

import json
import os
import shutil
import tempfile

from PySide import QtCore

SYSTEM_PROMPT = (
    "Tu es un agent qui pilote FreeCAD pour l'utilisateur, via les outils MCP "
    "'freecad' : exec_python (exécuter du Python dans la session FreeCAD), "
    "get_scene (inspecter les documents/objets) et screenshot (voir la vue 3D). "
    "Méthode : inspecte d'abord la scène, agis par petites étapes de code, "
    "appelle App.ActiveDocument.recompute() après chaque modification, puis "
    "vérifie visuellement avec screenshot quand c'est pertinent. "
    "Si aucun document n'est ouvert, crée-en un avec App.newDocument(). "
    "Réponds à l'utilisateur en français, de façon concise."
)


def find_claude():
    """Trouve le binaire claude, y compris hors du PATH restreint de FreeCAD."""
    path = shutil.which("claude")
    if path:
        return path
    for candidate in (
        os.path.expanduser("~/.local/bin/claude"),
        os.path.expanduser("~/.claude/local/claude"),
        "/usr/local/bin/claude",
    ):
        if os.path.exists(candidate):
            return candidate
    return None


class ClaudeRunner(QtCore.QObject):
    """Un tour de conversation Claude Code dans un QProcess."""

    text_received = QtCore.Signal(str)          # texte de l'assistant
    tool_used = QtCore.Signal(str, str)         # (nom d'outil, aperçu des arguments)
    turn_finished = QtCore.Signal(bool, str)    # (succès, session_id)
    error = QtCore.Signal(str)

    def __init__(self, rpc_port, parent=None):
        super().__init__(parent)
        self.rpc_port = rpc_port
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
            self.error.emit("Un tour est déjà en cours.")
            return
        claude = find_claude()
        if not claude:
            self.error.emit(
                "CLI `claude` introuvable. Installe Claude Code puis connecte-toi "
                "(commande `claude` puis /login) avec ton abonnement."
            )
            return
        self._write_mcp_config()

        args = [
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
                "Échec du lancement de claude : " + self._process.errorString()
            )
        )
        self._process.start(claude, args)
        self._process.write(prompt.encode("utf-8"))
        self._process.closeWriteChannel()

    def stop(self):
        if self.running:
            self._process.kill()

    # --- interne ----------------------------------------------------------

    def _write_mcp_config(self):
        bridge = os.path.join(os.path.dirname(__file__), "mcp_bridge.py")
        python3 = shutil.which("python3") or "python3"
        config = {
            "mcpServers": {
                "freecad": {
                    "command": python3,
                    "args": [bridge],
                    "env": {"FREECAD_RPC_PORT": str(self.rpc_port)},
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
                self.error.emit(str(event.get("result", "erreur inconnue")))

    def _on_finished(self, exit_code, _exit_status):
        stderr = bytes(self._process.readAllStandardError()).decode("utf-8", "replace")
        ok = exit_code == 0
        if not ok and stderr.strip():
            self.error.emit(stderr.strip()[-2000:])
        self.turn_finished.emit(ok, self.session_id)

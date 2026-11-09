"""Local RPC server running INSIDE FreeCAD.

Listens on 127.0.0.1:<random port> via QTcpServer. Qt callbacks run on the
GUI thread, so the received Python code executes directly in the FreeCAD
context with no threading issues.

Security model (FreeCAD Addon Index rules for AI agents):
  * loopback only, random port, started on demand (never at FreeCAD startup);
  * every request carries the current turn's token, otherwise the connection is
    closed; any input that is not part of the protocol closes the connection;
  * model-generated code is shown to the user in FreeCAD and runs only once
    approved, unless the user opted in to automatic execution in the panel;
  * each approved run is wrapped in one undo transaction;
  * the agent's other tools (shell, files, web…) reach FreeCAD through the
    CLI's permission-prompt tool: every call is shown with its complete input
    and allowed only once approved; there is no automatic mode for them;
  * screenshots are written to a per-user private directory.

Protocol: one JSON request per line, one JSON response per line.
  {"op": "exec", "token": "...", "code": "..."}            -> {"ok": true, "stdout": "..."}
  {"op": "scene", "token": "..."}                          -> {"ok": true, "scene": "..."}
  {"op": "screenshot", "token": "...", "width": 1280, ...} -> {"ok": true, "path": "..."}
  {"op": "permission", "token": "...", "tool_name": "Bash", "input": {...}}
                                                           -> {"ok": true, "allow": false}
"""

import contextlib
import hmac
import io
import json
import os
import secrets
import shutil
import traceback

import FreeCAD as App

from PySide import QtCore, QtNetwork

from . import approval, paths

MAX_REQUEST_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BUFFER_BYTES = 16 * 1024 * 1024
MAX_CONNECTIONS = 8
REQUEST_TIMEOUT_MS = 15_000

DENIED_MESSAGE = (
    "The user reviewed this code in FreeCAD and denied running it. Do not "
    "resubmit the same code; explain what you intended or propose a "
    "different approach."
)


class FreeCADRpcServer:
    def __init__(self):
        self._token = secrets.token_urlsafe(32)
        self._active = False
        self._server = QtNetwork.QTcpServer()
        self._server.setMaxPendingConnections(MAX_CONNECTIONS)
        self._buffers = {}  # socket -> bytes waiting for a newline
        self._timers = {}
        self._approval_open = False
        # Opt-in set by the panel; off by default per the Index rules.
        self.auto_approve = False
        self.agent_name = "The agent"
        # Optional callable(kind: "exec"|"tool", approved: bool, detail: str)
        # notified of every decision (shown in the chat history).
        self.decision_callback = None
        self._server.newConnection.connect(self._on_new_connection)
        if not self._server.listen(QtNetwork.QHostAddress.LocalHost, 0):
            raise RuntimeError(
                "Cannot listen on localhost: " + self._server.errorString()
            )
        self._screenshot_dir = paths.new_private_dir("screenshots-")
        app = QtCore.QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.close)

    @property
    def port(self):
        return int(self._server.serverPort())

    @property
    def token(self):
        return self._token

    def close(self):
        global _instance
        self.end_turn(self._token)
        self._server.close()
        shutil.rmtree(self._screenshot_dir, ignore_errors=True)
        if _instance is self:
            _instance = None

    def begin_turn(self):
        """Only the current turn's bridge may call this FreeCAD instance."""
        self.end_turn(self._token)
        self._token = secrets.token_urlsafe(32)
        self._active = True
        return self._token

    def end_turn(self, token):
        if not hmac.compare_digest(token, self._token):
            return  # A late completion must not revoke a newer turn.
        self._active = False
        self.auto_approve = False
        if self._approval_open:
            approval.cancel_pending_approval()
        for sock in list(self._buffers):
            self._drop(sock, "turn ended")

    # --- réseau -----------------------------------------------------------

    def _on_new_connection(self):
        while self._server.hasPendingConnections():
            sock = self._server.nextPendingConnection()
            if len(self._buffers) >= MAX_CONNECTIONS:
                sock.abort()
                sock.deleteLater()
                continue
            sock.setReadBufferSize(MAX_REQUEST_BYTES + 1)
            self._buffers[sock] = b""
            timer = QtCore.QTimer(sock)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda s=sock: self._drop(s, "request timeout"))
            self._timers[sock] = timer
            timer.start(REQUEST_TIMEOUT_MS)
            sock.readyRead.connect(lambda s=sock: self._on_ready_read(s))
            sock.disconnected.connect(lambda s=sock: self._forget_socket(s))
            if sock.bytesAvailable():
                self._on_ready_read(sock)

    def _forget_socket(self, sock):
        self._buffers.pop(sock, None)
        timer = self._timers.pop(sock, None)
        if timer is not None:
            timer.stop()
        sock.deleteLater()

    def _on_ready_read(self, sock):
        if sock not in self._buffers:
            return
        buffer = self._buffers.get(sock, b"") + bytes(sock.readAll())
        total = sum(len(data) for s, data in self._buffers.items() if s is not sock) + len(buffer)
        if len(buffer) > MAX_REQUEST_BYTES or total > MAX_TOTAL_BUFFER_BYTES:
            self._drop(sock, "request too large")
            return
        self._buffers[sock] = buffer
        while b"\n" in self._buffers.get(sock, b""):
            line, rest = self._buffers[sock].split(b"\n", 1)
            self._buffers[sock] = rest
            if not line.strip():
                continue
            try:
                request = json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError, RecursionError):
                self._drop(sock, "input is not a JSON request")
                return
            if not isinstance(request, dict):
                self._drop(sock, "input is not a JSON object")
                return
            if not self._authorized(request):
                App.Console.PrintWarning(
                    "[Claude Code Connector] rejected RPC request: bad token\n"
                )
                self._reply(sock, {"ok": False, "error": "unauthorized: invalid or missing token"})
                self._finish_connection(sock)
                return
            if not self._valid_request(request):
                self._drop(sock, "invalid RPC operation or arguments")
                return
            self._timers[sock].stop()  # Approval has its own longer tool timeout.
            try:
                response = self._dispatch(request)
            except Exception:  # noqa: BLE001 — report to the agent, keep serving
                response = {"ok": False, "error": traceback.format_exc()}
            if sock in self._buffers:  # Stop may run in the modal approval loop.
                self._reply(sock, response)
                self._finish_connection(sock)
            return  # One request per connection; the bridge opens one per tool.

    def _reply(self, sock, response):
        sock.write((json.dumps(response) + "\n").encode("utf-8"))
        sock.flush()

    def _finish_connection(self, sock):
        # A client that stops reading must not retain an outgoing response
        # indefinitely. Also handle disconnects that preceded signal wiring.
        timer = self._timers.get(sock)
        if timer is not None:
            timer.start(REQUEST_TIMEOUT_MS)
        sock.disconnectFromHost()
        if sock in self._buffers and sock.state() == QtNetwork.QAbstractSocket.UnconnectedState:
            self._forget_socket(sock)

    def _drop(self, sock, why):
        """Close a connection whose input is not part of the protocol."""
        App.Console.PrintWarning(
            "[Claude Code Connector] closed RPC connection: %s\n" % why
        )
        if sock not in self._buffers:
            return
        self._forget_socket(sock)
        sock.abort()

    def _authorized(self, request):
        supplied = request.get("token", "")
        try:
            return self._active and isinstance(supplied, str) and hmac.compare_digest(
                supplied.encode("utf-8"), self._token.encode("utf-8")
            )
        except UnicodeEncodeError:
            return False

    @staticmethod
    def _valid_request(request):
        op = request.get("op")
        if op == "exec":
            return isinstance(request.get("code"), str)
        if op == "scene":
            return True
        if op == "screenshot":
            return all(type(request.get(key, default)) is int
                       for key, default in (("width", 1280), ("height", 800)))
        if op == "permission":
            return isinstance(request.get("tool_name"), str) and isinstance(
                request.get("input"), dict
            )
        return False

    # --- opérations -------------------------------------------------------

    def _dispatch(self, request):
        op = request.get("op")
        if op == "exec":
            return self._op_exec(request.get("code", ""))
        if op == "scene":
            return self._op_scene()
        if op == "screenshot":
            return self._op_screenshot(
                int(request.get("width", 1280)), int(request.get("height", 800))
            )
        if op == "permission":
            return self._op_permission(request["tool_name"], request["input"])
        return {"ok": False, "error": "unknown op: %r" % op}

    def _op_exec(self, code):
        import FreeCADGui as Gui

        if not isinstance(code, str):
            return {"ok": False, "error": "code must be a string"}
        lease = self._token
        if not self._active:
            return {"ok": False, "error": "The agent turn has ended."}

        if not self.auto_approve:
            if self._approval_open:
                return {
                    "ok": False,
                    "error": "Another code run is waiting for the user's "
                    "approval; retry once it has been answered.",
                }
            self._approval_open = True
            try:
                approved = approval.request_exec_approval(code, self.agent_name)
            finally:
                self._approval_open = False
            self._notify("exec", approved, code)
            if not approved:
                App.Console.PrintMessage(
                    "[Claude Code Connector] user denied a code run\n"
                )
                return {"ok": False, "denied": True, "error": DENIED_MESSAGE}
        else:
            self._notify("exec", True, code)

        if not self._active or lease != self._token:
            return {"ok": False, "error": "The agent turn ended before execution."}

        env = {"App": App, "FreeCAD": App, "Gui": Gui, "FreeCADGui": Gui}
        out = io.StringIO()
        try:
            transaction = self._open_transaction()
        except Exception:
            return {"ok": False, "error": traceback.format_exc()}
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                exec(compile(code, "<agent>", "exec"), env, env)
        except Exception:
            return {"ok": False, "stdout": out.getvalue(), "error": traceback.format_exc()}
        finally:
            self._commit_transaction(transaction)
        return {"ok": True, "stdout": out.getvalue()}

    def _op_permission(self, tool_name, tool_input):
        """Ask the user about one call of the agent's own tools. Never
        automatic: the exec auto-run opt-in does not apply here."""
        if not self._active:
            return {"ok": False, "error": "The agent turn has ended."}
        if self._approval_open:
            return {
                "ok": True,
                "allow": False,
                "message": "Another request is waiting for the user's "
                "approval; retry this tool call once it has been answered.",
            }
        lease = self._token
        self._approval_open = True
        try:
            allowed = approval.request_tool_approval(tool_name, tool_input, self.agent_name)
        finally:
            self._approval_open = False
        if not self._active or lease != self._token:
            allowed = False
        self._notify("tool", allowed, tool_name)
        return {"ok": True, "allow": allowed}

    def _notify(self, kind, approved, detail):
        if self.decision_callback is not None:
            try:
                self.decision_callback(kind, approved, detail)
            except Exception:  # noqa: BLE001 — UI feedback must not break exec
                pass

    def _open_transaction(self):
        """Include all changed documents, even ones created by this run.

        FreeCAD's application-wide transaction lazily opens a transaction in
        each modified document, sharing one ID. Persist until exec completes.
        """
        return App.setActiveTransaction("%s: agent code" % self.agent_name, True)

    def _commit_transaction(self, transaction):
        try:
            App.closeActiveTransaction(False, transaction)
        except Exception as exc:  # noqa: BLE001 — user code may close transactions/documents
            App.Console.PrintWarning(
                "[Claude Code Connector] cannot close undo transaction: %s\n" % exc
            )

    def _op_scene(self):
        lines = []
        docs = App.listDocuments()
        if not docs:
            lines.append("(no open document)")
        for name, doc in docs.items():
            active = " [active]" if App.ActiveDocument is doc else ""
            lines.append("Document %r%s — file: %s" % (name, active, doc.FileName or "(unsaved)"))
            for obj in doc.Objects:
                extra = ""
                shape = getattr(obj, "Shape", None)
                if shape is not None:
                    try:
                        bb = shape.BoundBox
                        extra = " bbox=(%.1f x %.1f x %.1f)" % (
                            bb.XLength, bb.YLength, bb.ZLength,
                        )
                    except Exception:
                        pass
                err = " !ERROR" if obj.State and "Invalid" in [str(s) for s in obj.State] else ""
                lines.append(
                    "  - %s (Label=%r, Type=%s)%s%s" % (obj.Name, obj.Label, obj.TypeId, extra, err)
                )
        return {"ok": True, "scene": "\n".join(lines)}

    def _op_screenshot(self, width, height):
        import FreeCADGui as Gui

        if Gui.ActiveDocument is None or Gui.ActiveDocument.ActiveView is None:
            return {"ok": False, "error": "No active 3D view."}
        width = max(16, min(width, 4096))
        height = max(16, min(height, 4096))
        path = os.path.join(self._screenshot_dir, "view.png")
        Gui.ActiveDocument.ActiveView.saveImage(path, width, height, "White")
        paths.restrict_file(path)
        return {"ok": True, "path": path}


_instance = None


def is_running():
    return _instance is not None


def get_server():
    """Return the RPC server (started on first use, singleton)."""
    global _instance
    if _instance is None:
        _instance = FreeCADRpcServer()
        App.Console.PrintWarning(
            "[Claude Code Connector] FreeCAD RPC server started on 127.0.0.1:%d. "
            "Connected agents can run Python in FreeCAD with your user "
            "permissions; each run is shown for approval unless automatic "
            "execution was enabled in the panel.\n" % _instance.port
        )
    return _instance

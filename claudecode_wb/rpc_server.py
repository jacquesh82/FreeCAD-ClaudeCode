"""Local RPC server running INSIDE FreeCAD.

Listens on 127.0.0.1:<random port> via QTcpServer. Qt callbacks run on the
GUI thread, so the received Python code executes directly in the FreeCAD
context with no threading issues.

Protocol: one JSON request per line, one JSON response per line. Every
request must carry the per-session "token"; unauthorized requests are
rejected without dispatching.
  {"op": "exec", "token": "...", "code": "..."}            -> {"ok": true, "stdout": "..."}
  {"op": "scene", "token": "..."}                          -> {"ok": true, "scene": "..."}
  {"op": "screenshot", "token": "...", "width": 1280, ...} -> {"ok": true, "path": "..."}
"""

import contextlib
import hmac
import io
import json
import os
import secrets
import tempfile
import traceback

import FreeCAD as App

from PySide import QtNetwork


class FreeCADRpcServer:
    def __init__(self):
        self._token = secrets.token_urlsafe(32)
        self._server = QtNetwork.QTcpServer()
        self._buffers = {}  # socket -> bytes waiting for a newline
        self._server.newConnection.connect(self._on_new_connection)
        if not self._server.listen(QtNetwork.QHostAddress.LocalHost, 0):
            raise RuntimeError(
                "Cannot listen on localhost: " + self._server.errorString()
            )

    @property
    def port(self):
        return int(self._server.serverPort())

    @property
    def token(self):
        return self._token

    def close(self):
        self._server.close()

    # --- réseau -----------------------------------------------------------

    def _on_new_connection(self):
        while self._server.hasPendingConnections():
            sock = self._server.nextPendingConnection()
            self._buffers[sock] = b""
            sock.readyRead.connect(lambda s=sock: self._on_ready_read(s))
            sock.disconnected.connect(lambda s=sock: self._buffers.pop(s, None))

    def _on_ready_read(self, sock):
        self._buffers[sock] = self._buffers.get(sock, b"") + bytes(sock.readAll())
        while b"\n" in self._buffers.get(sock, b""):
            line, rest = self._buffers[sock].split(b"\n", 1)
            self._buffers[sock] = rest
            if not line.strip():
                continue
            try:
                request = json.loads(line.decode("utf-8"))
                response = self._dispatch(request)
            except Exception:
                response = {"ok": False, "error": traceback.format_exc()}
            sock.write((json.dumps(response) + "\n").encode("utf-8"))
            sock.flush()

    # --- opérations -------------------------------------------------------

    def _dispatch(self, request):
        supplied = request.get("token", "")
        if not isinstance(supplied, str) or not hmac.compare_digest(
            supplied.encode("utf-8"), self._token.encode("utf-8")
        ):
            App.Console.PrintWarning(
                "[Claude Code Connector] rejected RPC request: bad token\n"
            )
            return {"ok": False, "error": "unauthorized: invalid or missing token"}
        op = request.get("op")
        if op == "exec":
            return self._op_exec(request.get("code", ""))
        if op == "scene":
            return self._op_scene()
        if op == "screenshot":
            return self._op_screenshot(
                int(request.get("width", 1280)), int(request.get("height", 800))
            )
        return {"ok": False, "error": "unknown op: %r" % op}

    def _op_exec(self, code):
        import FreeCADGui as Gui

        env = {"App": App, "FreeCAD": App, "Gui": Gui, "FreeCADGui": Gui}
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                exec(compile(code, "<claude>", "exec"), env, env)
        except Exception:
            return {"ok": False, "stdout": out.getvalue(), "error": traceback.format_exc()}
        return {"ok": True, "stdout": out.getvalue()}

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
        path = os.path.join(
            tempfile.gettempdir(), "freecad_claude_screenshot.png"
        )
        Gui.ActiveDocument.ActiveView.saveImage(path, width, height, "White")
        return {"ok": True, "path": path}


_instance = None


def get_server():
    """Return the RPC server (lazily started, singleton)."""
    global _instance
    if _instance is None:
        _instance = FreeCADRpcServer()
        App.Console.PrintMessage(
            "[Claude Code Connector] FreeCAD RPC server on 127.0.0.1:%d\n"
            % _instance.port
        )
    return _instance

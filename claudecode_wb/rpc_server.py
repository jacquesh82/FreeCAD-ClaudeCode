"""Serveur RPC local exécuté DANS FreeCAD.

Écoute sur 127.0.0.1:<port aléatoire> via QTcpServer : les callbacks Qt tournent
sur le thread GUI, donc le code Python reçu s'exécute directement dans le
contexte FreeCAD sans problème de thread.

Protocole : une requête JSON par ligne, une réponse JSON par ligne.
  {"op": "exec", "code": "..."}            -> {"ok": true, "stdout": "..."}
  {"op": "scene"}                          -> {"ok": true, "scene": "..."}
  {"op": "screenshot", "width": 1280, ...} -> {"ok": true, "path": "..."}
"""

import contextlib
import io
import json
import os
import tempfile
import traceback

import FreeCAD as App

from PySide import QtNetwork


class FreeCADRpcServer:
    def __init__(self):
        self._server = QtNetwork.QTcpServer()
        self._buffers = {}  # socket -> bytes en attente d'une fin de ligne
        self._server.newConnection.connect(self._on_new_connection)
        if not self._server.listen(QtNetwork.QHostAddress.LocalHost, 0):
            raise RuntimeError(
                "Impossible d'écouter en local : " + self._server.errorString()
            )

    @property
    def port(self):
        return int(self._server.serverPort())

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
        op = request.get("op")
        if op == "exec":
            return self._op_exec(request.get("code", ""))
        if op == "scene":
            return self._op_scene()
        if op == "screenshot":
            return self._op_screenshot(
                int(request.get("width", 1280)), int(request.get("height", 800))
            )
        if op == "sketch":
            return self._op_sketch()
        return {"ok": False, "error": "opération inconnue : %r" % op}

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
            lines.append("(aucun document ouvert)")
        for name, doc in docs.items():
            active = " [actif]" if App.ActiveDocument is doc else ""
            lines.append("Document %r%s — fichier: %s" % (name, active, doc.FileName or "(non enregistré)"))
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
                err = " !ERREUR" if obj.State and "Invalid" in [str(s) for s in obj.State] else ""
                lines.append(
                    "  - %s (Label=%r, Type=%s)%s%s" % (obj.Name, obj.Label, obj.TypeId, extra, err)
                )
        return {"ok": True, "scene": "\n".join(lines)}

    def _op_screenshot(self, width, height):
        import FreeCADGui as Gui

        if Gui.ActiveDocument is None or Gui.ActiveDocument.ActiveView is None:
            return {"ok": False, "error": "Aucune vue 3D active."}
        path = os.path.join(
            tempfile.gettempdir(), "freecad_claude_screenshot.png"
        )
        Gui.ActiveDocument.ActiveView.saveImage(path, width, height, "White")
        return {"ok": True, "path": path}

    def _op_sketch(self):
        from . import sketch

        data = sketch.get_sketch_json()
        if not data:
            return {
                "ok": False,
                "error": "Aucun croquis : le volet Croquis est vide ou fermé.",
            }
        return {"ok": True, "sketch": data}


_instance = None


def get_server():
    """Retourne le serveur RPC (démarré paresseusement, singleton)."""
    global _instance
    if _instance is None:
        _instance = FreeCADRpcServer()
        App.Console.PrintMessage(
            "[Claude Code] Serveur RPC FreeCAD sur 127.0.0.1:%d\n" % _instance.port
        )
    return _instance

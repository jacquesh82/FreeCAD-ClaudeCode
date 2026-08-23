#!/usr/bin/env python3
"""Pont MCP (stdio) entre Claude Code et le serveur RPC FreeCAD.

Ce script est lancé par le CLI `claude` (déclaré dans un fichier --mcp-config).
Il parle le protocole MCP en JSON-RPC ligne-par-ligne sur stdin/stdout et
relaie chaque appel d'outil vers FreeCAD via TCP (127.0.0.1:$FREECAD_RPC_PORT).

Dépendances : stdlib uniquement (doit tourner avec le python3 système).
"""

import base64
import json
import os
import socket
import sys

RPC_HOST = "127.0.0.1"
RPC_PORT = int(os.environ.get("FREECAD_RPC_PORT", "0"))

TOOLS = [
    {
        "name": "exec_python",
        "description": (
            "Exécute du code Python dans la session FreeCAD ouverte. "
            "Variables disponibles : App/FreeCAD, Gui/FreeCADGui. "
            "Utilise print() pour renvoyer des valeurs. "
            "Pense à App.ActiveDocument.recompute() après modification."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Code Python à exécuter"}
            },
            "required": ["code"],
        },
    },
    {
        "name": "get_scene",
        "description": (
            "Liste les documents FreeCAD ouverts et leurs objets "
            "(nom, label, type, bounding box, erreurs)."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "screenshot",
        "description": (
            "Capture la vue 3D active de FreeCAD et renvoie l'image (PNG) "
            "pour vérification visuelle du modèle."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "width": {"type": "integer", "default": 1280},
                "height": {"type": "integer", "default": 800},
            },
        },
    },
]


def rpc_call(payload, timeout=180.0):
    """Envoie une requête JSON-ligne au serveur RPC FreeCAD, renvoie la réponse."""
    with socket.create_connection((RPC_HOST, RPC_PORT), timeout=timeout) as sock:
        sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf.decode("utf-8"))


def tool_result(text, is_error=False, extra_content=None):
    content = [{"type": "text", "text": text}]
    if extra_content:
        content.extend(extra_content)
    return {"content": content, "isError": is_error}


def handle_tool_call(name, args):
    if RPC_PORT == 0:
        return tool_result("FREECAD_RPC_PORT non défini — FreeCAD est-il lancé ?", True)
    try:
        if name == "exec_python":
            resp = rpc_call({"op": "exec", "code": args.get("code", "")})
            if resp.get("ok"):
                out = resp.get("stdout", "")
                return tool_result("OK\n" + out if out else "OK (aucune sortie)")
            return tool_result(
                (resp.get("stdout", "") + "\n" + resp.get("error", "erreur inconnue")).strip(),
                True,
            )
        if name == "get_scene":
            resp = rpc_call({"op": "scene"})
            if resp.get("ok"):
                return tool_result(resp.get("scene", ""))
            return tool_result(resp.get("error", "erreur inconnue"), True)
        if name == "screenshot":
            resp = rpc_call(
                {
                    "op": "screenshot",
                    "width": args.get("width", 1280),
                    "height": args.get("height", 800),
                }
            )
            if not resp.get("ok"):
                return tool_result(resp.get("error", "erreur inconnue"), True)
            with open(resp["path"], "rb") as f:
                data = base64.b64encode(f.read()).decode("ascii")
            return tool_result(
                "Capture de la vue 3D :",
                extra_content=[
                    {"type": "image", "data": data, "mimeType": "image/png"}
                ],
            )
        return tool_result("Outil inconnu : %s" % name, True)
    except ConnectionRefusedError:
        return tool_result(
            "Connexion à FreeCAD refusée (port %d) — le volet Claude Code est-il ouvert ?" % RPC_PORT,
            True,
        )
    except Exception as exc:  # noqa: BLE001 — tout remonter à l'agent
        return tool_result("Erreur du pont MCP : %r" % exc, True)


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method = msg.get("method")
        msg_id = msg.get("id")
        if method == "initialize":
            requested = msg.get("params", {}).get("protocolVersion", "2024-11-05")
            send(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "protocolVersion": requested,
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "freecad", "version": "0.1.0"},
                    },
                }
            )
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = msg.get("params", {})
            result = handle_tool_call(
                params.get("name", ""), params.get("arguments", {}) or {}
            )
            send({"jsonrpc": "2.0", "id": msg_id, "result": result})
        elif method == "ping":
            send({"jsonrpc": "2.0", "id": msg_id, "result": {}})
        elif msg_id is not None:
            # Méthode inconnue mais requête (pas une notification) : répondre.
            send(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32601, "message": "méthode inconnue : %s" % method},
                }
            )
        # Les notifications (notifications/initialized, etc.) sont ignorées.


if __name__ == "__main__":
    main()

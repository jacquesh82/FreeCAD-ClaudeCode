#!/usr/bin/env python3
"""MCP (stdio) bridge between Claude Code and the FreeCAD RPC server.

This script is launched by the `claude` CLI (declared in a --mcp-config file).
It speaks the MCP protocol as line-delimited JSON-RPC on stdin/stdout and
forwards each tool call to FreeCAD over TCP (127.0.0.1:$FREECAD_RPC_PORT),
authenticating with $FREECAD_RPC_TOKEN.

Dependencies: stdlib only (must run with the system Python).
"""

import base64
import json
import os
import socket
import sys

RPC_HOST = "127.0.0.1"
RPC_PORT = int(os.environ.get("FREECAD_RPC_PORT", "0"))
RPC_TOKEN = os.environ.get("FREECAD_RPC_TOKEN", "")

TOOLS = [
    {
        "name": "exec_python",
        "description": (
            "Runs arbitrary Python code inside the open FreeCAD session. "
            "Available variables: App/FreeCAD, Gui/FreeCADGui. "
            "Use print() to return values. "
            "Remember to call App.ActiveDocument.recompute() after modifications."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "Python code to execute"}
            },
            "required": ["code"],
        },
    },
    {
        "name": "get_scene",
        "description": (
            "Lists the open FreeCAD documents and their objects "
            "(name, label, type, bounding box, errors)."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "screenshot",
        "description": (
            "Captures the active FreeCAD 3D view and returns the image (PNG) "
            "for visual verification of the model."
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
    """Send one JSON-line request to the FreeCAD RPC server, return the response."""
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
    if RPC_PORT == 0 or not RPC_TOKEN:
        return tool_result(
            "FREECAD_RPC_PORT/FREECAD_RPC_TOKEN not set — is the FreeCAD "
            "Claude Code Connector panel open?",
            True,
        )
    try:
        if name == "exec_python":
            resp = rpc_call({"op": "exec", "token": RPC_TOKEN, "code": args.get("code", "")})
            if resp.get("ok"):
                out = resp.get("stdout", "")
                return tool_result("OK\n" + out if out else "OK (no output)")
            return tool_result(
                (resp.get("stdout", "") + "\n" + resp.get("error", "unknown error")).strip(),
                True,
            )
        if name == "get_scene":
            resp = rpc_call({"op": "scene", "token": RPC_TOKEN})
            if resp.get("ok"):
                return tool_result(resp.get("scene", ""))
            return tool_result(resp.get("error", "unknown error"), True)
        if name == "screenshot":
            resp = rpc_call(
                {
                    "op": "screenshot",
                    "token": RPC_TOKEN,
                    "width": args.get("width", 1280),
                    "height": args.get("height", 800),
                }
            )
            if not resp.get("ok"):
                return tool_result(resp.get("error", "unknown error"), True)
            with open(resp["path"], "rb") as f:
                data = base64.b64encode(f.read()).decode("ascii")
            return tool_result(
                "3D view screenshot:",
                extra_content=[
                    {"type": "image", "data": data, "mimeType": "image/png"}
                ],
            )
        return tool_result("Unknown tool: %s" % name, True)
    except ConnectionRefusedError:
        return tool_result(
            "Connection to FreeCAD refused (port %d) — is the Claude Code "
            "Connector panel open?" % RPC_PORT,
            True,
        )
    except Exception as exc:  # noqa: BLE001 — surface everything to the agent
        return tool_result("MCP bridge error: %r" % exc, True)


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
            # Unknown method but a request (not a notification): answer it.
            send(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32601, "message": "unknown method: %s" % method},
                }
            )
        # Notifications (notifications/initialized, etc.) are ignored.


if __name__ == "__main__":
    main()

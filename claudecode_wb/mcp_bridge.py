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
import time

RPC_HOST = "127.0.0.1"
try:
    RPC_PORT = int(os.environ.get("FREECAD_RPC_PORT", "0"))
except ValueError:
    RPC_PORT = 0
RPC_TOKEN = os.environ.get("FREECAD_RPC_TOKEN", "")
MAX_MESSAGE_CHARS = 8 * 1024 * 1024
MAX_RESPONSE_BYTES = 32 * 1024 * 1024

TOOLS = [
    {
        "name": "exec_python",
        "description": (
            "Runs Python code inside the open FreeCAD session. "
            "Available variables: App/FreeCAD, Gui/FreeCADGui. "
            "Use print() to return values. "
            "Remember to call App.ActiveDocument.recompute() after modifications. "
            "The complete code is shown to the user inside FreeCAD and runs only "
            "once they approve it; this may take a while. If the user denies a "
            "run, the call returns an error: do not resubmit the same code, "
            "explain or propose a different approach instead."
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


# exec_python waits for the user's approval in FreeCAD: allow a long review.
EXEC_TIMEOUT = 3600.0


def rpc_call(payload, timeout=180.0):
    """Send one JSON-line request to the FreeCAD RPC server, return the response."""
    with socket.create_connection((RPC_HOST, RPC_PORT), timeout=timeout) as sock:
        deadline = time.monotonic() + timeout
        sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        buf = b""
        while not buf.endswith(b"\n"):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("FreeCAD RPC response timed out")
            sock.settimeout(remaining)
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
            if len(buf) > MAX_RESPONSE_BYTES:
                raise ValueError("FreeCAD RPC response too large")
    response = json.loads(buf.decode("utf-8"))
    if not isinstance(response, dict):
        raise ValueError("FreeCAD RPC response must be an object")
    return response


def tool_result(text, is_error=False, extra_content=None):
    content = [{"type": "text", "text": text}]
    if extra_content:
        content.extend(extra_content)
    return {"content": content, "isError": is_error}


def handle_tool_call(name, args):
    if not isinstance(args, dict):
        return tool_result("Tool arguments must be an object", True)
    if RPC_PORT == 0 or not RPC_TOKEN:
        return tool_result(
            "FREECAD_RPC_PORT/FREECAD_RPC_TOKEN not set — is the FreeCAD "
            "Claude Code Connector panel open?",
            True,
        )
    try:
        if name == "exec_python":
            resp = rpc_call(
                {"op": "exec", "token": RPC_TOKEN, "code": args.get("code", "")},
                timeout=EXEC_TIMEOUT,
            )
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


def protocol_error(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def handle_message(msg):
    """Validate at the protocol boundary; malformed messages never kill stdio."""
    if not isinstance(msg, dict):
        return protocol_error(None, -32600, "Request must be an object")
    msg_id = msg.get("id")
    if msg_id is not None and type(msg_id) not in (str, int):
        return protocol_error(None, -32600, "Invalid request id")
    method = msg.get("method")
    if msg.get("jsonrpc") != "2.0" or not isinstance(method, str):
        return protocol_error(msg_id, -32600, "Invalid JSON-RPC request")
    if "id" not in msg:
        return None  # Notifications never produce a response.
    params = msg.get("params", {})
    if not isinstance(params, dict):
        return protocol_error(msg_id, -32602, "params must be an object")
    if method == "initialize":
        requested = params.get("protocolVersion", "2024-11-05")
        if not isinstance(requested, str):
            return protocol_error(msg_id, -32602, "protocolVersion must be a string")
        result = {
            "protocolVersion": requested,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "freecad", "version": "0.2.0"},
        }
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return protocol_error(msg_id, -32602, "Tool name must be a string and arguments an object")
        if name == "exec_python" and not isinstance(arguments.get("code"), str):
            return protocol_error(msg_id, -32602, "code must be a string")
        if name == "screenshot" and any(
            type(arguments.get(key, default)) is not int
            for key, default in (("width", 1280), ("height", 800))
        ):
            return protocol_error(msg_id, -32602, "Screenshot dimensions must be integers")
        result = handle_tool_call(name, arguments)
    elif method == "ping":
        result = {}
    else:
        return protocol_error(msg_id, -32601, "Unknown method: %s" % method)
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def main():
    while True:
        line = sys.stdin.readline(MAX_MESSAGE_CHARS + 1)
        if not line:
            return
        if len(line) > MAX_MESSAGE_CHARS:
            while line and not line.endswith("\n"):
                line = sys.stdin.readline(MAX_MESSAGE_CHARS + 1)
            send(protocol_error(None, -32600, "Request too large"))
            continue
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except (ValueError, RecursionError):
            send(protocol_error(None, -32700, "Invalid JSON"))
            continue
        response = handle_message(msg)
        if response is not None:
            send(response)


if __name__ == "__main__":
    main()

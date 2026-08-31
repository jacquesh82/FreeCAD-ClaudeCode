"""FreeCAD addon "Claude Code Connector (unofficial)": a chat panel driving a
Claude Code agent that operates FreeCAD.

Architecture:
  panel.py         Qt dock panel (QDockWidget) — chat, prompt submission
  claude_runner.py QProcess wrapper around the `claude -p --output-format stream-json` CLI
  rpc_server.py    local TCP server INSIDE FreeCAD (GUI thread) executing Python;
                   requires a per-session token on every request
  mcp_bridge.py    standalone stdio MCP server launched by Claude Code; forwards
                   tool calls to rpc_server over TCP
"""

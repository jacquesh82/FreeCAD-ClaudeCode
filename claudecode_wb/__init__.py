"""FreeCAD addon "Claude Code Connector (unofficial)": a chat panel driving a
Claude Code agent that operates FreeCAD.

Architecture:
  panel.py         Qt dock panel (QDockWidget) — chat, attachments, consent,
                   opt-in dangerous modes
  approval.py      code-approval dialog and per-service data consent
  paths.py         per-user private directory for every file written for the agent
  runner_base.py   QProcess lifecycle shared by the agent runners
  claude_runner.py `claude -p --output-format stream-json` runner
  zcode_runner.py  ZCode CLI runner
  rpc_server.py    local TCP server INSIDE FreeCAD (GUI thread) executing Python
                   after user approval; revocable per-turn token on every request
  process_host.py  stdlib launcher owning each turn's process group / Windows job
  mcp_bridge.py    standalone stdio MCP server launched by the CLI; forwards
                   tool calls to rpc_server over TCP
"""

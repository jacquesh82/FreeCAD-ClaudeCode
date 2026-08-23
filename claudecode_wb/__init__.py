"""Addon FreeCAD « Claude Code » : volet de chat pilotant un agent Claude Code.

Architecture :
  panel.py         volet Qt (QDockWidget) — chat, envoi des prompts
  claude_runner.py QProcess autour du CLI `claude -p --output-format stream-json`
  rpc_server.py    serveur TCP local DANS FreeCAD (thread GUI) qui exécute du Python
  mcp_bridge.py    serveur MCP stdio autonome lancé par Claude Code ; relaie les
                   appels d'outils vers rpc_server via TCP
"""

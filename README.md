# FreeCAD Claude Code Workbench

A FreeCAD (≥ 1.1) addon that adds a dockable **Claude Code chat panel**: an AI
agent that drives FreeCAD for you — creating objects, editing documents,
inspecting the scene, and visually checking its own work with 3D-view
screenshots.

Authentication goes through your **Claude subscription**: the addon launches
the `claude` CLI (Claude Code) already logged in on your machine. No API key
required.

![Claude Code panel driving FreeCAD — Oldham coupling modeled by the agent](docs/screenshot.png)

*The agent modeling an Oldham coupling from a single chat request, then
verifying the result with a screenshot of the 3D view.*

## Features

- **Dockable chat panel** available in every workbench (docked on the right)
- **Subscription auth** — uses your existing Claude Code login, no API key
- **Agentic tool use** via MCP: the agent can
  - `exec_python` — run Python in the live FreeCAD session
  - `get_scene` — list open documents, objects, bounding boxes, errors
  - `screenshot` — capture the active 3D view and *see* it (visual feedback loop)
- **Conversation continuity** across turns (`--resume <session_id>`)
- **Safe by default** — only the three FreeCAD tools are allowed; an explicit
  checkbox enables full access (`--dangerously-skip-permissions`)
- Model selector (default / sonnet / opus / haiku), Stop button, New conversation

## Architecture

```
┌─ FreeCAD ────────────────────────────────┐
│  Qt dock panel (panel.py)                │
│    │ prompt            ▲ events          │
│    ▼                   │ stream-json     │
│  QProcess: claude -p --mcp-config …      │──── Claude subscription
│                                          │
│  rpc_server.py (TCP on 127.0.0.1, GUI    │
│  thread) ◄── mcp_bridge.py (stdio MCP    │
│              server spawned by claude)   │
└──────────────────────────────────────────┘
```

- Each chat turn runs `claude -p --output-format stream-json`; the panel
  streams assistant text and tool calls into the history view.
- `mcp_bridge.py` is a dependency-free (stdlib-only) MCP stdio server that
  forwards tool calls over TCP to `rpc_server.py`, a tiny QTcpServer running
  on FreeCAD's GUI thread — so agent code executes safely in the live
  document context.

## Requirements

- FreeCAD ≥ 1.1 (Qt6 / PySide6)
- [Claude Code](https://claude.com/claude-code) installed and logged in
  (`claude`, then `/login`) with a Claude subscription
- A system `python3` (the MCP bridge uses only the standard library)

## Installation

Clone this repository into your FreeCAD user `Mod` directory.

> **Note:** FreeCAD 1.1+ may use a *versioned* user directory. Check the real
> path with `FreeCAD.getUserAppDataDir()` in the Python console — on Linux it
> is typically `~/.local/share/FreeCAD/v1-1/`.

```bash
cd ~/.local/share/FreeCAD/v1-1/Mod   # adjust to your getUserAppDataDir()
git clone https://github.com/jacquesh82/FreeCAD-ClaudeCode.git ClaudeCodeWB
```

Restart FreeCAD. Then either:

1. Select the **Claude Code** workbench, or
2. Use the **Claude Code → Claude Code panel** menu entry.

The panel stays docked and usable across all workbenches.

## Usage

Type a request in the panel, e.g.:

> Model the 3 discs of an Oldham coupling, exploded view, and show me the result.

The agent inspects the scene, executes FreeCAD Python in small steps,
recomputes the document, and can verify its work with a screenshot.
`Enter` sends, `Shift+Enter` inserts a newline, **Stop** aborts the current
turn, **New conversation** resets the session.

## Security notes

- The RPC server listens on `127.0.0.1` only, on a random port chosen at
  startup.
- By default the agent is restricted to the three FreeCAD MCP tools
  (`--allowedTools`). Enabling the "allow all tools" checkbox lets the agent
  use files, bash, etc. — use at your own risk.
- The agent executes arbitrary Python inside FreeCAD by design: review what
  it does in the chat history, and save your documents before long tasks.

## License

MIT — see [LICENSE](LICENSE).

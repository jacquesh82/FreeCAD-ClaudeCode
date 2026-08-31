# Claude Code Connector for FreeCAD (unofficial)

An unofficial FreeCAD (≥ 1.1) addon that adds a dockable **Claude Code chat
panel**: an AI agent that drives FreeCAD for you — creating objects, editing
documents, inspecting the scene, and visually checking its own work with
3D-view screenshots. This is a third-party project and is not affiliated
with or endorsed by Anthropic.

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
- **Restricted toolset by default** — only the three FreeCAD tools are
  pre-authorized; an explicit checkbox widens access to all Claude Code
  tools (`--dangerously-skip-permissions`). Note: this is **not** a sandbox —
  see [Security notes](#security-notes).
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
- A system Python (`python3` on Linux/macOS, `python`/`py` on Windows — the
  MCP bridge uses only the standard library)

Windows, Linux and macOS are supported; Windows support is community-tested
(please report issues).

## Installation

### With the installer script (recommended)

The installer auto-detects your FreeCAD user `Mod` directory, checks the
required FreeCAD version (≥ 1.1), the `claude` CLI and a Python interpreter
for the MCP bridge, then installs the addon (symlink on Linux/macOS, copy on
Windows).

```bash
git clone https://github.com/jacquesh82/FreeCAD-ClaudeCode.git
cd FreeCAD-ClaudeCode
./install.sh                 # Linux/macOS
install.bat                  # Windows
```

Options: `--mod-dir <path>` if auto-detection fails, `--copy` to force a
copy instead of a symlink, `--uninstall` to remove the addon.

### Manual installation

Clone this repository into your FreeCAD user `Mod` directory.

> **Note:** FreeCAD 1.1+ may use a *versioned* user directory. Check the real
> path with `FreeCAD.getUserAppDataDir()` in the Python console — on Linux it
> is typically `~/.local/share/FreeCAD/v1-1/`.

```bash
cd ~/.local/share/FreeCAD/v1-1/Mod   # adjust to your getUserAppDataDir()
git clone https://github.com/jacquesh82/FreeCAD-ClaudeCode.git ClaudeCodeWB
```

Restart FreeCAD. Then either:

1. Select the **Claude Code Connector** workbench, or
2. Use the **Claude Code Connector → Claude Code Connector panel** menu entry.

The panel stays docked and usable across all workbenches.

## Usage

Type a request in the panel, e.g.:

> Model the 3 discs of an Oldham coupling, exploded view, and show me the result.

The agent inspects the scene, executes FreeCAD Python in small steps,
recomputes the document, and can verify its work with a screenshot.
`Enter` sends, `Shift+Enter` inserts a newline, **Stop** aborts the current
turn, **New conversation** resets the session.

## Data disclosure — read this before your first prompt

Nothing leaves your machine until you type a prompt. Once you do, your
prompt **and the tool results the agent produces are sent to Anthropic's
API** as part of the Claude conversation. That explicitly includes:

- `get_scene` output: document names, **file paths**, object labels/types
  and bounding boxes;
- **full screenshots of your 3D view** (the PNG is uploaded as an image);
- anything `exec_python` prints, and your prompt text itself.

If your designs are proprietary or confidential, assume the agent may share
the geometry it inspects with Anthropic. Do not use this addon on
confidential work if that disclosure is unacceptable.

## Security notes

- **This addon is not a sandbox.** Even in the default mode, the
  `exec_python` tool lets the agent run **arbitrary Python code inside
  FreeCAD with your user permissions**. Do not describe the default mode as
  "safe": it is a restricted toolset, not a security boundary. Enabling the
  "allow all tools" checkbox additionally grants file and shell access.
- The RPC server listens on `127.0.0.1` only, on a random port chosen at
  startup, and requires a per-session secret token: the token is generated
  at startup and passed only to the MCP bridge via the config's environment
  variables. Requests without the token are rejected.
- The agent executes arbitrary Python inside FreeCAD by design: review what
  it does in the chat history, and save your documents before long tasks.

## License

MIT — see [LICENSE](LICENSE).

#!/bin/sh
# Linux/macOS installer wrapper for the Claude Code Connector (unofficial).
set -e
dir="$(cd "$(dirname "$0")" && pwd)"
exec python3 "$dir/install.py" "$@"

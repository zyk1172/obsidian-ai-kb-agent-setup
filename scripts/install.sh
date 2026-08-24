#!/bin/sh
set -eu

REPO_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
ROOT="${OBSIDIAN_AI_HOME:-$HOME/.local/share/obsidian-ai-kb}"
BIN_DIR="${OBSIDIAN_AI_BIN_DIR:-$HOME/.local/bin}"
PYTHON="${PYTHON:-python3}"

mkdir -p "$ROOT" "$BIN_DIR"

if [ ! -d "$ROOT/.venv" ]; then
  "$PYTHON" -m venv "$ROOT/.venv"
fi

"$ROOT/.venv/bin/python" -m pip install --upgrade pip
"$ROOT/.venv/bin/python" -m pip install -r "$REPO_ROOT/requirements.txt"

install -m 0644 "$REPO_ROOT/mcp-server/obsidian_ai_kb.py" "$ROOT/obsidian_ai_kb.py"
install -m 0644 "$REPO_ROOT/requirements.txt" "$ROOT/requirements.txt"
install -m 0755 "$REPO_ROOT/mcp-server/obsidian-ai-kb" "$BIN_DIR/obsidian-ai-kb"

if [ ! -f "$ROOT/env" ]; then
  install -m 0600 "$REPO_ROOT/configs/env.example" "$ROOT/env"
  echo "Created $ROOT/env — review vault and embedding settings before first use."
else
  echo "Kept existing $ROOT/env unchanged."
fi

printf '\nInstalled:\n  server: %s\n  launcher: %s\n  config: %s\n' \
  "$ROOT/obsidian_ai_kb.py" "$BIN_DIR/obsidian-ai-kb" "$ROOT/env"
printf '\nNext: edit the config if needed, restart your MCP client, then run kb_status.\n'

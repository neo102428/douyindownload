#!/bin/sh
set -eu

CLAUDE_SRC="/Users/liujinrui/.local/lib/node_modules/@anthropic-ai/claude-code-darwin-arm64/claude"
CLAUDE_BIN="$HOME/.local/bin/claude"
ZSHRC="$HOME/.zshrc"
ZPROFILE="$HOME/.zprofile"

if [ ! -x "$CLAUDE_SRC" ]; then
  echo "Claude binary not found at: $CLAUDE_SRC" >&2
  exit 1
fi

mkdir -p "$HOME/.local/bin"
ln -sf "$CLAUDE_SRC" "$CLAUDE_BIN"

if [ -f "$ZSHRC" ] && ! grep -Fq 'export PATH="$HOME/.local/bin:$PATH"' "$ZSHRC"; then
  printf '\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$ZSHRC"
fi

if [ ! -f "$ZPROFILE" ]; then
  printf 'if [ -f "$HOME/.zshrc" ]; then\n  source "$HOME/.zshrc"\nfi\n' > "$ZPROFILE"
fi

echo "Installed: $CLAUDE_BIN"

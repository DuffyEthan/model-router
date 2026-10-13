#!/bin/sh
# model-router installer.
#   curl -fsSL https://raw.githubusercontent.com/DuffyEthan/model-router/main/install.sh | sh
# Options: --plugin-only | --cli-only
set -eu

REPO="DuffyEthan/model-router"
RAW="https://raw.githubusercontent.com/$REPO/main"
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
SHARE_DIR="${SHARE_DIR:-$HOME/.local/share/model-router}"

do_plugin=1
do_cli=1
case "${1:-}" in
  --plugin-only) do_cli=0 ;;
  --cli-only) do_plugin=0 ;;
  "") ;;
  *) echo "usage: install.sh [--plugin-only|--cli-only]" >&2; exit 2 ;;
esac

command -v claude >/dev/null 2>&1 || { echo "error: the 'claude' CLI is not on your PATH. Install Claude Code first." >&2; exit 1; }

if [ "$do_plugin" = 1 ]; then
  echo "==> Installing the Claude Code plugin (per-prompt routing inside Claude Code)"
  claude plugin install model-router --marketplace "$REPO"
fi

if [ "$do_cli" = 1 ]; then
  command -v python3 >/dev/null 2>&1 || { echo "error: python3 is required for the 'route' command." >&2; exit 1; }
  echo "==> Installing the 'route' command to $BIN_DIR"
  mkdir -p "$BIN_DIR" "$SHARE_DIR"
  here="$(cd "$(dirname "$0")" 2>/dev/null && pwd || true)"
  if [ -n "$here" ] && [ -f "$here/cli/route.py" ]; then
    cp "$here/cli/route.py" "$SHARE_DIR/route.py"      # running from a clone
  else
    curl -fsSL "$RAW/cli/route.py" -o "$SHARE_DIR/route.py"  # running via curl | sh
  fi
  chmod +x "$SHARE_DIR/route.py"
  ln -sf "$SHARE_DIR/route.py" "$BIN_DIR/route"
  case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) echo "note: $BIN_DIR is not on your PATH; add it to use 'route'." ;;
  esac
fi

echo "==> Done."
[ "$do_plugin" = 1 ] && echo "    Start Claude Code and type /router to check the plugin is active."
[ "$do_cli" = 1 ] && echo "    Try: route --dry-run \"what does ls -la do\""
exit 0

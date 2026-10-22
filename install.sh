#!/bin/sh
# model-router installer.
#   curl -fsSL https://raw.githubusercontent.com/DuffyEthan/model-router/main/install.sh | sh
# Options: --plugin-only | --cli-only
#
# The 'model-router' CLI (cli/route.py) is always downloaded from the pinned ref
# below, and its SHA-256 is checked against the value hard-coded here before it
# is installed.
# It is never copied from the current directory, so `curl | sh` and running
# from a checkout install the same verified file.
set -eu

# --- Pinned release ------------------------------------------------------------
# Update both values together on each release. To get them, run
#   make release-checksums TAG=vX.Y.Z
# and paste the output here.
MR_REF="v0.2.2"
MR_ROUTE_PY_SHA256="1852f7dafab62c31d93da58cc86c20f671af8c1c0ea4c57043e4aa0890bc4165"
# -------------------------------------------------------------------------------

REPO="DuffyEthan/model-router"
# Download base URL. Overridable for mirrors and tests; the checksum above is
# enforced whatever the source.
MR_BASE_URL="${MR_BASE_URL:-https://raw.githubusercontent.com/$REPO}"
BIN_DIR="${BIN_DIR:-$HOME/.local/bin}"
SHARE_DIR="${SHARE_DIR:-$HOME/.local/share/model-router}"

die() { echo "error: $*" >&2; exit 1; }

have() { command -v "$1" >/dev/null 2>&1; }

# Print the SHA-256 of file $1. Fails if no hashing tool is available.
sha256_of() {
  if have sha256sum; then
    sum=$(sha256sum <"$1")
  elif have shasum; then
    sum=$(shasum -a 256 <"$1")
  else
    return 1
  fi
  printf '%s\n' "${sum%% *}"
}

do_plugin=1
do_cli=1
case "${1:-}" in
  --plugin-only) do_cli=0 ;;
  --cli-only) do_plugin=0 ;;
  "") ;;
  *) echo "usage: install.sh [--plugin-only|--cli-only]" >&2; exit 2 ;;
esac

have claude || die "the 'claude' CLI is not on your PATH. Install Claude Code first."

if [ "$do_cli" = 1 ]; then
  # Check prerequisites before changing anything.
  have python3 || die "python3 is required for the 'model-router' command."
  have curl || die "curl is required to download the 'model-router' command."
  have sha256sum || have shasum \
    || die "sha256sum or shasum is required to verify the download; refusing to install unverified code."
fi

if [ "$do_plugin" = 1 ]; then
  echo "==> Installing the Claude Code plugin (per-prompt routing inside Claude Code)"
  claude plugin install model-router --marketplace "$REPO"
fi

if [ "$do_cli" = 1 ]; then
  echo "==> Installing the 'model-router' command ($MR_REF) to $BIN_DIR"
  mkdir -p "$BIN_DIR" "$SHARE_DIR"
  tmp=$(mktemp "$SHARE_DIR/.route.py.XXXXXX")
  trap 'rm -f "$tmp"' EXIT
  trap 'exit 1' HUP INT TERM
  curl -fsSL "$MR_BASE_URL/$MR_REF/cli/route.py" -o "$tmp" \
    || die "could not download route.py ($MR_REF) from $MR_BASE_URL."
  actual=$(sha256_of "$tmp") || die "could not compute the SHA-256 of the download."
  if [ "$actual" != "$MR_ROUTE_PY_SHA256" ]; then
    echo "error: checksum mismatch for route.py ($MR_REF); nothing was installed." >&2
    echo "  expected: $MR_ROUTE_PY_SHA256" >&2
    echo "  actual:   $actual" >&2
    exit 1
  fi
  chmod 755 "$tmp"
  mv -f "$tmp" "$SHARE_DIR/route.py"
  ln -sf "$SHARE_DIR/route.py" "$BIN_DIR/model-router"
  # Before v0.2.2 the command was 'route', which the system's own 'route' tool
  # usually shadows. An old link points at the same file, so it keeps working
  # until it's removed.
  if [ -L "$BIN_DIR/route" ] && [ "$(readlink "$BIN_DIR/route")" = "$SHARE_DIR/route.py" ]; then
    echo "note: the command is now 'model-router'. The old 'route' link still works but is"
    echo "      deprecated; remove it with: rm \"$BIN_DIR/route\""
  fi
  case ":$PATH:" in
    *":$BIN_DIR:"*)
      found=$(command -v model-router || true)
      if [ "$found" != "$BIN_DIR/model-router" ]; then
        echo "note: another 'model-router' comes first on your PATH ($found);"
        echo "      run $BIN_DIR/model-router or put $BIN_DIR earlier in PATH."
      fi
      ;;
    *) echo "note: $BIN_DIR is not on your PATH; add it to use 'model-router'." ;;
  esac
fi

echo "==> Done."
[ "$do_plugin" = 1 ] && echo "    Start Claude Code and type /router to check the plugin is active."
[ "$do_cli" = 1 ] && echo "    Try: model-router --dry-run \"what does ls -la do\""
exit 0

#!/usr/bin/env bash
# Tokenometro installer for Linux, WSL or macOS: install.py does the work (it also runs on Windows).
#   bash install.sh               -> install/update, register the SessionStart hook and start it
#   bash install.sh --statusline  -> also use statusline.py as statusLine (to see plan usage %)
#   bash install.sh --uninstall   -> remove the hook and our statusLine (keeps files and data)
command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }
exec python3 "$(cd "$(dirname "$0")" && pwd)/install.py" "$@"

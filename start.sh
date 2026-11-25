#!/usr/bin/env bash
# SessionStart hook of earlier installs: start.py does the work now (also on Windows).
exec python3 "$(cd "$(dirname "$0")" && pwd)/start.py" "$@"

#!/usr/bin/env bash
# Tokenometro installer (Linux, WSL or macOS; needs python3 >= 3.8, no dependencies).
#   bash install.sh               -> install/update, register the SessionStart hook and start it
#   bash install.sh --statusline  -> also use statusline.py as statusLine (to see plan usage %)
#   bash install.sh --uninstall   -> remove the hook and our statusLine (keeps files and data)
# It only reads ~/.claude/projects and listens on 127.0.0.1: nothing leaves your machine.
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.local/share/tokenometro"
OLD="$HOME/.local/share/claude-tokens"          # folder used by earlier versions
SETTINGS="$HOME/.claude/settings.json"
HOOK='"$HOME/.local/share/tokenometro/start.sh"'
HOOK_OLD='"$HOME/.local/share/claude-tokens/start.sh"'
STATUS='python3 "$HOME/.local/share/tokenometro/statusline.py"'
PORT="${TOKENOMETRO_PORT:-${CLAUDE_TOKENS_PORT:-7878}}"
# a custom port has to travel with the hook, or the dashboard would start on 7878 next session
[ "$PORT" = "7878" ] || HOOK="TOKENOMETRO_PORT=$PORT $HOOK"
NOTES=()
PORT_BUSY=0

command -v python3 >/dev/null || { echo "python3 is required"; exit 1; }

stop_tracker() {  # only the process listening on the port, never by name
  local pid=""
  if command -v ss >/dev/null 2>&1; then
    pid=$(ss -ltnpH "sport = :$PORT" 2>/dev/null | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2 || true)
  elif command -v lsof >/dev/null 2>&1; then
    pid=$(lsof -ti "tcp:$PORT" -sTCP:LISTEN 2>/dev/null | head -1 || true)
  fi
  [ -n "$pid" ] || return 0
  # never kill another program that happens to use the port
  if ps -o args= -p "$pid" 2>/dev/null | grep -q 'tracker\.py'; then
    kill "$pid" 2>/dev/null || true; sleep 1
  else
    echo "Port $PORT is used by another program (pid $pid); it was left running. Set TOKENOMETRO_PORT to use another port."
    PORT_BUSY=1
  fi
}

# $1 = add | remove ; $2 = 1 to also manage the statusLine
edit_settings() {
  mkdir -p "$HOME/.claude"
  [ -f "$SETTINGS" ] && cp "$SETTINGS" "$SETTINGS.bak-tokenometro-$(date +%Y%m%d%H%M%S)"
  python3 - "$SETTINGS" "$1" "${2:-0}" "$HOOK" "$HOOK_OLD" "$STATUS" <<'EOF'
import json, os, sys
path, mode, with_status, hook, hook_old, status = sys.argv[1:]
if mode == "remove" and not os.path.exists(path):
    sys.exit(0)
s = json.load(open(path, encoding="utf-8")) if os.path.exists(path) else {}
if not isinstance(s, dict):
    sys.exit("settings.json is not a JSON object")
if not isinstance(s.get("hooks"), dict):
    s["hooks"] = {}
ss = s["hooks"].get("SessionStart")
ss = s["hooks"]["SessionStart"] = ss if isinstance(ss, list) else []
ours = lambda h: isinstance(h, dict) and isinstance(h.get("command"), str) and \
    h["command"].endswith(("/tokenometro/start.sh\"", "/claude-tokens/start.sh\""))
for e in ss:
    if isinstance(e, dict) and isinstance(e.get("hooks"), list):
        e["hooks"] = [h for h in e["hooks"] if not ours(h)]
ss[:] = [e for e in ss if not isinstance(e, dict) or e.get("hooks") or "hooks" not in e]
if mode == "add":
    ss.append({"matcher": "startup|resume", "hooks": [{"type": "command", "command": hook, "timeout": 5}]})
if not ss:
    del s["hooks"]["SessionStart"]
if not s["hooks"]:
    del s["hooks"]
msg = ""
cur = (s.get("statusLine") or {}).get("command")
if with_status == "1" and mode == "add":
    if cur in (None, status):
        s["statusLine"] = {"type": "command", "command": status}
    else:
        msg = "statusline-taken"
if mode == "remove" and cur == status:
    del s["statusLine"]
text = json.dumps(s, indent=2, ensure_ascii=False) + "\n"
json.loads(text)
tmp = path + ".tmp-tokenometro"
with open(tmp, "w", encoding="utf-8") as f:  # write aside and swap: never a half-written settings.json
    f.write(text)
os.replace(tmp, path)
print(msg)
EOF
}

if [ "${1:-}" = "--uninstall" ]; then
  edit_settings remove 1 >/dev/null || { echo "Could not edit $SETTINGS (is it valid JSON?)" >&2; exit 1; }
  stop_tracker
  echo "Hook removed and Tokenometro stopped. To delete everything: rm -rf \"$DEST\""
  exit 0
fi

stop_tracker
mkdir -p "$DEST"
# Migration from ~/.local/share/claude-tokens: keep data and settings
if [ -d "$OLD" ] && [ ! -f "$DEST/usage.db" ]; then
  for f in usage.db usage.db-wal usage.db-shm clients.json clientes.json prices.json plan.json; do
    [ -f "$OLD/$f" ] && cp "$OLD/$f" "$DEST/"
  done
  NOTES+=("- Data and settings copied from $OLD; once everything works you can delete that folder.")
fi
# running the installed copy (e.g. install.sh --statusline from $DEST): nothing to copy onto itself
if [ "$(cd "$SRC" && pwd -P)" != "$(cd "$DEST" && pwd -P)" ]; then
  cp "$SRC/tracker.py" "$SRC/start.sh" "$SRC/statusline.py" "$SRC/install.sh" "$SRC/clients.example.json" "$DEST/"
  for f in README.md README.es.md LICENSE; do [ -f "$SRC/$f" ] && cp "$SRC/$f" "$DEST/"; done
  [ -f "$DEST/prices.json" ] || cp "$SRC/prices.json" "$DEST/"
fi
[ -f "$DEST/clients.json" ] || [ -f "$DEST/clientes.json" ] || cp "$SRC/clients.example.json" "$DEST/clients.json"
chmod +x "$DEST/start.sh" "$DEST/tracker.py" "$DEST/install.sh" "$DEST/statusline.py"

WITH_STATUS=0; [ "${1:-}" = "--statusline" ] && WITH_STATUS=1
MSG=$(edit_settings add "$WITH_STATUS") || { echo "Could not edit $SETTINGS (is it valid JSON?). Nothing was registered; the files are in $DEST." >&2; exit 1; }
if [ "$MSG" = "statusline-taken" ]; then
  NOTES+=("- You already have a statusLine and it was left untouched. To let the dashboard see plan usage, chain it:")
  NOTES+=("    python3 \"\$HOME/.local/share/tokenometro/statusline.py\" --tee | <your current command>")
fi

echo "Reading your Claude Code history (the first run can take a while)…"
python3 "$DEST/tracker.py" ingest >/dev/null
[ "${TOKENOMETRO_NO_START:-}" = "1" ] || [ "$PORT_BUSY" = "1" ] || "$DEST/start.sh"
cat <<MSG

Done. Dashboard: http://127.0.0.1:$PORT  (starts by itself when you open Claude Code).
- Clients and projects: $DEST/clients.json  · prices: $DEST/prices.json
- Backup of your previous settings.json: $SETTINGS.bak-tokenometro-*
- Uninstall: bash $DEST/install.sh --uninstall
MSG
for n in "${NOTES[@]:-}"; do [ -n "$n" ] && echo "$n"; done
exit 0

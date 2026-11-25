#!/usr/bin/env bash
# SessionStart hook: starts Tokenometro unless it is already listening. Never blocks or prints anything
# (whatever a SessionStart hook prints goes into the context and would cost tokens).
D="$(cd "$(dirname "$0")" && pwd)"
PORT="${TOKENOMETRO_PORT:-${CLAUDE_TOKENS_PORT:-7878}}"
(exec 3<>/dev/tcp/127.0.0.1/"$PORT") 2>/dev/null && exit 0
# two sessions opening at once: only the one that creates the lock starts it (a lock older than a minute is stale)
LOCK="$D/.starting"
if ! mkdir "$LOCK" 2>/dev/null; then
  [ -n "$(find "$LOCK" -maxdepth 0 -mmin +1 2>/dev/null)" ] || exit 0
fi
( sleep 20; rmdir "$LOCK" 2>/dev/null ) >/dev/null 2>&1 &
# keep the log small: start over when it passes 1 MB
[ -f "$D/tracker.log" ] && [ "$(wc -c <"$D/tracker.log")" -gt 1048576 ] && mv -f "$D/tracker.log" "$D/tracker.log.1"
if command -v setsid >/dev/null 2>&1; then
  setsid nohup python3 "$D/tracker.py" serve >>"$D/tracker.log" 2>&1 </dev/null &
else
  nohup python3 "$D/tracker.py" serve >>"$D/tracker.log" 2>&1 </dev/null &
fi
exit 0

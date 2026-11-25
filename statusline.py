#!/usr/bin/env python3
"""Claude Code statusLine: records plan usage (rate_limits) for Tokenometro and prints it, with a forecast:
"5h 46% · runs out 16:40" when, at the pace since the window opened, you would hit 100% before it resets.

Claude Code sends a JSON on stdin with rate_limits.five_hour / seven_day (used_percentage, resets_at).
It is saved to plan.json, next to this script, in the same format as the sub-meter plugin.
If you already have a statusLine, put this script in front of it: python3 statusline.py --tee | your-command
"""
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plan.json")
WINDOW = {"five_hour": timedelta(hours=5), "seven_day": timedelta(days=7)}
LANG = (os.environ.get("TOKENOMETRO_LANG") or os.environ.get("CLAUDE_TOKENS_LANG") or "en")[:2].lower()


def runs_out(pct, reset, kind, now=None):
    """When the window would reach 100% at the pace used since it opened; None if it lasts until the reset,
    or if it opened too recently for the pace to mean anything (the first 10% of the window)."""
    now = now or datetime.now(timezone.utc)
    start = reset - WINDOW[kind]
    elapsed = (now - start).total_seconds()
    if pct <= 0 or pct >= 100 or elapsed < WINDOW[kind].total_seconds() * 0.1:
        return None
    end = now + timedelta(seconds=elapsed * (100 - pct) / pct)
    return end if end < reset else None


def label(kind, pct, end):
    es = LANG == "es"
    name = {"five_hour": "5h", "seven_day": "semana" if es else "week"}[kind]
    text = f"{name} {pct:.0f}%"
    if end:
        local = end.astimezone()
        when = local.strftime("%H:%M")
        if kind == "seven_day":
            days = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom") if es else ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
            when = f"{days[local.weekday()]} {when}"
        text += f" · {'se agota' if es else 'runs out'} {when}"
    return text


def main():
    for stream in (sys.stdin, sys.stdout):  # Claude Code speaks UTF-8; a Windows pipe would default to cp1252
        try:
            stream.reconfigure(encoding="utf-8")
        except AttributeError:
            pass
    raw = sys.stdin.read()
    tee = "--tee" in sys.argv
    try:
        d = json.loads(raw or "{}")
    except ValueError:
        d = {}
    rl = d.get("rate_limits") or {}
    limits = []
    for kind in ("five_hour", "seven_day"):
        w = rl.get(kind) or {}
        if w.get("used_percentage") is None:
            continue
        try:
            reset = datetime.fromtimestamp(float(w.get("resets_at")), timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError):
            continue
        limits.append({"kind": kind, "percentUsed": w["used_percentage"], "resetsAt": reset.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                       "_reset": reset})
    if limits:
        try:
            # one temp file per call: Claude Code can run several statusLines at once
            fd, tmp = tempfile.mkstemp(prefix=".plan-", suffix=".tmp", dir=os.path.dirname(OUT))
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump({"limits": [{k: v for k, v in x.items() if k != "_reset"} for x in limits],
                               "at": int(time.time() * 1000)}, f)
                os.replace(tmp, OUT)
            except BaseException:
                os.unlink(tmp)
                raise
        except OSError:
            pass
    if tee:
        sys.stdout.write(raw)
        return
    parts = []
    for x in limits:
        try:
            pct = float(x["percentUsed"])
        except (TypeError, ValueError):
            continue
        parts.append(label(x["kind"], pct, runs_out(pct, x["_reset"], x["kind"])))
    print("  ".join(parts))


if __name__ == "__main__":
    main()

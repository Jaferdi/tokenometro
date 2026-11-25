#!/usr/bin/env python3
"""Claude Code statusLine: records plan usage (rate_limits) for Tokenometro and prints it.

Claude Code sends a JSON on stdin with rate_limits.five_hour / seven_day (used_percentage, resets_at).
It is saved to plan.json, next to this script, in the same format as the sub-meter plugin.
If you already have a statusLine, put this script in front of it: python3 statusline.py --tee | your-command
"""
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plan.json")


def main():
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
        reset = w.get("resets_at")
        try:
            reset = datetime.fromtimestamp(float(reset), timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        except (TypeError, ValueError):
            reset = None
        if reset:
            limits.append({"kind": kind, "percentUsed": w["used_percentage"], "resetsAt": reset})
    if limits:
        try:
            # one temp file per call: Claude Code can run several statusLines at once
            fd, tmp = tempfile.mkstemp(prefix=".plan-", suffix=".tmp", dir=os.path.dirname(OUT))
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump({"limits": limits, "at": int(time.time() * 1000)}, f)
                os.replace(tmp, OUT)
            except BaseException:
                os.unlink(tmp)
                raise
        except OSError:
            pass
    if tee:
        sys.stdout.write(raw)
        return
    names = {"five_hour": "5h", "seven_day": "week"}
    print("  ".join(f"{names[x['kind']]} {float(x['percentUsed']):.0f}%" for x in limits))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Try Tokenometro with made-up data, without installing anything or touching your Claude Code setup.

    python3 demo.py                 # dashboard at http://127.0.0.1:7879 (Ctrl+C to stop)
    python3 demo.py --port 8000 --lang es

Everything happens in a temporary folder that is deleted on exit: a fake ~/.claude with a month of
invented sessions, its own database and its own settings. Your real logs and usage.db are never read.
"""
import argparse
import json
import os
import random
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

SRC = os.path.dirname(os.path.abspath(__file__))

PROJECTS = [
    # (folder, weight, typical model, typical work)
    ("/home/demo/code/acme/checkout-api", 5, "claude-opus-5-5", "code"),
    ("/home/demo/code/acme/web-store", 4, "claude-opus-5-5", "code"),
    ("/home/demo/code/blue-harbor/mobile-app", 3, "claude-sonnet-5-5", "code"),
    ("/home/demo/code/blue-harbor/docs", 2, "claude-opus-5-5", "docs"),
    ("/home/demo/notes", 1, "claude-haiku-5-5", "chat"),
]
TITLES = {
    "code": ["Fix flaky payment tests", "Add refund endpoint", "Migrate auth to OAuth", "Speed up product search",
             "Cart totals rounding bug", "Refactor order service", "Dark mode for settings", "Upgrade framework",
             "Push notifications", "Offline cache for catalogue", "Rate limiting middleware", "CSV export of orders"],
    "docs": ["Write API reference", "Release notes 2.4", "Onboarding guide", "Review README"],
    "chat": ["Plan the week", "Summarise meeting notes", "Compare hosting options"],
}
FILES = ["src/orders/service.ts", "src/payments/stripe.ts", "src/cart/totals.ts", "src/auth/oauth.ts",
         "tests/payments.spec.ts", "src/search/index.ts", "app/screens/Settings.tsx", "docs/api.md", "README.md"]
COMMANDS = ["npm test", "npm run lint", "git status", "git diff --stat", "npm run build", "pytest -q",
            "docker compose up -d db", "npx tsc --noEmit"]
DOC_EXT = (".md", ".json", ".yaml")


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


class Session:
    def __init__(self, rnd, sid, cwd, model, kind, start, turns):
        self.rnd, self.sid, self.cwd, self.model, self.kind = rnd, sid, cwd, model, kind
        self.t, self.turns, self.ctx, self.lines, self.n = start, turns, 18_000, [], 0

    def _id(self, p):
        self.n += 1
        return f"{p}_{self.sid[:8]}{self.n:05d}"

    def _base(self, typ, **kw):
        d = {"type": typ, "sessionId": self.sid, "cwd": self.cwd, "timestamp": iso(self.t), "uuid": self._id("u")}
        d.update(kw)
        return d

    def _assistant(self, tools, model=None, side=False, agent=None, out=None):
        r = self.rnd
        fresh = r.randint(600, 5_000)
        written = r.randint(1_500, 9_000)
        usage = {"input_tokens": r.randint(3, 40), "cache_read_input_tokens": self.ctx,
                 "cache_creation_input_tokens": written,
                 "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": written},
                 "output_tokens": out or r.randint(250, 2_400)}
        content = [{"type": "tool_use", "id": tid, "name": n, "input": inp} for tid, n, inp in tools]
        d = self._base("assistant", message={"id": self._id("msg"), "model": model or self.model, "usage": usage,
                                            "content": content or [{"type": "text", "text": "Done."}]})
        if side:
            d.update(isSidechain=True, agentId=agent)
        self.ctx += fresh + written // 3
        return d

    def _tick(self, a=4, b=50):
        self.t += timedelta(seconds=self.rnd.randint(a, b))

    def build(self, compact_at=300_000, pause_at=None, subagent=None, huge_read=False, title=None):
        r = self.rnd
        out = [{"type": "custom-title", "sessionId": self.sid, "customTitle": title or r.choice(TITLES[self.kind])}]
        agents = []
        for turn in range(self.turns):
            if pause_at and turn == pause_at:
                self.t += timedelta(hours=r.randint(3, 9))  # resumed later: the cache has to be written again
            prompt = r.choice(["Let's continue", "Can you check the failing test?", "Now add tests for it",
                               "Why is this slow?", "Ship it", "Explain this function", "Tidy this up"])
            out.append(self._base("user", message={"role": "user", "content": prompt}))
            self._tick(2, 6)
            for _step in range(r.randint(1, 5)):
                tools = []
                if self.kind != "chat":
                    name = r.choice(["Read", "Read", "Edit", "Bash", "Bash", "Grep"] if self.kind == "code"
                                    else ["Read", "Edit", "Grep", "WebSearch"])
                    f = r.choice([x for x in FILES if (x.endswith(DOC_EXT) == (self.kind == "docs"))] or FILES)
                    inp = {"Read": {"file_path": f"{self.cwd}/{f}"}, "Edit": {"file_path": f"{self.cwd}/{f}"},
                           "Bash": {"command": r.choice(COMMANDS)}, "Grep": {"pattern": "TODO", "path": self.cwd},
                           "WebSearch": {"query": "best practices " + f}}[name]
                    tools.append((self._id("toolu"), name, inp))
                out.append(self._assistant(tools))
                self._tick()
                for tid, name, inp in tools:
                    size = r.randint(400, 9_000)
                    if huge_read and name == "Read" and turn == 1:
                        size, huge_read = 240_000, False
                    err = name == "Bash" and r.random() < 0.12
                    out.append(self._base("user", message={"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": tid, "is_error": err,
                         "content": [{"type": "text", "text": "x" * size}]}]}))
                    self.ctx += size // 4
                    self._tick(1, 20)
                if self.ctx > compact_at:
                    out.append(self._base("system", subtype="compact_boundary", content="Conversation compacted"))
                    self.ctx = r.randint(25_000, 45_000)
            if subagent and turn == self.turns // 2:
                aid = f"a{self.sid[:12]}"
                lines = []
                for _ in range(r.randint(6, 14)):
                    lines.append(self._assistant([(self._id("toolu"), "Grep", {"pattern": "export", "path": self.cwd})],
                                                 model=subagent, side=True, agent=aid))
                    self._tick(2, 12)
                agents.append((aid, lines))
            self._tick(30, 240)
        return out, agents


def generate(home, seed=7):
    rnd = random.Random(seed)
    now = datetime.now(timezone.utc)
    root = os.path.join(home, ".claude", "projects")
    total = 0
    for day in range(30, -1, -1):
        n = 0 if (now - timedelta(days=day)).weekday() >= 5 and rnd.random() < 0.7 else rnd.randint(2, 6)
        for i in range(n):
            cwd, _w, model, kind = rnd.choices(PROJECTS, weights=[p[1] for p in PROJECTS])[0]
            if kind == "code" and model.startswith("claude-opus") and rnd.random() < 0.25:
                model = "claude-sonnet-5-5"
            start = (now - timedelta(days=day)).replace(hour=rnd.randint(8, 18), minute=rnd.randint(0, 59))
            if start > now - timedelta(minutes=50):
                start = now - timedelta(minutes=rnd.randint(50, 240))
            sid = "%08x-%04x-4%03x-a%03x-%012x" % (rnd.getrandbits(32), rnd.getrandbits(16), rnd.getrandbits(12),
                                                   rnd.getrandbits(12), rnd.getrandbits(48))
            turns = {"code": rnd.randint(6, 28), "docs": rnd.randint(4, 12), "chat": rnd.randint(2, 6)}[kind]
            s = Session(rnd, sid, cwd, model, kind, start, turns)
            lines, agents = s.build(
                pause_at=rnd.randint(2, turns - 1) if turns > 4 and rnd.random() < 0.2 else None,
                subagent=rnd.choice(["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]) if rnd.random() < 0.35 else None,
                huge_read=rnd.random() < 0.1)
            write_session(root, cwd, sid, lines, agents)
            total += 1
    # two sessions still open, so "Right now" has something to show
    for cwd, model, ago, title in (("/home/demo/code/acme/checkout-api", "claude-opus-5-5", 95, "Add refund endpoint"),
                                   ("/home/demo/code/blue-harbor/mobile-app", "claude-sonnet-5-5", 40, "Offline cache for catalogue")):
        sid = "%08x-0000-4000-a000-%012x" % (rnd.getrandbits(32), rnd.getrandbits(48))
        s = Session(rnd, sid, cwd, model, "code", now - timedelta(minutes=ago), 40)
        lines, agents = s.build(title=title)
        lines = [x for x in lines if x.get("timestamp", "") <= iso(now - timedelta(minutes=2))] or lines
        write_session(root, cwd, sid, lines, agents)
        total += 1
    return total


AGENTS = [("Explore", "Find where orders are created"), ("Explore", "Map the payment flow"),
          ("general-purpose", "Check test coverage of cart"), ("Plan", "Plan the OAuth migration"),
          ("Explore", "List endpoints without auth"), ("general-purpose", "Compare caching libraries")]


def rnd_agent(aid):
    kind, desc = AGENTS[int(aid[1:9], 16) % len(AGENTS)]
    return {"agentType": kind, "description": desc}


def write_session(root, cwd, sid, lines, agents):
    d = os.path.join(root, cwd.replace("/", "-"))
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, sid + ".jsonl"), "w") as f:
        f.writelines(json.dumps(x, separators=(",", ":")) + "\n" for x in lines)
    for aid, al in agents:
        sd = os.path.join(d, sid, "subagents")
        os.makedirs(sd, exist_ok=True)
        with open(os.path.join(sd, f"agent-{aid}.jsonl"), "w") as f:
            f.writelines(json.dumps(x, separators=(",", ":")) + "\n" for x in al)
        with open(os.path.join(sd, f"agent-{aid}.meta.json"), "w") as f:
            json.dump(rnd_agent(aid), f)


def plan_reading(app):
    now = datetime.now(timezone.utc)
    z = lambda t: t.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    with open(os.path.join(app, "plan.json"), "w") as f:
        json.dump({"at": int(time.time() * 1000), "limits": [
            {"kind": "five_hour", "percentUsed": 46, "resetsAt": z(now + timedelta(hours=2, minutes=10))},
            {"kind": "seven_day", "percentUsed": 61, "resetsAt": z(now + timedelta(days=2, hours=5))}]}, f)


def main():
    ap = argparse.ArgumentParser(description="Tokenometro with made-up data")
    ap.add_argument("--port", type=int, default=7879)
    ap.add_argument("--lang", choices=("en", "es"), default="en")
    ap.add_argument("--currency", choices=("USD", "EUR"), default="USD")
    a = ap.parse_args()

    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", a.port)) == 0:
            sys.exit(f"Port {a.port} is already in use: try  python3 demo.py --port {a.port + 1}")
    tmp = tempfile.mkdtemp(prefix="tokenometro-demo-")
    home, app = os.path.join(tmp, "home"), os.path.join(tmp, "app")
    os.makedirs(os.path.join(home, ".claude"))
    os.makedirs(app)
    for f in ("tracker.py", "prices.json"):
        shutil.copy(os.path.join(SRC, f), app)
    with open(os.path.join(app, "clients.json"), "w") as f:
        json.dump({"clients": {"acme": ["/code/acme/"], "blue-harbor": ["/code/blue-harbor/"]},
                   "cut": (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d"),
                   "budget": {"day": 30, "week": 150, "month": 600}}, f)
    with open(os.path.join(home, ".claude", "settings.json"), "w") as f:
        json.dump({}, f)
    n = generate(home)
    plan_reading(app)
    env = dict(os.environ, HOME=home, TOKENOMETRO_PORT=str(a.port), TOKENOMETRO_LANG=a.lang,
               TOKENOMETRO_CURRENCY=a.currency)
    for k in [k for k in env if k.startswith("CLAUDE_TOKENS_")]:
        del env[k]
    subprocess.run([sys.executable, os.path.join(app, "tracker.py"), "ingest"], env=env,
                   stdout=subprocess.DEVNULL, check=True)
    print(f"Demo with {n} made-up sessions: http://127.0.0.1:{a.port}  (Ctrl+C to stop; nothing is kept)")
    p = subprocess.Popen([sys.executable, os.path.join(app, "tracker.py"), "serve"], env=env)
    # stopped with Ctrl+C or kill: stop the server too and delete the made-up data
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        p.wait()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        if p.poll() is None:
            p.terminate()
            p.wait()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()

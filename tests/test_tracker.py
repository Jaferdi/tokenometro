"""Tests for Tokenometro. Standard library only: python3 -m unittest discover -s tests

Each test case gets its own copy of tracker.py in a temporary folder and a fake HOME, so nothing
reads or writes your real ~/.claude or usage.db.
"""
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import demo  # noqa: E402

PRICE_FIELDS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")


def read_text(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def read(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_tracker(tmp, clients=None):
    """Import a private copy of tracker.py with HOME pointing at tmp/home."""
    home, app = os.path.join(tmp, "home"), os.path.join(tmp, "app")
    os.makedirs(os.path.join(home, ".claude", "projects"), exist_ok=True)
    os.makedirs(app, exist_ok=True)
    for f in ("tracker.py", "prices.json"):
        shutil.copy(os.path.join(ROOT, f), app)
    if clients is not None:
        with open(os.path.join(app, "clients.json"), "w") as f:
            json.dump(clients, f)
    old = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
    os.environ["HOME"] = os.environ["USERPROFILE"] = home  # Windows reads ~ from USERPROFILE
    for k in [k for k in os.environ if k.startswith(("TOKENOMETRO_", "CLAUDE_TOKENS_")) or k == "CLAUDE_CONFIG_DIR"]:
        del os.environ[k]
    try:
        spec = importlib.util.spec_from_file_location(f"tracker_{abs(hash(tmp))}", os.path.join(app, "tracker.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return mod, home, app


def record(sid, ts, mid, model="claude-opus-5-5", inp=1_000_000, out=0, cr=0, cwd="/work/demo", first_type=False):
    d = {"sessionId": sid, "timestamp": ts, "cwd": cwd, "type": "assistant",
         "message": {"id": mid, "model": model, "usage": {"input_tokens": inp, "output_tokens": out,
                                                         "cache_read_input_tokens": cr}}}
    if first_type:
        d = {"type": d.pop("type"), **d}
    return json.dumps(d)


class Base(unittest.TestCase):
    clients = None

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tokenometro-test-")
        self.t, self.home, self.app = load_tracker(self.tmp, self.clients)
        self.proj = os.path.join(self.home, ".claude", "projects", "-work-demo")
        os.makedirs(self.proj)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, lines, mode="w"):
        with open(os.path.join(self.proj, name), mode) as f:
            f.write("".join(x + "\n" for x in lines))

    def cost(self, model):
        con = self.t.connect()
        try:
            C = self.t.cost_expr(self.t.load_prices())
            return con.execute(f"SELECT SUM({C}) FROM msgs WHERE model=?", (model,)).fetchone()[0]
        finally:
            con.close()


class PricesTest(unittest.TestCase):
    def test_every_model_has_every_field(self):
        prices = read(os.path.join(ROOT, "prices.json"))
        for name, p in prices.items():
            if name.startswith("_") and name != "_default":
                continue
            for f in PRICE_FIELDS:
                self.assertIsInstance(p.get(f), (int, float), f"{name}.{f}")
                self.assertGreaterEqual(p[f], 0, f"{name}.{f}")

    def test_currency_block(self):
        cur = read(os.path.join(ROOT, "prices.json"))["_currency"]
        self.assertIn(cur["default"], ("USD", "EUR"))
        self.assertGreater(cur["eur_per_usd"], 0)


class CostTest(Base):
    def test_longest_model_name_wins(self):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        self.write("s.jsonl", [record("s1", ts, "m1", "claude-opus-5-5"), record("s1", ts, "m2", "claude-opus-5")])
        self.t.ingest()
        prices = self.t.load_prices()
        self.assertAlmostEqual(self.cost("claude-opus-5-5"), prices["claude-opus-5-5"]["input"])
        self.assertAlmostEqual(self.cost("claude-opus-5"), prices["claude-opus-5"]["input"])

    def test_unknown_model_uses_default(self):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        self.write("s.jsonl", [record("s1", ts, "m1", "some-future-model")])
        self.t.ingest()
        self.assertAlmostEqual(self.cost("some-future-model"), self.t.load_prices()["_default"]["input"])

    def test_money_converts_only_for_display(self):
        self.t.set_currency("USD")
        self.assertIn("$", self.t.money(10, 0))
        rate = self.t._currency_cfg()[0]
        self.t.set_currency("EUR")
        self.assertIn("€", self.t.money(10, 0))
        self.assertIn(str(round(10 * rate)), self.t.money(10, 0))


class IngestTest(Base):
    def count(self):
        con = sqlite3.connect(self.t.DB)
        try:
            return con.execute("SELECT COUNT(*) FROM msgs").fetchone()[0]
        finally:
            con.close()

    def test_partial_line_waits_for_its_end(self):
        ts = "2026-11-25T09:00:00.000Z"
        self.write("s.jsonl", [record("s1", ts, "m1")])
        with open(os.path.join(self.proj, "s.jsonl"), "a") as f:
            f.write('{"type":"assistant","sessionId":"s1"')
        self.t.ingest()
        self.assertEqual(self.count(), 1)
        self.write("s.jsonl", [record("s1", ts, "m1"), record("s1", ts, "m2")])
        self.t.ingest()
        self.assertEqual(self.count(), 2)

    def test_type_first_and_spaces(self):
        ts = "2026-11-25T09:00:00.000Z"
        line = record("s1", ts, "m1", first_type=True).replace('":"', '": "')
        self.write("s.jsonl", [line, '{"type":"api-request-blob","body":{"type":"user"}}'])
        self.t.ingest()
        self.assertEqual(self.count(), 1)

    def test_rewritten_file_is_read_again(self):
        ts = "2026-11-25T09:00:00.000Z"
        self.write("s.jsonl", [record("aaaa", ts, "a1"), record("aaaa", ts, "a2")])
        self.t.ingest()
        self.write("s.jsonl", [record("bbbb", ts, f"b{i}") for i in range(4)])
        self.t.ingest()
        con = sqlite3.connect(self.t.DB)
        ids = {r[0] for r in con.execute("SELECT id FROM msgs")}
        con.close()
        self.assertTrue({"b0", "b1", "b2", "b3"} <= ids)

    def test_malformed_lines_are_skipped(self):
        ts = "2026-11-25T09:00:00.000Z"
        self.write("s.jsonl", ['{"type":"assistant", broken', '{"type":"assistant","message":"not a dict"}',
                               record("s1", ts, "m1")])
        self.t.ingest()
        self.assertEqual(self.count(), 1)


def now_ts(minutes_ago=0):
    t = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def turn(sid, mid, cwd="/work/front", branch=None, ctx=40_000, minutes_ago=0, tools=()):
    d = json.loads(record(sid, now_ts(minutes_ago), mid, inp=ctx, cwd=cwd))
    if branch:
        d["gitBranch"] = branch
    d["message"]["content"] = [{"type": "tool_use", "id": f"{mid}_{i}", "name": n, "input": {}} for i, n in enumerate(tools)]
    return json.dumps(d)


def shape(sid, mcp=None, deferred=0, system=8_000):
    tool = lambda n, size: {"name": n, "description": "x" * size, "input_schema": {}}
    tools = [tool("Bash", 4_000), tool("Read", 4_000)]
    tools += [tool(f"mcp__{mcp}__{n}", 10_000) for n in ("a", "b")] if mcp else []
    tools += [dict(tool(f"mcp__later__t{i}", 50), defer_loading=True) for i in range(deferred)]
    return json.dumps({"type": "api-request-shape", "sessionId": sid, "timestamp": now_ts(),
                       "shape": {"system": [{"type": "text", "text": "s" * system}], "tools": tools}})


class ClaudeConfigDirTest(Base):
    def tearDown(self):
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        super().tearDown()

    def test_defaults_include_dot_config_claude(self):
        newer = os.path.join(self.home, ".config", "claude")
        os.makedirs(os.path.join(newer, "projects"))
        self.assertEqual(self.t._claude_dirs(), [newer, os.path.join(self.home, ".claude")])

    def test_env_list_replaces_defaults_and_skips_empty_folders(self):
        a, b, empty = (os.path.join(self.tmp, x) for x in ("a", "b", "empty"))
        for d in (a, b):
            os.makedirs(os.path.join(d, "projects"))
        os.makedirs(empty)
        os.environ["CLAUDE_CONFIG_DIR"] = f"{a}, {empty} ,{b}"
        self.assertEqual(self.t._claude_dirs(), [a, b])

    def test_ingest_reads_every_folder(self):
        other = os.path.join(self.tmp, "other", "projects", "-work-other")
        os.makedirs(other)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        self.write("s.jsonl", [record("s1", ts, "m1", "claude-opus-5-5")])
        with open(os.path.join(other, "o.jsonl"), "w") as f:
            f.write(record("s2", ts, "m2", "claude-opus-5-5") + "\n")
        self.t.PROJECTS_DIRS = [os.path.join(self.home, ".claude", "projects"), os.path.dirname(other)]
        self.t.ingest()
        con = self.t.connect()
        try:
            self.assertEqual(con.execute("SELECT COUNT(DISTINCT session) FROM msgs").fetchone()[0], 2)
        finally:
            con.close()


class BranchAndFixedCostTest(Base):
    def test_task_comes_from_the_branch_name(self):
        for branch, task in (("feature/7067", "7067"), ("fix/ABC-123-login", "ABC-123"), ("task/18499-ui", "18499"),
                             ("main", None), ("feature/v2", None), ("release/1.10", None), ("", None), (None, None)):
            self.assertEqual(self.t.task_of(branch), task, branch)

    def test_a_task_spans_repos_and_other_branches_keep_their_name(self):
        self.write("a.jsonl", [turn("s1", "m1", "/work/front", "feature/7067"), turn("s2", "m2", "/work/back", "feature/7067"),
                               turn("s3", "m3", "/work/front", "main"), turn("s4", "m4", "/work/nogit")])
        self.t.ingest()
        rows = {r["task"] or r["branch"]: r for r in self.t.data(7, self.t.ALL)["branches"]}
        self.assertEqual(set(rows), {"7067", "main"})
        self.assertEqual(rows["7067"]["repos"], ["back", "front"])
        self.assertEqual(rows["7067"]["sessions"], 2)

    def test_fixed_cost_parts_ignore_subagents_and_deferred_tools(self):
        self.write("s1.jsonl", [shape("s1", mcp="docs", deferred=3), turn("s1", "m1")])
        sub = os.path.join(self.proj, "s1", "subagents")
        os.makedirs(sub)
        with open(os.path.join(sub, "agent-x.jsonl"), "w") as f:
            f.write(shape("s1", system=10) + "\n")
        self.t.ingest()
        fx = self.t.data(7, self.t.ALL)["fixed"]
        parts = {p["k"]: p["tokens"] for p in fx["parts"]}
        self.assertEqual(parts["system"], 2_000)
        self.assertIn("mcp:docs", parts)
        self.assertNotIn("mcp:later", parts)
        self.assertEqual(fx["deferred"], 3)
        self.assertEqual(fx["servers"]["docs"]["sessions"], 1)

    def test_fixed_cost_is_the_smallest_context_read_on_every_turn(self):
        self.write("s1.jsonl", [turn("s1", "m1", ctx=50_000, minutes_ago=3), turn("s1", "m2", ctx=30_000, minutes_ago=2),
                                turn("s1", "m3", ctx=90_000, minutes_ago=1)])
        self.t.ingest()
        p = self.t.load_prices()["claude-opus-5-5"]
        fx = self.t.data(7, self.t.ALL)["fixed"]
        self.assertEqual(fx["base"], 30_000)
        self.assertAlmostEqual(fx["cost"], 30_000 * (3 * p["cache_read"] + p["cache_write_5m"]) / 1e6, places=6)

    def test_heavy_unused_mcp_server_is_recommended(self):
        lines = []
        for i in range(4):
            lines += [shape(f"s{i}", mcp="docs"), turn(f"s{i}", f"m{i}", minutes_ago=i)]
        self.write("a.jsonl", lines)
        self.t.ingest()
        self.assertIn("mcp", [r["id"] for r in self.t.data(7, self.t.ALL)["recs"]])
        self.write("b.jsonl", [turn(f"s{i}", f"u{i}", tools=["mcp__docs__a"]) for i in range(2)])
        self.t.ingest()
        self.assertNotIn("mcp", [r["id"] for r in self.t.data(7, self.t.ALL)["recs"]])

    def test_old_database_gets_the_branch_and_reads_history_again(self):
        con = sqlite3.connect(self.t.DB)
        con.executescript("""CREATE TABLE files(path TEXT PRIMARY KEY, offset INTEGER, size INTEGER);
            CREATE TABLE msgs(id TEXT PRIMARY KEY, ts TEXT, day TEXT, hour INTEGER, session TEXT, project TEXT, client TEXT,
              model TEXT, sidechain INTEGER, agent TEXT, inp INTEGER, cw5 INTEGER, cw1h INTEGER, cr INTEGER, out INTEGER,
              think INTEGER, web INTEGER);
            INSERT INTO files VALUES('x.jsonl', 500, 500);""")
        con.commit()
        con.close()
        con = self.t.connect()
        try:
            self.assertIn("branch", [r[1] for r in con.execute("PRAGMA table_info(msgs)")])
            self.assertEqual(con.execute("SELECT offset FROM files").fetchone()[0], 0)
        finally:
            con.close()


class SecretsTest(Base):
    def test_masks(self):
        cases = {
            "curl -u admin:hunter2 https://x": "hunter2",
            "git clone https://bob:s3cr3t@github.com/x": "s3cr3t",
            'curl -H "Cookie: sid=abc123" x': "abc123",
            "mysql -psecretpw db": "secretpw",
            "export API_KEY=sk-live-1234567890": "sk-live-1234567890",
            "curl -H 'Authorization: Bearer eyJhbGciOi.payload.sig'": "eyJhbGciOi",
        }
        for cmd, secret in cases.items():
            self.assertNotIn(secret, self.t.input_head("Bash", {"command": cmd}), cmd)
        self.assertEqual(self.t.input_head("Bash", {"command": "ls -la"}), "ls -la")


class PlanTest(Base):
    def write_plan(self, reset, pct=40):
        z = lambda t: t.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        with open(os.path.join(self.app, "plan.json"), "w") as f:
            json.dump({"at": int(time.time() * 1000), "limits": [
                {"kind": "five_hour", "percentUsed": pct, "resetsAt": z(reset)}]}, f)

    def plan(self):
        con = self.t.connect()
        try:
            prices = self.t.load_prices()
            C = self.t.cost_expr(prices)
            return self.t.plan_info(con, C, "(inp+cw5+cw1h+cr+out)", [])
        finally:
            con.close()

    def test_current_window_has_pct(self):
        self.write_plan(datetime.now(timezone.utc) + timedelta(hours=2))
        w = {x["kind"]: x for x in self.plan()["windows"]}["five_hour"]
        self.assertEqual(w["pct"], 40)

    def test_expired_window_falls_back_to_rolling(self):
        self.write_plan(datetime.now(timezone.utc) - timedelta(hours=1))
        w = {x["kind"]: x for x in self.plan()["windows"]}["five_hour"]
        self.assertIsNone(w["pct"])


class StatuslineForecastTest(unittest.TestCase):
    def setUp(self):
        import statusline
        self.s = statusline
        self.now = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)

    def test_runs_out_before_the_reset(self):
        # opened 2h ago, 70% used: 30% more takes ~51 min, the reset is in 3h
        end = self.s.runs_out(70, self.now + timedelta(hours=3), "five_hour", self.now)
        self.assertAlmostEqual((end - self.now).total_seconds() / 60, 51.4, places=0)

    def test_lasts_until_the_reset_or_too_early_to_tell(self):
        self.assertIsNone(self.s.runs_out(30, self.now + timedelta(hours=3), "five_hour", self.now))
        self.assertIsNone(self.s.runs_out(20, self.now + timedelta(hours=4, minutes=50), "five_hour", self.now))
        self.assertIsNone(self.s.runs_out(100, self.now + timedelta(hours=1), "five_hour", self.now))

    def test_printed_line(self):
        end = self.now + timedelta(minutes=30)
        self.assertTrue(self.s.label("five_hour", 70, end).startswith("5h 70% · runs out "))
        self.assertEqual(self.s.label("seven_day", 20, None), "week 20%")


def paused(sid, days_ago, cw=2_000_000):
    """Two turns of one session two hours apart, `days_ago` days back: one pause over 1 h with a big cache write."""
    out = []
    for i, minutes in enumerate((days_ago * 1440 + 120, days_ago * 1440)):
        d = json.loads(record(sid, now_ts(minutes), f"{sid}_{i}", inp=10))
        d["message"]["usage"]["cache_creation_input_tokens"] = cw
        out.append(json.dumps(d))
    return out


class PausesCardTest(Base):
    HOOK = {"hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "python3 x.py"}]}]}}

    def card(self, hook, since=None):
        with open(os.path.join(self.home, ".claude", "settings.json"), "w") as f:
            json.dump(self.HOOK if hook else {}, f)
        self.t.ingest()
        if since is not None:
            con = self.t.connect()
            con.execute("INSERT OR REPLACE INTO seen(key, ts) VALUES ('gap_hook', ?)", (now_ts(since * 1440),))
            con.commit()
            con.close()
        return [r for r in self.t.data(90, self.t.ALL)["recs"] if r["id"] == "gaps"]

    def test_without_the_hook_it_is_pending_and_offers_the_prompt(self):
        self.write("a.jsonl", paused("s1", 3))
        [c] = self.card(False)
        self.assertEqual((c["status"], c["metric"]), ("todo", 1))
        self.assertTrue(c["prompt"] and c["tools"])

    def test_a_new_hook_is_under_review_for_a_week_and_earlier_pauses_today_do_not_count(self):
        self.write("a.jsonl", paused("s1", 3) + paused("s2", 0))
        [c] = self.card(True)
        self.assertEqual((c["status"], c["metric"], c["prompt"], c["tools"]), ("warn", 0, "", []))
        self.assertIn((datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d"), c["evidence"])

    def test_after_a_week_with_cheaper_pauses_it_is_done(self):
        self.write("a.jsonl", paused("s1", 20) + paused("s2", 15) + paused("s3", 12))
        [c] = self.card(True, since=10)
        self.assertEqual((c["status"], c["metric"]), ("ok", 0))

    def test_after_a_week_with_the_same_pauses_it_stays_under_review(self):
        self.write("a.jsonl", paused("s1", 20) + paused("s2", 15) + paused("s3", 5) + paused("s4", 3))
        [c] = self.card(True, since=10)
        self.assertEqual((c["status"], c["metric"]), ("warn", 2))

    def test_removing_the_hook_goes_back_to_pending(self):
        self.write("a.jsonl", paused("s1", 3))
        self.card(True, since=10)
        [c] = self.card(False)
        self.assertEqual(c["status"], "todo")
        con = self.t.connect()
        self.assertIsNone(con.execute("SELECT ts FROM seen WHERE key='gap_hook'").fetchone())
        con.close()


class FunnelTest(Base):
    """Only studied cards are recommended; the bar is relative to each user's own spend."""

    def hist(self, rid, days, impact=10.0, status="todo"):
        con = self.t.connect()
        con.executemany("INSERT OR REPLACE INTO recs_hist VALUES(?,?,?,?,?)",
                        [((datetime.now() - timedelta(days=d)).strftime("%Y-%m-%d"), rid, status, impact, 1) for d in range(days)])
        con.commit()
        return con

    def run_funnel(self, recs, span=7, week_cost=100.0):
        con = self.t.connect()
        self.t.funnel(con, recs, span, week_cost)
        con.close()
        return {x["id"]: (x["stage"], x["missing"]) for x in recs}

    def rec(self, rid, impact=5.0, status="todo", **kw):
        return dict({"id": rid, "status": status, "impact": impact}, **kw)

    def test_new_findings_wait_until_they_repeat_on_three_days(self):
        self.hist("a", 2).close()
        self.hist("b", 3).close()
        got = self.run_funnel([self.rec("a"), self.rec("b"), self.rec("c")])
        self.assertEqual(got["a"][0], "study")
        self.assertIn("2", got["a"][1][0])
        self.assertEqual(got["b"], ("shown", []))
        self.assertEqual(got["c"][0], "study")

    def test_the_bar_is_relative_to_each_users_spend(self):
        self.hist("x", 7).close()
        light = self.run_funnel([self.rec("x", impact=0.5)], week_cost=20)       # 0.5 $/week on 20 $/week: worth it
        heavy = self.run_funnel([self.rec("x", impact=0.5)], week_cost=1000)     # same 0.5 $ on 1000 $/week: not yet
        tiny = self.run_funnel([self.rec("x", impact=0.05)], week_cost=2)        # under the 0.25 $ floor
        capped = self.run_funnel([self.rec("x", impact=3)], week_cost=1000)     # 3 $/week always counts, whatever the spend
        self.assertEqual(light["x"][0], "shown")
        self.assertEqual(heavy["x"][0], "study")
        self.assertEqual(tiny["x"][0], "study")
        self.assertEqual(capped["x"][0], "shown")

    def test_cards_without_an_amount_only_need_to_repeat(self):
        self.hist("q", 4).close()
        self.assertEqual(self.run_funnel([self.rec("q", impact=None)])["q"], ("shown", []))

    def test_a_detector_can_ask_for_enough_sessions(self):
        self.hist("s", 7).close()
        got = self.run_funnel([self.rec("s", sample={"sessions": 2, "days": 1}),
                               self.rec("s2", sample={"sessions": 5, "days": 3})])
        self.assertEqual(got["s"][0], "study")
        self.hist("s2", 7).close()
        got = self.run_funnel([self.rec("s2", sample={"sessions": 5, "days": 3})])
        self.assertEqual(got["s2"][0], "shown")

    def test_applied_cards_are_always_shown(self):
        got = self.run_funnel([self.rec("w", status="warn"), self.rec("o", status="ok")])
        self.assertEqual((got["w"][0], got["o"][0]), ("shown", "shown"))

    def test_dismissed_until_its_cost_doubles(self):
        self.hist("d", 7, impact=10).close()
        self.t.dismiss_rec("d")
        self.assertEqual(self.run_funnel([self.rec("d")])["d"][0], "dismissed")
        self.hist("d", 1, impact=19).close()
        self.assertEqual(self.run_funnel([self.rec("d")])["d"][0], "dismissed")
        self.hist("d", 1, impact=25).close()
        self.assertEqual(self.run_funnel([self.rec("d")])["d"][0], "shown")
        self.t.dismiss_rec("d")
        self.t.restore_rec("d")
        self.assertEqual(self.run_funnel([self.rec("d")])["d"][0], "shown")

    def test_only_plain_ids_can_be_dismissed(self):
        for bad in ("", "../x", "A B", "x" * 41):
            with self.assertRaises(ValueError):
                self.t.dismiss_rec(bad)

    def test_the_dashboard_data_carries_the_stage(self):
        self.write("a.jsonl", paused("s1", 3))
        recs = self.t.data(30, self.t.ALL)["recs"]
        self.assertTrue(recs)
        self.assertTrue(all(x["stage"] in ("shown", "study", "dismissed") and isinstance(x["missing"], list) for x in recs))


def req(sid, mid, minutes_ago, ctx, cw=0, model="claude-opus-5-5", tools=(), h1=False):
    """One main-conversation request: `ctx` tokens of context, `cw` of them written to the cache (for 1 hour if h1),
    the rest read."""
    d = json.loads(record(sid, now_ts(minutes_ago), mid, model=model, inp=10, cr=max(ctx - cw - 10, 0)))
    d["message"]["usage"]["cache_creation_input_tokens"] = cw
    if h1:
        d["message"]["usage"]["cache_creation"] = {"ephemeral_1h_input_tokens": cw, "ephemeral_5m_input_tokens": 0}
    d["message"]["content"] = [{"type": "tool_use", "id": f"{mid}_{i}", "name": n, "input": {}} for i, n in enumerate(tools)]
    return json.dumps(d)


def result(tool_id, chars, error=False):
    return json.dumps({"type": "user", "sessionId": "x", "timestamp": now_ts(), "cwd": "/work/demo",
                       "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id,
                                                                "content": "x" * chars, "is_error": error}]}})


class ClientsFileTest(Base):
    def load(self, cfg):
        with open(self.t.CLIENTS_FILE, "w") as f:
            json.dump(cfg, f)
        return self.t._load_groups()[0]

    def test_made_up_clients_from_earlier_installs_are_ignored_but_real_ones_kept(self):
        cfg = {"clients": {"company-a": ["/work/company-a"], "company-b": ["/work/company-b", "/clients/b"],
                           "acme": ["/work/acme"], "company-c": ["/work/company-a", "/x"]}}
        self.assertEqual(set(self.load(cfg)), {"acme", "company-c"})


class PaceTest(Base):
    def days(self, spec):
        """spec: {days_ago: requests}, one request of 1M input tokens each, at this same time of day."""
        self.write("a.jsonl", [record(f"s{d}", now_ts(d * 1440), f"m{d}_{i}") for d, n in spec.items() for i in range(n)])
        return self.t.data(30, self.t.ALL)["pace"]

    def test_month_end_follows_the_last_seven_days(self):
        p = self.days({1: 2, 3: 5})
        self.assertAlmostEqual(p["rate"], self.cost("claude-opus-5-5") / 7)  # 7 requests over 7 days
        self.assertGreaterEqual(p["proj"], p["month"])
        self.assertIsNone(p["odd"])  # under 10 days to compare with

    def test_unusual_day_is_flagged_with_its_session(self):
        spec = {d: 1 for d in range(8, 28)}
        spec[2] = 6
        p = self.days(spec)
        self.assertIsNotNone(p["odd"])
        self.assertAlmostEqual(p["odd"]["ratio"], 6, places=3)
        self.assertEqual(p["odd"]["session"], "s2")

    def test_a_normal_week_is_not_flagged(self):
        p = self.days({d: 1 for d in range(0, 28)})
        self.assertIsNone(p["odd"])


class NewDetectorsTest(Base):
    def rec(self, rid):
        return next((r for r in self.t.data(30, self.t.ALL)["recs"] if r["id"] == rid), None)

    def test_cache_rebuilt_without_a_pause_is_found_with_its_cause(self):
        self.write("a.jsonl", [
            req("s1", "a1", 30, 200_000, 2_000), req("s1", "a2", 29, 205_000, 5_000, model="claude-sonnet-5-5"),   # model switch
            req("s2", "b1", 30, 800_000, 2_000), req("s2", "b2", 29, 700_000, 690_000),                          # trimmed
            req("s3", "c1", 30, 100_000, 2_000), req("s3", "c2", 29, 101_000, 100_000),                          # no cause
            req("s4", "d1", 30, 800_000, 2_000), req("s4", "d2", 29, 50_000, 45_000),                            # compaction: no
            req("s5", "e1", 300, 100_000, 2_000), req("s5", "e2", 200, 101_000, 100_000),                        # pause: no
            req("s6", "f1", 30, 100_000, 2_000), req("s6", "f2", 29, 103_000, 3_000),                            # normal turn: no
        ])
        r = self.rec("cache-miss")
        self.assertEqual(r["metric"], 2)       # trimmed + no cause; the model switch wrote 5K only, under 20K
        self.assertEqual(r["sample"], {"sessions": 2, "days": 1})
        self.assertGreater(r["impact"], 0)

    def test_switching_model_is_named_as_the_cause(self):
        self.write("a.jsonl", [req("s1", "a1", 30, 200_000, 2_000), req("s1", "a2", 29, 205_000, 200_000, model="claude-sonnet-5-5")])
        r = self.rec("cache-miss")
        self.assertEqual(r["metric"], 1)
        self.assertIn("model", r["evidence"])

    def test_huge_tool_outputs_are_priced_by_the_turns_that_carry_them(self):
        self.write("a.jsonl", [req("s1", "a1", 30, 50_000, 1_000, tools=("Bash", "Read")), result("a1_0", 80_000), result("a1_1", 80_000)]
                   + [req("s1", f"a{i}", 30 - i, 70_000 + i, 1_000) for i in range(2, 12)])
        r = self.rec("tool-output")
        self.assertEqual(r["metric"], 1)        # Read has its own card
        self.assertIn("Bash", r["evidence"])
        self.assertGreater(r["impact"], 0)
        self.assertTrue(r["tools"])              # no bashOutputMaxChars in settings: suggest it

    def test_nothing_to_say_when_outputs_are_small(self):
        self.write("a.jsonl", [req("s1", "a1", 30, 50_000, 1_000, tools=("Bash",)), result("a1_0", 3_000),
                               req("s1", "a2", 29, 51_000, 1_000)])
        self.assertIsNone(self.rec("tool-output"))
        self.assertIsNone(self.rec("cache-miss"))

    def test_five_minute_cache_rebuilt_after_short_pauses_suggests_one_hour(self):
        rows = []
        for s in range(3):  # every turn 20 minutes after the last: each one writes the whole context again
            rows += [req(f"s{s}", f"s{s}_{i}", 200 - 20 * i, 300_000 + i, 300_000 if i else 2_000) for i in range(5)]
        self.write("a.jsonl", rows)
        r = self.rec("ttl-1h")
        self.assertEqual((r["status"], r["sample"]["sessions"]), ("todo", 3))
        self.assertGreater(r["impact"], 0)
        self.assertEqual(self.t.APPLY["ttl-1h"], {"promptCacheTtl": "1h"})

    def test_one_hour_cache_without_pauses_suggests_five_minutes(self):
        rows = [req("s1", f"a{i}", 100 - i, 100_000 + 1_000 * i, 20_000, h1=True) for i in range(20)]
        self.write("a.jsonl", rows)
        r = self.rec("ttl-5m")
        self.assertEqual(r["status"], "todo")
        self.assertIn("1-hour", r["evidence"])
        self.assertIsNone(self.rec("ttl-1h"))

    def test_one_hour_cache_that_pays_off_says_nothing(self):
        rows = []
        for s in range(3):
            rows += [req(f"s{s}", f"s{s}_{i}", 200 - 20 * i, 300_000 + i, 2_000, h1=True) for i in range(5)]
        self.write("a.jsonl", rows)
        self.assertIsNone(self.rec("ttl-5m"))
        self.assertIsNone(self.rec("ttl-1h"))

    def test_runs_of_three_failures_of_the_same_tool_are_counted(self):
        rows = []
        for i in range(4):  # Bash fails four times in a row, then works
            rows += [req("s1", f"a{i}", 30 - i, 50_000, 1_000, tools=("Bash",)), result(f"a{i}_0", 100, error=i < 4)]
        rows += [req("s1", "b0", 20, 50_000, 1_000, tools=("Bash",)), result("b0_0", 100)]
        for i in range(2):  # two failures only: not a loop
            rows += [req("s2", f"c{i}", 30 - i, 50_000, 1_000, tools=("Read",)), result(f"c{i}_0", 100, error=True)]
        self.write("a.jsonl", rows)
        r = self.rec("error-loops")
        self.assertEqual((r["metric"], r["sample"]), (1, {"sessions": 1, "days": 1}))
        self.assertIn("Bash: 1 (4 tries", r["evidence"])
        self.assertNotIn("Read", r["evidence"])

    def test_heavy_global_claude_md_is_flagged_and_light_one_is_not(self):
        fx = {"parts": [{"k": "claude_md", "tokens": 6_000}], "base": 30_000, "cost": 30.0, "sessions": 10}
        r = self.t.claude_md_rec(fx)
        self.assertAlmostEqual(r["impact"], 30.0 * 4_000 / 30_000 * 0.5)
        self.assertEqual(r["metric"], 6_000)
        fx["parts"][0]["tokens"] = 2_000
        self.assertIsNone(self.t.claude_md_rec(fx))


class UpdateTest(Base):
    def save(self, name, value):
        with open(os.path.join(self.app, name), "w") as f:
            json.dump(value, f)

    def test_versions_agree(self):
        self.assertEqual(read(os.path.join(ROOT, ".claude-plugin", "plugin.json"))["version"], self.t.VERSION)
        self.assertIn(f'version = "{self.t.VERSION}"', read_text(os.path.join(ROOT, "pyproject.toml")))

    def test_newer_release_shows_the_command_for_how_it_was_installed(self):
        self.save("update.json", {"checked": time.time(), "latest": "99.0.0", "url": "https://example.test/r"})
        info = self.t.update_info()
        self.assertEqual((info["latest"], info["how"]), ("99.0.0", "git pull && python3 install.py"))
        self.save("install.json", {"via": "plugin"})
        self.assertEqual(self.t.update_info()["how"], "claude plugin update tokenometro@tokenometro")
        self.save("update.json", {"checked": time.time(), "latest": self.t.VERSION})
        self.assertIsNone(self.t.update_info())

    def test_check_asks_github_once_a_day_and_can_be_turned_off(self):
        calls = []

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, n):
                return b'{"tag_name": "v99.1.0", "html_url": "https://example.test/r"}'

        real = self.t.urllib.request.urlopen
        self.t.urllib.request.urlopen = lambda req, timeout: calls.append(req.full_url) or Resp()
        try:
            os.environ["TOKENOMETRO_UPDATE_CHECK"] = "0"
            self.t.check_update()
            self.assertEqual(calls, [])
            del os.environ["TOKENOMETRO_UPDATE_CHECK"]
            self.t.check_update()
            self.t.check_update()
            self.assertEqual(calls, ["https://api.github.com/repos/Jaferdi/tokenometro/releases/latest"])
            self.assertEqual(self.t.update_info()["latest"], "99.1.0")
        finally:
            self.t.urllib.request.urlopen = real
            os.environ.pop("TOKENOMETRO_UPDATE_CHECK", None)


class CliTest(unittest.TestCase):
    def test_help_and_unknown_command(self):
        import cli
        self.assertEqual(cli.main(["help"]), 0)
        self.assertEqual(cli.main(["nope"]), 2)


class I18nTest(unittest.TestCase):
    def test_same_keys_in_both_languages(self):
        src = read_text(os.path.join(ROOT, "tracker.py"))
        block = src[src.index("const I18N"):]
        en = block[block.index("en:{"):block.index("es:{")]
        es = block[block.index("es:{"):block.index("};")]
        keys = lambda s: set(re.findall(r"(?:^|[{,\s])([a-zA-Z_][a-zA-Z0-9_]*):(?=['\"`(])", s))
        self.assertEqual(keys(en) - keys(es), set(), "missing in es")
        self.assertEqual(keys(es) - keys(en), set(), "missing in en")

    def test_every_used_key_exists(self):
        src = read_text(os.path.join(ROOT, "tracker.py"))
        page = src[src.index("PAGE = r"):]
        block = page[page.index("const I18N"):]
        en = block[block.index("en:{"):block.index("es:{")]
        defined = set(re.findall(r"(?:^|[{,\s])([a-zA-Z_][a-zA-Z0-9_]*):(?=['\"`(])", en))
        used = set(re.findall(r"\bt\('([a-zA-Z0-9_]+)'", page)) | set(re.findall(r'data-i18n(?:-aria|-title)?="([a-zA-Z0-9_]+)"', page))
        self.assertEqual(used - defined, set())


class DemoAndServerTest(Base):
    clients = {"clients": {"acme": ["/code/acme/"], "blue-harbor": ["/code/blue-harbor/"]}}

    def setUp(self):
        super().setUp()
        demo.generate(self.home)
        self.t.ingest()

    def test_data_has_every_section(self):
        for lang in ("en", "es"):
            self.t.set_lang(lang)
            r = self.t.data(30, self.t.ALL)
            for k in ("kpi_today", "kpi_range", "sessions", "models", "clients", "recs", "compare", "plan", "live"):
                self.assertIn(k, r)
            self.assertGreater(len(r["sessions"]), 10)
            self.assertEqual({c["client"] for c in r["clients"]} - {"acme", "blue-harbor", "otros"}, set())
        json.dumps(r)

    def test_empty_client_does_not_break(self):
        r = self.t.data(7, "nobody")
        self.assertEqual(r["sessions"], [])

    def test_demo_has_branches_and_fixed_cost(self):
        r = self.t.data(30, self.t.ALL)
        self.assertTrue(any(b["task"] == "PAY-142" for b in r["branches"]))
        self.assertGreater(r["fixed"]["measured"], 10)

    def test_weekly_digest_is_written_once(self):
        from datetime import date
        monday = date.today() - timedelta(days=date.today().weekday())
        path = self.t.write_digest(monday)
        self.assertTrue(os.path.basename(path).startswith("tokenometro-week-"))
        text = read_text(path)
        self.assertIn("# Tokenometro", text)
        self.assertIn("PAY-", text)
        with open(path, "w") as f:
            f.write("kept")
        self.t.write_digest(monday)
        self.assertEqual(read_text(path), "kept")

    def test_session_detail(self):
        sid = self.t.data(30, self.t.ALL)["sessions"][0]["session"]
        d = self.t.session_detail(sid)
        self.assertTrue(d["by_model"])

    def test_http_rejects_foreign_host(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), self.t.H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        try:
            ok = urllib.request.urlopen(f"http://127.0.0.1:{port}/api/clients", timeout=10)
            self.assertEqual(ok.status, 200)
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/data", headers={"Host": f"evil.example:{port}"})
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(req, timeout=10)
            self.assertEqual(e.exception.code, 403)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_quit_needs_the_token(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), self.t.H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        self.t._TOKEN = "secret-token"
        try:
            for headers in ({}, {"X-Tokenometro-Token": "wrong"}):
                with self.assertRaises(urllib.error.HTTPError) as e:
                    urllib.request.urlopen(urllib.request.Request(f"http://127.0.0.1:{port}/api/quit", headers=headers), timeout=10)
                self.assertEqual(e.exception.code, 403)
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/quit", headers={"X-Tokenometro-Token": "secret-token"})
            self.assertEqual(urllib.request.urlopen(req, timeout=10).read(), b"bye")
        finally:
            srv.shutdown()
            srv.server_close()


class ApplyTest(Base):
    def settings(self, value=None):
        path = os.path.join(self.home, ".claude", "settings.json")
        if value is not None:
            with open(path, "w") as f:
                json.dump(value, f)
        return read(path)

    def test_apply_sets_the_key_keeps_the_rest_and_undo_puts_it_back(self):
        self.settings({"model": "opus", "env": {"FOO": "1"}})
        self.t.apply_rec("subagent-model")
        self.assertEqual(self.settings(), {"model": "opus", "env": {"FOO": "1", "CLAUDE_CODE_SUBAGENT_MODEL": "sonnet"}})
        self.assertTrue([f for f in os.listdir(os.path.join(self.home, ".claude")) if ".bak-tokenometro-" in f])
        self.t.apply_rec("subagent-model")  # twice is once
        self.t.undo_rec("subagent-model")
        self.assertEqual(self.settings(), {"model": "opus", "env": {"FOO": "1"}})

    def test_undo_leaves_what_you_changed_by_hand(self):
        self.settings({"autoCompactWindow": 200000})
        self.t.apply_rec("ctx")
        self.assertEqual(self.settings()["autoCompactWindow"], 300000)
        self.settings({"autoCompactWindow": 250000})
        self.t.undo_rec("ctx")
        self.assertEqual(self.settings()["autoCompactWindow"], 250000)

    def test_only_known_recommendations(self):
        with self.assertRaises(ValueError):
            self.t.apply_rec("anything")
        with self.assertRaises(ValueError):
            self.t.undo_rec("ctx")

    def test_card_offers_apply_and_then_shows_it_applied(self):
        recs = [{"id": "ctx", "status": "todo"}, {"id": "reads", "status": "todo"}]
        con = self.t.connect()
        self.t.attach_applied(con, recs)
        self.assertEqual([r["can_apply"] for r in recs], [True, False])
        self.t.apply_rec("ctx")
        self.t.attach_applied(con, recs)
        con.close()
        self.assertFalse(recs[0]["can_apply"])
        self.assertEqual(recs[0]["applied"]["day"], datetime.now().strftime("%Y-%m-%d"))

    def test_http_apply_needs_the_page_token(self):
        self.settings({})
        srv = ThreadingHTTPServer(("127.0.0.1", 0), self.t.H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        post = lambda headers: urllib.request.urlopen(urllib.request.Request(
            f"http://127.0.0.1:{port}/api/apply", data=b'{"id": "ctx"}', headers=headers, method="POST"), timeout=10)
        try:
            page = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10).read().decode()
            self.assertIn(self.t._PAGE_TOKEN, page)
            for headers in ({}, {"X-Tokenometro-Page": "wrong"},
                            {"X-Tokenometro-Page": self.t._PAGE_TOKEN, "Origin": "https://evil.example"}):
                with self.assertRaises(urllib.error.HTTPError) as e:
                    post(headers)
                self.assertEqual(e.exception.code, 403)
            self.assertNotIn("autoCompactWindow", self.settings())
            self.assertEqual(json.loads(post({"X-Tokenometro-Page": self.t._PAGE_TOKEN}).read()), {"ok": True})
            self.assertEqual(self.settings()["autoCompactWindow"], 300000)
        finally:
            srv.shutdown()
            srv.server_close()


class ReportTest(Base):
    clients = {"clients": {"acme": ["/work/"]}}

    def setUp(self):
        super().setUp()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        # 1M input tokens of claude-opus-5-5 today, plus a model prices.json does not know
        self.write("s.jsonl", [record("11111111-1111-4111-a111-111111111111", now, "m1"),
                               record("11111111-1111-4111-a111-111111111111", now, "m2", "claude-future-9", inp=10)])
        self.t.ingest()

    def test_unpriced_models_are_reported(self):
        names = [m["model"] for m in self.t.data(7, self.t.ALL)["unpriced"]]
        self.assertEqual(names, ["claude-future-9"])

    def test_csv_report(self):
        import csv
        import io
        month = datetime.now().strftime("%Y-%m")
        self.t.set_currency("EUR")
        rows = list(csv.DictReader(io.StringIO(self.t.report(month, "session"))))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["client"], "acme")
        self.assertGreater(float(rows[0]["cost_usd"]), 0)
        self.assertIn("cost_eur", rows[0])
        by_client = list(csv.DictReader(io.StringIO(self.t.report(month, "client"))))
        self.assertEqual([r["client"] for r in by_client], ["acme"])
        self.assertEqual(self.t.report("1999-01", "session").count("\n"), 1)  # header only

    def test_csv_cells_are_never_formulas(self):
        self.assertEqual(self.t._cell('=HYPERLINK("http://x")'), '\'=HYPERLINK("http://x")')
        self.assertEqual(self.t._cell("@SUM(A1)"), "'@SUM(A1)")
        self.assertEqual(self.t._cell("Add refund endpoint"), "Add refund endpoint")
        self.assertEqual(self.t._cell(-3.5), -3.5)


class InstallerTest(unittest.TestCase):
    """install.py and start.py in a throwaway HOME (and LOCALAPPDATA on Windows), without starting the server."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, "home")
        os.makedirs(os.path.join(self.home, ".claude", "projects"))
        self.env = dict(os.environ, HOME=self.home, USERPROFILE=self.home, LOCALAPPDATA=os.path.join(self.home, "AppData"),
                        TOKENOMETRO_NO_START="1", TOKENOMETRO_PORT="7998", PYTHONIOENCODING="utf-8")
        self.settings = os.path.join(self.home, ".claude", "settings.json")

    def install(self, *args):
        import subprocess
        return subprocess.run([sys.executable, os.path.join(ROOT, "install.py"), *args], env=self.env,
                              capture_output=True, text=True, encoding="utf-8", timeout=120)

    def hooks(self):
        s = read(self.settings)
        return [h["command"] for e in s.get("hooks", {}).get("SessionStart", []) for h in e.get("hooks", [])]

    def test_install_replaces_old_hooks_and_uninstall_cleans_up(self):
        with open(self.settings, "w", encoding="utf-8") as f:
            json.dump({"hooks": {"SessionStart": [
                {"hooks": [{"type": "command", "command": '"$HOME/.local/share/claude-tokens/start.sh"'}]},
                {"matcher": "x", "hooks": [{"type": "command", "command": "echo mine"}]}]},
                "statusLine": {"type": "command", "command": "mybar"}}, f)
        r = self.install("--statusline")
        self.assertEqual(r.returncode, 0, r.stderr)
        hooks = self.hooks()
        self.assertEqual(len(hooks), 2)
        self.assertIn("echo mine", hooks)
        self.assertTrue(any("start.py" in h and "--port 7998" in h for h in hooks))
        dest = os.path.join(self.home, "AppData", "tokenometro") if os.name == "nt" else os.path.join(self.home, ".local", "share", "tokenometro")
        self.assertEqual(read(os.path.join(dest, "install.json")), {"via": "git"})  # picks the update command
        self.assertEqual(read(self.settings)["statusLine"]["command"], "mybar")  # someone else's: untouched
        self.assertIn("--tee", r.stdout)
        r = self.install("--uninstall")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.hooks(), ["echo mine"])

    def test_statusline_is_ours_when_free(self):
        self.assertEqual(self.install("--statusline").returncode, 0)
        self.assertIn("statusline.py", read(self.settings)["statusLine"]["command"])
        self.install("--uninstall")
        self.assertNotIn("statusLine", read(self.settings))

    def test_invalid_settings_is_left_alone(self):
        with open(self.settings, "w", encoding="utf-8") as f:
            f.write("{oops")
        r = self.install()
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(read_text(self.settings), "{oops")

    def test_plugin_sync_seeds_once_and_reports_code_changes(self):
        import start
        dest = os.path.join(self.tmp, "data")
        self.assertTrue(start.sync(ROOT, dest))
        for f in ("tracker.py", "statusline.py", "prices.json"):
            self.assertTrue(os.path.exists(os.path.join(dest, f)), f)
        self.assertFalse(os.path.exists(os.path.join(dest, "clients.json")))  # only the user creates it
        with open(os.path.join(dest, "prices.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        self.assertFalse(start.sync(ROOT, dest))
        self.assertEqual(read_text(os.path.join(dest, "prices.json")), "{}")  # the user's prices are kept


if __name__ == "__main__":
    unittest.main()

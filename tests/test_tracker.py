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
    old = os.environ.get("HOME")
    os.environ["HOME"] = home
    for k in [k for k in os.environ if k.startswith(("TOKENOMETRO_", "CLAUDE_TOKENS_"))]:
        del os.environ[k]
    try:
        spec = importlib.util.spec_from_file_location(f"tracker_{abs(hash(tmp))}", os.path.join(app, "tracker.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        if old is not None:
            os.environ["HOME"] = old
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


class BudgetAndReportTest(Base):
    clients = {"clients": {"acme": ["/work/"]}, "budget": {"day": 2, "week": 1000, "month": "oops"}}

    def setUp(self):
        super().setUp()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        # 1M input tokens of claude-opus-5-5 today, plus a model prices.json does not know
        self.write("s.jsonl", [record("11111111-1111-4111-a111-111111111111", now, "m1"),
                               record("11111111-1111-4111-a111-111111111111", now, "m2", "claude-future-9", inp=10)])
        self.t.ingest()

    def test_budget_levels(self):
        b = {x["kind"]: x for x in self.t.data(7, self.t.ALL)["budget"]}
        self.assertEqual(set(b), {"day", "week"})  # "oops" is ignored
        self.assertEqual(b["day"]["level"], "over")
        self.assertEqual(b["week"]["level"], "ok")

    def test_status_file_for_statusline(self):
        self.t.write_status()
        st = read(os.path.join(self.app, "status.json"))
        self.assertTrue(any(x["level"] == "over" for x in st["budget"]))

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


if __name__ == "__main__":
    unittest.main()

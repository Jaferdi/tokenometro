#!/usr/bin/env python3
"""Local Claude Code token tracker.

Reads the transcripts in ~/.claude/projects (read-only, incremental) and serves a
dashboard at http://127.0.0.1:7878. No dependencies: standard library only.

  python3 tracker.py serve      # periodic ingest + dashboard
  python3 tracker.py ingest     # ingest only (and print a summary)
"""
import glob
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
import threading
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOME = os.path.expanduser("~")
BASE = os.path.dirname(os.path.abspath(__file__))
PROJECTS = os.path.join(HOME, ".claude", "projects")
DB = os.path.join(BASE, "usage.db")
PRICES = os.path.join(BASE, "prices.json")
CONTROL = os.path.join(BASE, ".control")  # port, pid and the token that lets the installer stop this server


def read_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def read_head(path, n):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read(n)


def _env(name, default):
    """TOKENOMETRO_<X>; the old name CLAUDE_TOKENS_<X> is still accepted."""
    return os.environ.get("TOKENOMETRO_" + name) or os.environ.get("CLAUDE_TOKENS_" + name) or default


PORT = int(_env("PORT", "7878"))
VERSION = "0.5.0"
REPO = "Jaferdi/tokenometro"
UPDATE_FILE = os.path.join(BASE, "update.json")


def _small_json(path):
    """A small file of ours that may not exist yet or be half-written: never an error, an empty dict."""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}
INGEST_EVERY = 60
CTX_LIMIT = 300_000  # if autoCompactWindow is set in settings.json, the recommendations use that value
LIVE_MIN = 20
COST_WARN = float(_env("COST_WARN", "50"))

_lock = threading.Lock()
_last_ingest = 0.0


# Spanish names (clientes.json, clientes/proyectos/corte/etiquetas) are still read for existing setups.
CLIENTS_FILE = os.path.join(BASE, "clients.json")
if not os.path.exists(CLIENTS_FILE):
    CLIENTS_FILE = os.path.join(BASE, "clientes.json")
ALL = "all"  # client filter meaning "no filter"; the legacy "todos" is still accepted by the API
OTHERS = "otros"  # stored in existing databases as the client of unmatched sessions: kept as the internal key


def _cfg_get(raw, new, old, default=None):
    return raw[new] if new in raw else raw.get(old, default)


# Earlier installs were seeded with these made-up clients as real ones: left untouched they are ignored,
# so they don't show up in the client selector.
SAMPLE_CLIENTS = {"company-a": ["/work/company-a"], "company-b": ["/work/company-b", "/clients/b"]}


def _load_groups():
    """clients.json: {"clients": {...}, "projects": {...}}. Also accepts the old format (clients at the root)."""
    try:
        raw = read_json(CLIENTS_FILE)
    except Exception:
        raw = {}
    clean = lambda d: {k: ([v] if isinstance(v, str) else list(v)) for k, v in (d or {}).items()
                       if not k.startswith("_") and SAMPLE_CLIENTS.get(k) != v}
    if any(k in raw for k in ("clients", "clientes", "projects", "proyectos")):
        return clean(_cfg_get(raw, "clients", "clientes")), clean(_cfg_get(raw, "projects", "proyectos")), raw
    return clean(raw), {}, {}


CLIENTS, PROJECTS_CFG, _CFG = _load_groups()
# Cut date of "Before / after": the date of the change you want to measure. Defaults to 7 days ago.
CUT_DEFAULT = _env("CUT", _cfg_get(_CFG, "cut", "corte") or (date.today() - timedelta(days=7)).isoformat())


def _match(groups, cwd):
    """First group whose fragment appears in the path, also in its dashed form
    (so /tmp/claude-1000/-home-…-project-x/… and ~/.claude/projects/-home-…/memory count for their project)."""
    c = cwd or ""
    for name, frags in groups.items():
        for f in frags:
            if f and (f in c or f.replace("/", "-") in c):
                return name
    return None


def client_of(cwd):
    return _match(CLIENTS, cwd) or OTHERS


def short_path(p):
    p = (p or "").replace(HOME, "~")
    return tr("(Claude scratchpad)", "(scratchpad de Claude)") if p.startswith("/tmp/claude-") else p


def session_groups(rows):
    """rows: (session, cwd, message count). Each session counts entirely for one client and one project:
    the one with most messages among its folders; with no configured project, its most used folder."""
    per = {}
    for sid, cwd, n in rows:
        per.setdefault(sid, []).append((cwd or "", n or 0))
    out = {}
    for sid, cs in per.items():
        def pick(groups):
            score = {}
            for cwd, n in cs:
                g = _match(groups, cwd)
                if g:
                    score[g] = score.get(g, 0) + n
            return max(score, key=score.get) if score else None
        top_cwd = max(cs, key=lambda x: x[1])[0]
        out[sid] = (pick(CLIENTS) or OTHERS, pick(PROJECTS_CFG) or short_path(top_cwd))
    return out


def reclassify(con):
    """Recompute each session's client (after ingesting or when the clients file changes)."""
    groups = session_groups(con.execute("SELECT session, project, COUNT(*) FROM msgs GROUP BY session, project"))
    for sid, (cl, _p) in groups.items():
        for t in ("msgs", "tools", "prompts"):
            con.execute(f"UPDATE {t} SET client=? WHERE session=? AND client IS NOT ?", (cl, sid, cl))
    con.commit()


def local(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
    except Exception:
        return None


def connect():
    con = sqlite3.connect(DB, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, offset INTEGER, size INTEGER);
        CREATE TABLE IF NOT EXISTS msgs(id TEXT PRIMARY KEY, ts TEXT, day TEXT, hour INTEGER,
          session TEXT, project TEXT, client TEXT, model TEXT, sidechain INTEGER, agent TEXT,
          inp INTEGER, cw5 INTEGER, cw1h INTEGER, cr INTEGER, out INTEGER, think INTEGER, web INTEGER);
        CREATE TABLE IF NOT EXISTS tools(id TEXT PRIMARY KEY, msg_id TEXT, day TEXT, session TEXT,
          client TEXT, name TEXT, sidechain INTEGER, result_chars INTEGER DEFAULT 0, is_error INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS prompts(uuid TEXT PRIMARY KEY, day TEXT, session TEXT, client TEXT, chars INTEGER);
        CREATE TABLE IF NOT EXISTS titles(session TEXT PRIMARY KEY, title TEXT);
        CREATE TABLE IF NOT EXISTS compacts(uuid TEXT PRIMARY KEY, day TEXT, session TEXT);
        CREATE TABLE IF NOT EXISTS plan_hist(at TEXT, kind TEXT, pct REAL, cost REAL, PRIMARY KEY(at, kind));
        CREATE TABLE IF NOT EXISTS recs_hist(day TEXT, id TEXT, status TEXT, impact REAL, metric REAL, PRIMARY KEY(day, id));
        CREATE INDEX IF NOT EXISTS i_msgs_day ON msgs(day);
        CREATE INDEX IF NOT EXISTS i_msgs_session ON msgs(session);
        CREATE INDEX IF NOT EXISTS i_tools_day ON tools(day);
        CREATE INDEX IF NOT EXISTS i_tools_session ON tools(session);
        CREATE INDEX IF NOT EXISTS i_msgs_agent ON msgs(agent);
        CREATE INDEX IF NOT EXISTS i_prompts_session ON prompts(session);
        """
    )
    if "input_head" not in [r[1] for r in con.execute("PRAGMA table_info(tools)")]:
        con.execute("ALTER TABLE tools ADD COLUMN input_head TEXT")
    if "head" not in [r[1] for r in con.execute("PRAGMA table_info(files)")]:
        con.execute("ALTER TABLE files ADD COLUMN head BLOB")
    con.execute("""CREATE TABLE IF NOT EXISTS shapes(session TEXT PRIMARY KEY, ts TEXT, system_chars INTEGER,
        tools_chars INTEGER, deferred INTEGER, parts TEXT)""")
    if "branch" not in [r[1] for r in con.execute("PRAGMA table_info(msgs)")]:
        con.execute("ALTER TABLE msgs ADD COLUMN branch TEXT")
        con.execute("UPDATE files SET offset=0")  # read the history again to fill the branch and the request shapes
        con.commit()
    con.execute("CREATE TABLE IF NOT EXISTS applied(id TEXT PRIMARY KEY, at TEXT, day TEXT, prev TEXT, backup TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS seen(key TEXT PRIMARY KEY, ts TEXT)")
    con.execute("CREATE TABLE IF NOT EXISTS dismissed(id TEXT PRIMARY KEY, day TEXT, impact REAL)")
    return con


def seen_since(con, key, on):
    """When a setting the user made by hand was first seen on, as a UTC timestamp like msgs.ts (None once it is
    gone), to measure from then."""
    if not on:
        con.execute("DELETE FROM seen WHERE key=?", (key,))
        con.commit()
        return None
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    con.execute("INSERT OR IGNORE INTO seen(key, ts) VALUES (?, ?)", (key, now))
    con.commit()
    return con.execute("SELECT ts FROM seen WHERE key=?", (key,)).fetchone()[0]


_SECRET = [
    (re.compile(r"(?i)\b(bearer|basic)\s+\S+"), r"\1 ***"),
    (re.compile(r"(?i)(password|passwd|pwd|secret|token|api[_-]?key|authorization|bearer)(\s*[:=]\s*|\s+)\S+"), r"\1\2***"),
    (re.compile(r"(?i)(?<!\S)(--defaults-extra-file=|-p)(?=\S)\S+"), r"\1***"),
    (re.compile(r"(?i)(://)[^/\s:@]+:[^/\s@]+@"), r"\1***@"),
    (re.compile(r"(?i)(?<!\S)(-u|--user)(\s+)\S+:\S+"), r"\1\2***"),
    (re.compile(r"(?i)(cookie|set-cookie)(\s*:\s*)[^'\"]+"), r"\1\2***"),
    (re.compile(r"(?<![/\w+=-])(?=[A-Za-z0-9+_=-]*\d)(?=[A-Za-z0-9+_=-]*[A-Za-z])[A-Za-z0-9+_=-]{32,}"), "***"),
]


def input_head(name, inp):
    """Short summary of what the tool was asked, with secrets masked (stored locally only)."""
    if not isinstance(inp, dict):
        return ""
    v = None
    for k in ("command", "file_path", "pattern", "url", "query", "skill", "description", "prompt", "path"):
        if isinstance(inp.get(k), str) and inp[k]:
            v = inp[k]
            if k == "pattern" and isinstance(inp.get("path"), str):
                v += "  in " + inp["path"]
            break
    if v is None:
        v = next((x for x in inp.values() if isinstance(x, str)), "")
    v = " ".join(v.split())[:200]
    for rx, sub in _SECRET:
        v = rx.sub(sub, v)
    return v[:160]


def _text_len(content):
    if isinstance(content, str):
        return len(content)
    n = 0
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    n += len(b.get("text") or "")
                elif b.get("type") == "image":
                    n += 6000  # an image weighs in the thousands of tokens
    return n


_FIRST_TYPE = re.compile(rb'\{\s*"type"\s*:\s*"([^"]+)"')
_WANTED_TYPES = {b"assistant", b"user", b"system", b"custom-title", b"api-request-shape"}
_WANTED_ANY = re.compile(rb'"type"\s*:\s*"(?:assistant|user)"|"compact_boundary"')


def shape_parts(shape):
    """What a request carries before the conversation: system prompt and tool definitions, in characters.
    Tools with defer_loading only travel as a name until they are searched for, so they barely count."""
    parts, deferred = {}, 0
    for tl in shape.get("tools") or []:
        if not isinstance(tl, dict):
            continue
        if tl.get("defer_loading"):
            deferred += 1
            continue
        name = tl.get("name") or ""
        key = "mcp:" + name.split("__")[1] if name.startswith("mcp__") and name.count("__") >= 2 else "builtin"
        parts[key] = parts.get(key, 0) + len(json.dumps(tl, ensure_ascii=False))
    system = sum(len(s.get("text") or "") for s in shape.get("system") or [] if isinstance(s, dict))
    return system, parts, deferred


def _handle(con, d, sub=False):
    t = d.get("type")
    if t == "custom-title":
        con.execute("INSERT OR REPLACE INTO titles VALUES(?,?)", (d.get("sessionId"), d.get("customTitle")))
        return
    if t == "api-request-shape":
        if sub or not d.get("sessionId"):
            return  # subagents carry their own, smaller set; the session's fixed cost is the main one
        system, parts, deferred = shape_parts(d.get("shape") or {})
        con.execute("INSERT OR REPLACE INTO shapes VALUES(?,?,?,?,?,?)",
                    (d["sessionId"], d.get("timestamp") or "", system, sum(parts.values()), deferred, json.dumps(parts)))
        return
    ts = d.get("timestamp") or ""
    lt = local(ts)
    if lt is None:
        return
    day, hour = lt.strftime("%Y-%m-%d"), lt.hour
    session, cwd = d.get("sessionId"), d.get("cwd") or ""
    cl = client_of(cwd)
    side = 1 if d.get("isSidechain") else 0
    if t == "system" and d.get("subtype") == "compact_boundary":
        con.execute("INSERT OR IGNORE INTO compacts VALUES(?,?,?)", (d.get("uuid"), day, session))
        return
    msg = d.get("message") or {}
    if t == "assistant":
        u = msg.get("usage") or {}
        model = msg.get("model") or "?"
        if not u or model.startswith("<"):
            return
        cc = u.get("cache_creation") or {}
        cw1h = cc.get("ephemeral_1h_input_tokens") or 0
        cw_total = u.get("cache_creation_input_tokens") or 0
        cw5 = cc.get("ephemeral_5m_input_tokens", cw_total - cw1h) or 0
        think = (u.get("output_tokens_details") or {}).get("thinking_tokens") or 0
        web = (u.get("server_tool_use") or {}).get("web_search_requests") or 0
        mid = msg.get("id") or d.get("uuid")
        con.execute(
            "INSERT OR REPLACE INTO msgs(id, ts, day, hour, session, project, client, model, sidechain, agent,"
            " inp, cw5, cw1h, cr, out, think, web, branch) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (mid, ts, day, hour, session, cwd, cl, model, side, d.get("agentId"),
             u.get("input_tokens") or 0, cw5, cw1h, u.get("cache_read_input_tokens") or 0,
             u.get("output_tokens") or 0, think, web, d.get("gitBranch") or None),
        )
        for b in msg.get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                con.execute(
                    "INSERT OR IGNORE INTO tools(id,msg_id,day,session,client,name,sidechain,input_head) VALUES(?,?,?,?,?,?,?,?)",
                    (b.get("id"), mid, day, session, cl, b.get("name") or "?", side, input_head(b.get("name"), b.get("input"))),
                )
    elif t == "user":
        content = msg.get("content")
        has_result = False
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    has_result = True
                    con.execute(
                        "UPDATE tools SET result_chars=?, is_error=? WHERE id=?",
                        (_text_len(b.get("content")), 1 if b.get("is_error") else 0, b.get("tool_use_id")),
                    )
        if not has_result and not side and not d.get("isMeta"):
            text = content if isinstance(content, str) else ""
            if isinstance(content, list):
                text = " ".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
            s = text.lstrip()
            if s and not s.startswith(("<command-", "<local-command", "<system-reminder", "<task-notification", "Caveat:")):
                con.execute("INSERT OR IGNORE INTO prompts VALUES(?,?,?,?,?)", (d.get("uuid"), day, session, cl, len(text)))


HEAD_BYTES = 256
BATCH_LINES = 5000


def _ingest_file(con, path, off, size, head):
    """Read complete lines from off, committing every BATCH_LINES so the write lock is never held for long.
    The offset only advances together with what was saved, so a database error just retries later."""
    n = 0
    sub = os.sep + "subagents" + os.sep in path
    try:
        with open(path, "rb") as f:
            f.seek(off)
            pending = 0
            for line in f:
                if not line.endswith(b"\n"):
                    break  # still being written: next time
                off += len(line)
                # cheap prefilter: big records (api-request-blob…) embed "type":"user" in their payload,
                # so when "type" is the first key its value decides; otherwise look for the wanted types
                m = _FIRST_TYPE.match(line)
                if m:
                    if m.group(1) not in _WANTED_TYPES:
                        continue
                elif not _WANTED_ANY.search(line):
                    continue
                try:
                    _handle(con, json.loads(line), sub)
                    n += 1
                    pending += 1
                except (ValueError, TypeError, AttributeError, KeyError):
                    pass  # malformed or unexpected record: skip it
                if pending >= BATCH_LINES:
                    con.execute("INSERT OR REPLACE INTO files(path, offset, size, head) VALUES(?,?,?,?)", (path, off, size, head))
                    con.commit()
                    pending = 0
        con.execute("INSERT OR REPLACE INTO files(path, offset, size, head) VALUES(?,?,?,?)", (path, off, size, head))
        con.commit()
    except sqlite3.Error:
        con.rollback()
    except OSError:
        pass
    return n


def ingest():
    global _last_ingest
    with _lock:
        con = connect()
        known = {p: (o, h) for p, o, h in con.execute("SELECT path, offset, head FROM files")}
        n_lines = 0
        for root, _dirs, files in os.walk(PROJECTS):
            for fn in files:
                if not fn.endswith(".jsonl"):
                    continue
                path = os.path.join(root, fn)
                off, old_head = known.get(path, (0, None))
                try:
                    size = os.path.getsize(path)
                    if size == off:
                        continue
                    with open(path, "rb") as f:
                        head = f.read(HEAD_BYTES)
                except OSError:
                    continue
                # transcripts only grow; if it shrank or its start changed, it was rewritten: read it again
                if size < off or (old_head and head[: len(old_head)] != bytes(old_head)):
                    off = 0
                n_lines += _ingest_file(con, path, off, size, head)
        if n_lines or not _last_ingest:
            reclassify(con)
        con.close()
        _last_ingest = time.time()
        return n_lines


def backfill():
    """Fill input_head on already-ingested calls (one-off, reads every transcript)."""
    con = connect()
    n = 0
    for root, _dirs, files in os.walk(PROJECTS):
        for fn in files:
            if not fn.endswith(".jsonl"):
                continue
            try:
                f = open(os.path.join(root, fn), "rb")
            except OSError:
                continue
            with f:
                for line in f:
                    if b'"tool_use"' not in line or b'"type":"assistant"' not in line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    for b in (d.get("message") or {}).get("content") or []:
                        if isinstance(b, dict) and b.get("type") == "tool_use":
                            n += con.execute("UPDATE tools SET input_head=? WHERE id=? AND input_head IS NULL",
                                             (input_head(b.get("name"), b.get("input")), b.get("id"))).rowcount
            con.commit()
    con.close()
    return n


def agent_meta(session, agent):
    for f in glob.glob(os.path.join(PROJECTS, "*", session or "?", "subagents", f"agent-{agent}.meta.json")):
        try:
            return read_json(f)
        except Exception:
            pass
    return {}


def load_prices():
    try:
        p = read_json(PRICES)
    except Exception:
        p = {}
    return p


def _currency_cfg():
    # old Spanish block _moneda {por_defecto, eur_por_usd, fecha} is still read
    pr = load_prices()
    m = pr.get("_currency") or pr.get("_moneda") or {}
    g = lambda new, old: m.get(new) if new in m else m.get(old)
    try:
        rate = float(g("eur_per_usd", "eur_por_usd") or 0)
    except (TypeError, ValueError):
        rate = 0
    default = str(_env("CURRENCY", None) or _env("MONEDA", None) or g("default", "por_defecto") or "USD").upper()
    return rate, (default if default in ("USD", "EUR") and (default == "USD" or rate > 0) else "USD"), g("date", "fecha") or ""


EUR_RATE, CUR_DEFAULT, EUR_DATE = _currency_cfg()
_cur = threading.local()
_lang = threading.local()
LANG_DEFAULT = str(_env("LANG", "en")).lower()
if LANG_DEFAULT not in ("en", "es"):
    LANG_DEFAULT = "en"


def set_lang(code):
    _lang.code = code if code in ("en", "es") else LANG_DEFAULT


def lang():
    return getattr(_lang, "code", LANG_DEFAULT)


def tr(en, es):
    return es if lang() == "es" else en


def set_currency(code):
    _cur.code = code if code in ("USD", "EUR") and (code == "USD" or EUR_RATE > 0) else CUR_DEFAULT


def currency():
    """Amounts are computed and stored in USD (official price list); they are only converted for display."""
    code = getattr(_cur, "code", CUR_DEFAULT)
    return {"code": code, "sym": "€" if code == "EUR" else "$", "rate": EUR_RATE if code == "EUR" else 1.0,
            "eur_rate": EUR_RATE, "eur_date": EUR_DATE}


def sym():
    return currency()["sym"]


def fnum(v, d=0):
    """Fixed decimals; decimal comma in Spanish."""
    t = f"{v:.{d}f}"
    return t.replace(".", ",") if lang() == "es" else t


def money(v, d=0):
    c = currency()
    return f"{fnum((v or 0) * c['rate'], d)} {c['sym']}"


def cost_terms(prices):
    """SQL expressions for the equivalent USD cost per token type, by model."""
    default = prices.get("_default", {"input": 4, "output": 20, "cache_read": 0.2, "cache_write_5m": 5, "cache_write_1h": 8})
    # Longest name first: "claude-opus-5-5" before "claude-opus-5"; the %…% match also covers Bedrock/Vertex prefixes.
    # a model missing a field takes it from _default instead of breaking the dashboard
    models = sorted(((m, {**default, **p}) for m, p in prices.items() if not m.startswith("_") and isinstance(p, dict)),
                    key=lambda x: -len(x[0]))
    fields = {
        "inp": lambda p: f"inp*{p['input']}",
        "out": lambda p: f"out*{p['output']}",
        "cr": lambda p: f"cr*{p['cache_read']}",
        "cw": lambda p: f"(cw5*{p['cache_write_5m']} + cw1h*{p['cache_write_1h']})",
    }
    terms = {}
    for k, f in fields.items():
        whens = " ".join(f"WHEN model LIKE '%{m}%' THEN {f(p)}" for m, p in models)
        terms[k] = f"((CASE {whens} ELSE {f(default)} END)/1e6)"
    return terms


def cost_expr(prices):
    t = cost_terms(prices)
    return "(" + " + ".join(t.values()) + ")"


def q(con, sql, args=()):
    cur = con.execute(sql, args)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def unpriced(models, prices):
    """Models with no entry in prices.json: they are being valued at _default."""
    keys = [k for k in prices if not k.startswith("_")]
    return [m for m in models if m.get("model") not in (None, "", "?") and not any(k in m["model"] for k in keys)]


def _cell(v):
    """A session title comes from a prompt: never let Excel or Sheets run it as a formula."""
    return "'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


def report(month, by="session", client=ALL):
    """CSV for one month (YYYY-MM), by session or by client, in USD and in the selected currency."""
    import csv
    import io
    con = connect()
    try:
        C = cost_expr(load_prices())
        w, a = "substr(m.day,1,7) = ?", [month]
        if client != ALL:
            w += " AND m.client = ?"
            a.append(client)
        if by == "client":
            rows = q(con, f"""SELECT m.client, COUNT(DISTINCT m.session) sessions,
                (SELECT COUNT(*) FROM prompts p WHERE substr(p.day,1,7)=? AND p.client=m.client) messages,
                SUM(m.inp) input, SUM(m.cw5+m.cw1h) cache_write, SUM(m.cr) cache_read, SUM(m.out) output, SUM({C}) cost
                FROM msgs m WHERE {w} GROUP BY m.client ORDER BY cost DESC""", [month] + a)
            head = ["client", "sessions", "messages"]
        else:
            rows = q(con, f"""SELECT m.client, MIN(m.day) date, m.session, COALESCE(t.title,'') title, MIN(m.project) project, COALESCE(MAX(m.branch),'') branch,
                GROUP_CONCAT(DISTINCT m.model) models,
                (SELECT COUNT(*) FROM prompts p WHERE p.session=m.session AND substr(p.day,1,7)=?) messages,
                SUM(m.inp) input, SUM(m.cw5+m.cw1h) cache_write, SUM(m.cr) cache_read, SUM(m.out) output, SUM({C}) cost
                FROM msgs m LEFT JOIN titles t ON t.session=m.session WHERE {w}
                GROUP BY m.session ORDER BY m.client, date""", [month] + a)
            head = ["client", "date", "session", "title", "project", "branch", "models", "messages"]
    finally:
        con.close()
    rate = _currency_cfg()[0]
    eur = currency()["code"] == "EUR" and rate > 0
    out = io.StringIO()
    wr = csv.writer(out)
    wr.writerow(head + ["input_tokens", "cache_write_tokens", "cache_read_tokens", "output_tokens", "cost_usd"]
                + (["cost_eur"] if eur else []))
    for r in rows:
        r["client"] = tr("others", "otros") if r["client"] == OTHERS else r["client"]
        if "project" in r:
            r["project"] = short_path(r["project"])
        wr.writerow([_cell(r[k]) for k in head] + [r["input"], r["cache_write"], r["cache_read"], r["output"], round(r["cost"] or 0, 4)]
                    + ([round((r["cost"] or 0) * rate, 4)] if eur else []))
    return out.getvalue()


def pace(con, C, cw_, ca_, now=None):
    """This month so far, where it ends at the pace of the last 7 full days, and last month for comparison.
    Plus the most unusual recent day: over twice your median day and over your 90th percentile (with at least
    10 active days to compare and a floor of 1 USD, so light users are not nagged), with the session behind it."""
    now = now or datetime.now()
    today = now.date()
    first = today.replace(day=1)
    prev_first = (first - timedelta(days=1)).replace(day=1)
    days_in = (first.replace(month=first.month % 12 + 1, year=first.year + first.month // 12) - first).days
    by = {r["day"]: r["cost"] or 0 for r in q(con, f"""SELECT day, SUM({C}) cost FROM msgs WHERE day >= ?{cw_}
        GROUP BY day""", [(today - timedelta(days=60)).isoformat()] + ca_)}
    cost = lambda d: by.get(d.isoformat(), 0)
    mtd_before = sum(cost(first + timedelta(days=i)) for i in range((today - first).days))
    rate = sum(cost(today - timedelta(days=i)) for i in range(1, 8)) / 7
    proj = mtd_before + max(cost(today), rate) + rate * (days_in - today.day)
    last = q(con, f"SELECT COALESCE(SUM({C}),0) c FROM msgs WHERE day >= ? AND day < ?{cw_}",
             [prev_first.isoformat(), first.isoformat()] + ca_)[0]["c"]
    out = {"month": mtd_before + cost(today), "proj": proj, "rate": rate, "last_month": last,
           "month_name": first.isoformat()[:7], "odd": None}
    past = sorted(v for d, v in by.items() if d < (today - timedelta(days=7)).isoformat() and v > 0.01)
    if len(past) >= 10:
        med, p90 = past[len(past) // 2], past[min(len(past) - 1, int(len(past) * 0.9))]
        bar = max(2 * med, p90, 1.0)
        odd = [(cost(d) / med, d) for d in (today - timedelta(days=i) for i in range(7)) if cost(d) > bar]
        if odd:
            ratio, d = max(odd)
            top = q(con, f"""SELECT m.session, t.title, SUM({C.replace('model', 'm.model')}) cost FROM msgs m
                LEFT JOIN titles t ON t.session = m.session WHERE m.day = ?{cw_.replace('client', 'm.client')}
                GROUP BY m.session ORDER BY cost DESC LIMIT 1""", [d.isoformat()] + ca_)
            out["odd"] = {"day": d.isoformat(), "cost": cost(d), "median": med, "ratio": ratio,
                          "session": top[0]["session"] if top else None, "title": top[0]["title"] if top else None,
                          "session_cost": top[0]["cost"] if top else 0}
    return out


def data(days, client, cut=CUT_DEFAULT):
    if time.time() - _last_ingest > 20:
        ingest()
    con = connect()
    prices = load_prices()
    C = cost_expr(prices)
    T = cost_terms(prices)
    since = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    today = datetime.now().strftime("%Y-%m-%d")
    w, a = "day >= ?", [since]
    if client != ALL:
        w += " AND client = ?"
        a.append(client)
    tot = "inp+cw5+cw1h+cr+out"
    # thinking is included in out at the same price; only the column changes
    T_think = T["out"].replace("out*", "think*")
    ctx = "(inp+cw5+cw1h+cr)"
    # Estimate: what the context above the limit cost, on main-conversation turns
    OV = (f"(CASE WHEN sidechain=0 AND {ctx}>{CTX_LIMIT} THEN ({T['cr']}+{T['cw']}+{T['inp']})"
          f"*(1-{CTX_LIMIT}.0/{ctx}) ELSE 0 END)")
    cw_, ca_ = ("", []) if client == ALL else (" AND client = ?", [client])

    def kpi(since_day):
        ww, aa = "day >= ?", [since_day]
        if client != ALL:
            ww += " AND client = ?"
            aa.append(client)
        r = q(con, f"""SELECT COALESCE(SUM({tot}),0) tokens, COALESCE(SUM({C}),0) cost,
            COALESCE(SUM(cr),0) cr, COALESCE(SUM(inp+cw5+cw1h+cr),0) ctx, COUNT(*) turns,
            COUNT(DISTINCT session) sessions, COALESCE(SUM(out),0) out, COALESCE(SUM(think),0) think,
            COALESCE(SUM(CASE WHEN sidechain=1 THEN {C} END),0) sub_cost, COALESCE(SUM({OV}),0) over,
            COALESCE(SUM({T['cr']}),0) c_cr, COALESCE(SUM({T['cw']}),0) c_cw, COALESCE(SUM({T['out']}),0) c_out,
            COALESCE(SUM({T_think}),0) c_think, COALESCE(SUM(cw1h),0) cw1h, COALESCE(SUM(cw5),0) cw5,
            COALESCE(SUM(sidechain),0) sub_turns
            FROM msgs WHERE {ww}""", aa)[0]
        r["main_turns"] = r["turns"] - r["sub_turns"]
        r["compacts"] = q(con, f"""SELECT COUNT(*) n FROM compacts WHERE day >= ? AND session IN
            (SELECT DISTINCT session FROM msgs WHERE {ww})""", [since_day] + aa)[0]["n"]
        big = q(con, f"""SELECT m.session, t.title, MAX(CASE WHEN m.sidechain=0 THEN {ctx} END) peak
            FROM msgs m LEFT JOIN titles t ON t.session=m.session WHERE {ww.replace('day', 'm.day').replace('client', 'm.client')}
            GROUP BY m.session HAVING peak > {CTX_LIMIT} ORDER BY peak DESC""", aa)
        r["big_sessions"] = len(big)
        r["big_top"] = big[0] if big else None
        r["tool_chars"] = q(con, f"SELECT COALESCE(SUM(result_chars),0) n FROM tools WHERE {ww}", aa)[0]["n"]
        r["prompts"] = q(con, f"SELECT COUNT(*) n FROM prompts WHERE {ww}", aa)[0]["n"]
        r["cache_pct"] = (100.0 * r["cr"] / r["ctx"]) if r["ctx"] else 0
        r["cost_per_prompt"] = r["cost"] / r["prompts"] if r["prompts"] else 0
        return r

    res = {
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "days": days, "client": client,
        "kpi_today": kpi(today),
        "pace": pace(con, C, cw_, ca_),
        "kpi_range": kpi(since),
        "daily": q(con, f"""SELECT day, SUM(inp) inp, SUM(cw5+cw1h) cw, SUM(cr) cr, SUM(out) out,
            SUM({C}) cost, SUM({T['cr']}) c_cr, SUM({T['cw']}) c_cw, SUM({T['inp']}) c_inp,
            SUM({T['out']}) c_out, SUM(CASE WHEN sidechain=0 THEN inp+cw5+cw1h+cr END)*1.0
              / NULLIF(SUM(CASE WHEN sidechain=0 THEN 1 END),0) avg_ctx,
            SUM(think) think FROM msgs WHERE {w} GROUP BY day ORDER BY day""", a),
        "models": q(con, f"""SELECT model, COUNT(*) turns, SUM({tot}) tokens, SUM({C}) cost,
            SUM(think) think, SUM(out) out FROM msgs WHERE {w} GROUP BY model ORDER BY cost DESC""", a),
        "clients": q(con, f"""SELECT client, SUM({tot}) tokens, SUM({C}) cost, COUNT(DISTINCT session) sessions
            FROM msgs WHERE {w} GROUP BY client ORDER BY cost DESC""", a),
        "sessions": q(con, f"""SELECT m.session, t.title, m.project, MIN(m.ts) first, MAX(m.ts) last,
            SUM(CASE WHEN m.sidechain=0 THEN 1 ELSE 0 END) turns, SUM({C}) cost,
            MAX(CASE WHEN m.sidechain=0 THEN m.inp+m.cw5+m.cw1h+m.cr END) peak_ctx,
            SUM(CASE WHEN m.sidechain=1 THEN {C} ELSE 0 END) sub_cost,
            SUM(CASE WHEN m.sidechain=0 THEN {T['cr']} ELSE 0 END) c_cr,
            SUM(CASE WHEN m.sidechain=0 THEN {T['cw']} ELSE 0 END) c_cw,
            SUM(CASE WHEN m.sidechain=0 THEN {T['inp']} ELSE 0 END) c_inp,
            SUM(CASE WHEN m.sidechain=0 THEN {T['out']} - {T_think} ELSE 0 END) c_out,
            SUM(CASE WHEN m.sidechain=0 THEN {T_think} ELSE 0 END) c_think, SUM({OV}) over,
            (SELECT COUNT(*) FROM prompts p WHERE p.session=m.session) prompts,
            (SELECT COUNT(*) FROM compacts c WHERE c.session=m.session) compacts
            FROM msgs m LEFT JOIN titles t ON t.session=m.session
            WHERE {w.replace('day', 'm.day').replace('client', 'm.client')}
            GROUP BY m.session ORDER BY cost DESC LIMIT 60""", a),
        "tools": q(con, f"""SELECT name, COUNT(*) calls, SUM(result_chars) chars, AVG(result_chars) avg_chars,
            MAX(result_chars) max_chars, SUM(is_error) errors, SUM(sidechain) in_sub
            FROM tools WHERE {w} GROUP BY name ORDER BY chars DESC LIMIT 18""", a),
    }
    def period(frm, to):
        r = q(con, f"""SELECT COALESCE(SUM({C}),0) cost, COUNT(*) turns,
            COALESCE(SUM(CASE WHEN sidechain=1 THEN {C} END),0) sub_cost, COALESCE(SUM({OV}),0) over,
            AVG(CASE WHEN sidechain=0 THEN {ctx} END) avg_ctx, COUNT(DISTINCT session) sessions
            FROM msgs WHERE day BETWEEN ? AND ?{cw_}""", [frm, to] + ca_)[0]
        r["prompts"] = q(con, f"SELECT COUNT(*) n FROM prompts WHERE day BETWEEN ? AND ?{cw_}", [frm, to] + ca_)[0]["n"]
        n = (date.fromisoformat(to) - date.fromisoformat(frm)).days + 1
        r.update(frm=frm, to=to, days=n, cost_day=r["cost"] / n,
                 cost_per_prompt=r["cost"] / r["prompts"] if r["prompts"] else 0,
                 turns_per_prompt=r["turns"] / r["prompts"] if r["prompts"] else 0,
                 sub_pct=100 * r["sub_cost"] / r["cost"] if r["cost"] else 0,
                 over_pct=100 * r["over"] / r["cost"] if r["cost"] else 0)
        return r

    try:
        cutd = min(date.fromisoformat(cut), date.today())
    except ValueError:
        cutd = date.fromisoformat(CUT_DEFAULT)
    n = (date.today() - cutd).days + 1
    res["compare"] = {"cut": cutd.isoformat(),
                      "before": period((cutd - timedelta(days=n)).isoformat(), (cutd - timedelta(days=1)).isoformat()),
                      "after": period(cutd.isoformat(), date.today().isoformat())}

    # Subagents one by one
    ag = q(con, f"""SELECT agent, session, GROUP_CONCAT(DISTINCT model) models, COUNT(*) turns, SUM({C}) cost,
        MAX({ctx}) peak_ctx, MIN(ts) first FROM msgs WHERE sidechain=1 AND agent IS NOT NULL AND {w}
        GROUP BY agent ORDER BY cost DESC LIMIT 15""", a)
    if ag:
        ph = ",".join("?" * len(ag))
        tch = {r["agent"]: r for r in q(con, f"""SELECT y.agent, COUNT(*) calls, SUM(x.result_chars) chars
            FROM tools x JOIN msgs y ON y.id=x.msg_id WHERE y.agent IN ({ph}) GROUP BY y.agent""", [r["agent"] for r in ag])}
        titles = dict(con.execute(f"SELECT session, title FROM titles WHERE session IN ({ph})", [r["session"] for r in ag]))
        for r in ag:
            m = agent_meta(r["session"], r["agent"])
            r.update(type=m.get("agentType"), description=m.get("description"), title=titles.get(r["session"]),
                     calls=(tch.get(r["agent"]) or {}).get("calls", 0), chars=(tch.get(r["agent"]) or {}).get("chars", 0))
    res["agents"] = ag
    res["opus_agents"] = q(con, f"""SELECT COUNT(DISTINCT agent) n, COALESCE(SUM({C}),0) cost FROM msgs
        WHERE sidechain=1 AND model LIKE '%opus%' AND day >= ? AND {w}""", [res["compare"]["cut"]] + a)[0]

    # Sessions active right now (independent of the selected period)
    since_ts = (datetime.now(timezone.utc) - timedelta(minutes=LIVE_MIN)).strftime("%Y-%m-%dT%H:%M:%S")
    live = []
    for (sid,) in con.execute(f"SELECT DISTINCT session FROM msgs WHERE ts >= ? AND sidechain=0{cw_}", [since_ts] + ca_):
        last = q(con, f"SELECT {ctx} ctx, ts, model, project FROM msgs WHERE session=? AND sidechain=0 ORDER BY ts DESC LIMIT 1", [sid])[0]
        tot_ = q(con, f"""SELECT SUM({C}) cost, SUM({OV}) over, SUM(CASE WHEN sidechain=1 THEN {C} ELSE 0 END) sub_cost
            FROM msgs WHERE session=?""", [sid])[0]
        tt = con.execute("SELECT title FROM titles WHERE session=?", [sid]).fetchone()
        live.append({"session": sid, "title": tt and tt[0], **last, **tot_,
                     "compacts": con.execute("SELECT COUNT(*) FROM compacts WHERE session=?", [sid]).fetchone()[0],
                     "prompts": con.execute("SELECT COUNT(*) FROM prompts WHERE session=?", [sid]).fetchone()[0],
                     "warn_ctx": last["ctx"] > CTX_LIMIT, "warn_cost": (tot_["cost"] or 0) > COST_WARN})
    res["live"] = sorted(live, key=lambda x: x["ts"], reverse=True)
    res["today_hours"] = q(con, f"SELECT hour, SUM({C}) cost FROM msgs WHERE day=?{cw_} GROUP BY hour", [today] + ca_)
    res["limits"] = {"ctx": CTX_LIMIT, "cost_warn": COST_WARN, "live_min": LIVE_MIN}

    res["branches"] = branches(con, w, a, C)
    res["fixed"] = fixed_cost(con, w, a, T, res["kpi_range"]["cost"])
    res["recs"] = recommendations(con, w, a, res["compare"]["cut"], res["kpi_range"], C, T, ctx)
    mcp = mcp_rec(con, w, a, res["fixed"])
    if mcp:
        res["recs"].append(mcp)
    chain = main_chain(con, w, a, T)
    cfg = read_config()
    for rec in (cache_miss_rec(con, chain), tool_output_rec(con, w, a, chain, cfg), ttl_rec(con, chain, prices, cfg),
                error_loops_rec(con, w, a, C), claude_md_rec(res["fixed"])):
        if rec:
            res["recs"].append(rec)

    ids = [x["session"] for x in res["sessions"]]
    if ids:
        tw = w + " AND session IN (" + ",".join("?" * len(ids)) + ")"
        top = {}
        for r in q(con, f"""SELECT session, name, COUNT(*) calls, SUM(result_chars) chars
                FROM tools WHERE {tw} GROUP BY session, name""", a + ids):
            if (r["chars"] or 0) > (top.get(r["session"], {}).get("chars") or 0):
                top[r["session"]] = r
        for x in res["sessions"]:
            t = top.get(x["session"])
            x["top_tool"] = t and {"name": t["name"], "calls": t["calls"], "chars": t["chars"]}

    mf = model_fit(con, w, a, C)
    for x in res["sessions"]:
        x["fit"] = mf["by_session"].get(x["session"])
    if mf["rec"]:
        res["recs"].append(mf["rec"])
    first = con.execute("SELECT MIN(day) FROM msgs").fetchone()[0]
    span = max(1, min(days, (date.today() - date.fromisoformat(first)).days + 1)) if first else days
    funnel(con, res["recs"], span, res["kpi_range"]["cost"] / span * 7)
    res["plan"] = plan_info(con, C, tot, res["live"])
    res["unpriced"] = unpriced(res["models"], prices)
    order = {"todo": 0, "warn": 1, "info": 2, "ok": 3}
    res["recs"].sort(key=lambda x: (order[x["status"]], -(x["impact"] or 0)))
    res["currency"] = currency()
    res["update"] = update_info()
    res["lang"] = lang()
    attach_history(con, res["recs"])
    attach_applied(con, res["recs"])
    rf = recs_file_path()
    res["recs_file"] = "~" + rf[len(HOME):] if rf.startswith(HOME + os.sep) else rf
    con.close()
    res["insights"] = insights(res)
    return res


_TASK = re.compile(r"(?<![A-Za-z0-9])([A-Z][A-Z0-9]+-[0-9]+)(?![0-9])|(?<![0-9])([0-9]{3,})(?![0-9])")


def task_of(branch):
    """A ticket in the branch name (feature/7067, fix/ABC-123-login) is the task; otherwise there is none."""
    m = _TASK.search(branch or "")
    return (m.group(1) or m.group(2)) if m else None


def branches(con, w, a, C):
    """Cost by git branch: whoever works with tickets in branch names sees tasks; everyone else, branches.
    Sessions outside a git repo have no branch and are left out (they still count everywhere else)."""
    raw = q(con, f"""SELECT branch, project, client, SUM({C}) cost, GROUP_CONCAT(DISTINCT session) sids,
        MIN(day) first, MAX(day) last FROM msgs WHERE branch IS NOT NULL AND branch NOT IN ('', 'HEAD') AND {w}
        GROUP BY branch, project, client""", a)
    # a ticket spans repos (front, back...) and is one line; other branches are grouped by name, listing their repos
    out = {}
    for r in raw:
        task = task_of(r["branch"])
        repo = os.path.basename((r["project"] or "").rstrip("/\\"))
        g = out.setdefault(("t", task) if task else ("b", r["branch"]),
                           {"branch": r["branch"], "task": task, "repos": set(), "clients": set(), "sids": set(),
                            "branches": set(), "cost": 0.0, "first": r["first"], "last": r["last"]})
        g["repos"].add(repo)
        g["branches"].add(r["branch"])
        if r["client"] and r["client"] != OTHERS:
            g["clients"].add(r["client"])
        g["sids"].update((r["sids"] or "").split(","))
        g["cost"] += r["cost"] or 0
        g["first"], g["last"] = min(g["first"], r["first"]), max(g["last"], r["last"])
    rows = []
    for g in out.values():
        g["sessions"] = len(g.pop("sids"))
        br = sorted(g.pop("branches"))
        g["branch"] = br[0] if len(br) == 1 else ", ".join(br)
        g["repos"], g["clients"] = sorted(g["repos"]), sorted(g["clients"])
        rows.append(g)
    return sorted(rows, key=lambda x: -x["cost"])[:200]


def claude_md_chars(path, depth=0):
    """Size of a CLAUDE.md plus the files it pulls in with @path lines (Claude Code inlines them)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return 0
    n = len(text)
    if depth < 3:
        for m in re.finditer(r"(?m)^\s*@(\S+)\s*$", text):
            p = os.path.expanduser(m.group(1))
            n += claude_md_chars(p if os.path.isabs(p) else os.path.join(os.path.dirname(path), p), depth + 1)
    return n


def fixed_cost(con, w, a, T, total):
    """What every session pays before you type anything: system prompt, tool definitions, MCP servers,
    CLAUDE.md, memory and skills. It is measured as the context of the session's first reply, and every
    later reply reads it again from the cache (cheap, but on every single turn)."""
    # the smallest context a session ever had: its first reply, or the one right after a compaction;
    # resumed sessions start with the whole conversation, so their first reply is not the fixed part
    mins = sorted(x for (x,) in con.execute(f"""SELECT MIN(inp+cw5+cw1h+cr) FROM msgs
        WHERE sidechain=0 AND (inp+cw5+cw1h+cr)>0 AND {w} GROUP BY session""", a))
    med = mins[len(mins) // 2] if mins else 0  # caps the sessions that were only ever seen resumed
    base = f"""SELECT session, MIN(MIN(inp+cw5+cw1h+cr), {int(med)}) base, MIN(model) model,
        SUM(cw1h) > SUM(cw5) h1 FROM msgs
        WHERE sidechain=0 AND (inp+cw5+cw1h+cr)>0 AND {w} GROUP BY session"""
    reread = T["cr"].replace("cr*", "b.base*").replace("model", "msgs.model")
    r = q(con, f"""SELECT COUNT(DISTINCT b.session) sessions, COALESCE(SUM({reread}),0) reread, COUNT(*) turns
        FROM msgs JOIN ({base}) b ON b.session=msgs.session
        WHERE msgs.sidechain=0 AND {w.replace('day', 'msgs.day').replace('client', 'msgs.client')}""", a + a)[0]
    # written to the cache once per session; every later turn only reads it
    write = T["cw"].replace("(cw5*", "(base*(1-h1)*").replace("cw1h*", "base*h1*")  # at the TTL the session used
    one = q(con, f"SELECT COALESCE(SUM({write}),0) c, COALESCE(AVG(base),0) base FROM ({base})", a)[0]
    wr, r["base"] = one["c"], one["base"]
    cost = (r["reread"] or 0) + (wr or 0)
    shapes = q(con, f"""SELECT s.system_chars, s.parts, s.deferred FROM shapes s
        WHERE s.session IN (SELECT DISTINCT session FROM msgs WHERE {w})""", a)
    parts, n = {}, len(shapes)
    for s in shapes:
        parts["system"] = parts.get("system", 0) + (s["system_chars"] or 0) / 4 / n
        for k, v in json.loads(s["parts"] or "{}").items():
            parts[k] = parts.get(k, 0) + v / 4 / n
    if n:
        g = claude_md_chars(os.path.join(HOME, ".claude", "CLAUDE.md")) / 4
        if g and r["base"] - sum(parts.values()) > g:
            parts["claude_md"] = g
    known = sum(parts.values())
    if n and r["base"] > known:
        parts["rest"] = r["base"] - known  # CLAUDE.md, memory, skill list, git status and the first message
    servers = {}
    for s in shapes:
        for k, v in json.loads(s["parts"] or "{}").items():
            if k.startswith("mcp:"):
                x = servers.setdefault(k[4:], {"chars": 0, "sessions": 0})
                x["chars"] += v
                x["sessions"] += 1
    servers = {k: {"tokens": round(x["chars"] / 4 / x["sessions"]), "sessions": x["sessions"]} for k, x in servers.items()}
    return {"sessions": r["sessions"], "base": round(r["base"] or 0), "turns": r["turns"], "cost": cost,
            "share": 100 * cost / total if total else 0, "measured": n,
            "deferred": round(sum(s["deferred"] or 0 for s in shapes) / n) if n else 0,
            "parts": sorted(({"k": k, "tokens": round(v)} for k, v in parts.items()), key=lambda x: -x["tokens"]),
            "servers": servers}


def mcp_rec(con, w, a, fx):
    """An MCP server whose tools are loaded in every session but almost never used costs its share of the
    fixed cost on every turn. Only servers above 2K tokens, seen in 3+ sessions, used in under 1 in 5."""
    if not fx["servers"] or not fx["base"]:
        return None
    worst = None
    for srv, s in fx["servers"].items():
        if s["tokens"] < 2000 or s["sessions"] < 3:
            continue
        used = q(con, f"""SELECT COUNT(DISTINCT session) n FROM tools WHERE name LIKE ? ESCAPE '\\' AND {w}""",
                 [("mcp__" + srv + "__").replace("_", "\\_") + "%"] + a)[0]["n"]
        if used * 5 >= s["sessions"]:
            continue
        cost = fx["cost"] * s["tokens"] / fx["base"] * s["sessions"] / max(fx["measured"], 1)
        if not worst or cost > worst[3]:
            worst = (srv, s, used, cost)
    if not worst:
        return None
    srv, s, used, cost = worst
    return {
        "id": "mcp", "title": tr(f"The MCP server {srv} is loaded in every session and almost never used",
                                 f"El servidor MCP {srv} se carga en cada sesión y casi no se usa"),
        "status": "todo", "impact": cost,
        "impact_note": tr("its share of the fixed cost of every session", "su parte del coste fijo de cada sesión"),
        "confidence": tr("medium", "media"),
        "evidence": tr(f"Its tool definitions take ~{fmt_tok(s['tokens'])} tokens in each request of {s['sessions']} session(s); "
                       f"its tools were used in {used}. Every reply reads them again: ~{money(cost, 2)} in the period.",
                       f"Sus definiciones ocupan ~{fmt_tok(s['tokens'])} tokens en cada petición de {s['sessions']} sesión(es); "
                       f"sus herramientas se usaron en {used}. Cada respuesta las vuelve a leer: ~{money(cost, 2)} en el periodo."),
        "action": tr(f"Disable {srv} where you don't need it (/mcp, or enable it per project), or turn on tool search "
                     "so its tools load only when needed.",
                     f"Desactiva {srv} donde no lo necesites (/mcp, o actívalo solo en su proyecto), o activa la búsqueda "
                     "de herramientas para que se carguen solo al usarlas."),
        "verify": tr("The fixed cost card shows that server smaller or gone.", "La tarjeta de coste fijo lo muestra más pequeño o ya no sale."),
        "tradeoff": tr("When you do need it, you enable it again.", "Cuando lo necesites, hay que volver a activarlo."),
        "metric": s["tokens"], "metric_label": tr("tokens per request", "tokens por petición"),
        "tools": [{"n": "/mcp", "k": tr("command", "comando"), "why": tr("enable or disable servers", "activar o desactivar servidores"),
                   "cost": tr("free", "gratis")}],
        "prompt": tr(f"Check where the MCP server {srv} is configured (~/.claude.json, .mcp.json or a plugin) and propose how to "
                     "enable it only in the projects that use it. Show me the diff before saving.",
                     f"Mira dónde está configurado el servidor MCP {srv} (~/.claude.json, .mcp.json o un plugin) y propón cómo "
                     "activarlo solo en los proyectos que lo usan. Enséñame el diff antes de guardar."),
    }
def main_chain(con, w, a, T):
    """Main-conversation requests in order, with what each cache write would have cost at the read price and the
    read price per token, for the detectors that follow a session turn by turn."""
    loss = f"({T['cw']} - {T['cr'].replace('cr*', '(cw5+cw1h)*')})"
    by = {}
    for r in q(con, f"""SELECT id, session, ts, day, model, inp+cw5+cw1h+cr ctx, cw5+cw1h cw, cw5, cw1h, cr, {loss} loss,
            {T['cr'].replace('cr*', '1*')} crp FROM msgs WHERE sidechain=0 AND {w} ORDER BY session, ts""", a):
        r["t"] = local(r["ts"])
        by.setdefault(r["session"], []).append(r)
    return by


def cache_miss_rec(con, chain):
    """The cache is written again in full although the previous request was under a minute or so before: the
    prefix changed. Counted when the write is over 20K tokens and over half the context, the context didn't shrink
    by more than 40% (that is a compaction or /clear) nor grow by more than half (a first big read). The cost is the
    write minus what those tokens would have cost read from the cache. Causes: the model changed, the context was
    trimmed (it shrank a little: old content cleared near the limit), or nothing visible in the logs."""
    causes = {}
    for rows in chain.values():
        for p, r in zip(rows, rows[1:]):
            if not (p["t"] and r["t"]) or (r["t"] - p["t"]).total_seconds() >= 300:
                continue
            if r["cw"] <= 20000 or r["cw"] <= 0.5 * r["ctx"] or not (0.6 * p["ctx"] <= r["ctx"] <= 1.5 * p["ctx"]):
                continue
            k = "model" if r["model"] != p["model"] else "trim" if r["ctx"] < 0.95 * p["ctx"] else "other"
            c = causes.setdefault(k, {"n": 0, "cost": 0.0, "sessions": set(), "days": set()})
            c["n"] += 1
            c["cost"] += max(r["loss"] or 0, 0)
            c["sessions"].add(r["session"])
            c["days"].add(r["day"])
    if not causes:
        return None
    total = sum(c["cost"] for c in causes.values())
    n = sum(c["n"] for c in causes.values())
    main = max(causes, key=lambda k: causes[k]["cost"])
    names = {"model": tr("model changed mid-session", "cambio de modelo a mitad de sesión"),
             "trim": tr("context trimmed near the limit", "contexto recortado cerca del límite"),
             "other": tr("no visible cause (MCP or plugins changed, an update…)", "sin causa visible (cambio de MCP o plugins, una actualización…)")}
    parts = "; ".join(f"{names[k]}: {c['n']} ({money(c['cost'], 1)})" for k, c in sorted(causes.items(), key=lambda x: -x[1]["cost"]))
    action = {
        "model": tr("Pick the model when the session starts and keep it: every switch (/model, plan modes that change model) "
                    "starts the cache from zero for the whole conversation. If you need another model, open a new session.",
                    "Elige el modelo al empezar la sesión y mantenlo: cada cambio (/model, modos de plan que cambian de modelo) "
                    "empieza la caché de cero con toda la conversación. Si necesitas otro modelo, abre una sesión nueva."),
        "trim": tr("Don't let sessions get near the context limit: when they do, old content is cleared and the whole cache is "
                   "written again, several times. Compact or /clear earlier (autoCompactWindow lower, e.g. 300000).",
                   "No dejes que las sesiones se acerquen al límite de contexto: entonces se borra contenido antiguo y la caché "
                   "entera se vuelve a escribir, varias veces. Compacta o haz /clear antes (autoCompactWindow más bajo, p. ej. 300000)."),
        "other": tr("Avoid changing MCP servers, plugins or settings in the middle of a long session, and start a new one after "
                    "updating Claude Code: any change in the tools or the system prompt invalidates the cache.",
                    "Evita cambiar servidores MCP, plugins o ajustes en mitad de una sesión larga, y empieza una nueva después "
                    "de actualizar Claude Code: cualquier cambio en las herramientas o el prompt de sistema invalida la caché."),
    }[main]
    sessions = set().union(*(c["sessions"] for c in causes.values()))
    days = set().union(*(c["days"] for c in causes.values()))
    return {
        "id": "cache-miss", "title": tr("The cache was rebuilt from scratch without a pause",
                                        "La caché se rehízo entera sin que hubiera pausa"),
        "status": "todo", "impact": total,
        "impact_note": tr("measured: each rewrite minus what those tokens cost read from the cache",
                          "medido: cada reescritura menos lo que esos tokens cuestan leídos de la caché"),
        "confidence": tr("high for the amount, medium for the cause", "alta en el importe, media en la causa"),
        "evidence": tr(f"{n} time(s) a request wrote the whole conversation to the cache again less than 5 minutes after the "
                       f"previous one, in {len(sessions)} session(s): {money(total, 1)}. By cause: {parts}.",
                       f"{n} vez(es) una petición volvió a escribir toda la conversación en la caché menos de 5 minutos después "
                       f"de la anterior, en {len(sessions)} sesión(es): {money(total, 1)}. Por causa: {parts}."),
        "action": action,
        "verify": tr("This card's amount drops and the main cause disappears from the list.",
                     "El importe de esta tarjeta baja y la causa principal desaparece de la lista."),
        "tradeoff": tr("None to speak of; it is about when you change things, not what you use.",
                       "Ninguna relevante; es cuándo cambias las cosas, no qué usas."),
        "metric": n, "metric_label": tr("cache rebuilds without a pause", "cachés rehechas sin pausa"),
        "sample": {"sessions": len(sessions), "days": len(days)},
        "tools": [],
        "prompt": "",
    }


def tool_output_rec(con, w, a, chain, cfg):
    """A tool result over ~5K tokens stays in the context and is read again on every later turn, until a
    compaction. Counted for every tool but Read (it has its own card), on the main conversation. The estimate is
    conservative: only the part over 2.5K tokens, only until the next compaction, and only half of it as avoidable."""
    after = {}
    for rows in chain.values():
        nxt = len(rows)
        for i in range(len(rows) - 1, -1, -1):
            if i + 1 < len(rows) and rows[i + 1]["ctx"] < 0.6 * rows[i]["ctx"]:
                nxt = i + 1
            after[rows[i]["id"]] = (nxt - i - 1, rows[i]["crp"] or 0)
    by = {}
    for r in q(con, f"""SELECT name, msg_id, session, day, result_chars FROM tools WHERE sidechain=0 AND name != 'Read'
            AND result_chars > 20000 AND {w}""", a):
        turns, price = after.get(r["msg_id"], (0, 0))
        b = by.setdefault(r["name"], {"n": 0, "cost": 0.0, "sessions": set(), "days": set(), "tokens": 0})
        b["n"] += 1
        b["tokens"] += r["result_chars"] / 4
        b["cost"] += max(r["result_chars"] / 4 - 2500, 0) * turns * price * 0.5
        b["sessions"].add(r["session"])
        b["days"].add(r["day"])
    if not by:
        return None
    total = sum(b["cost"] for b in by.values())
    n = sum(b["n"] for b in by.values())
    top = sorted(by.items(), key=lambda x: -x[1]["cost"])[:3]
    parts = "; ".join(f"{k}: {b['n']} (~{fmt_tok(b['tokens'])} tokens, {money(b['cost'], 2)})" for k, b in top)
    limit = cfg.get("bashOutputMaxChars")
    bash = any(k == "Bash" for k, _ in top)
    tip = (tr(f" bashOutputMaxChars is {limit}; lower it if Bash still leads.", f" bashOutputMaxChars está en {limit}; bájalo si Bash sigue en cabeza.")
           if limit else tr(" Setting bashOutputMaxChars caps what a command can return.",
                            " Con bashOutputMaxChars se pone un tope a lo que puede devolver un comando.")) if bash else ""
    sessions = set().union(*(b["sessions"] for b in by.values()))
    days = set().union(*(b["days"] for b in by.values()))
    return {
        "id": "tool-output", "title": tr("Huge tool outputs stay in the context", "Salidas enormes de herramientas se quedan en el contexto"),
        "status": "todo", "impact": total,
        "impact_note": tr("estimate: tokens over 2.5K × later turns until a compaction × cache read price, halved",
                          "estimación: tokens por encima de 2,5K × turnos siguientes hasta compactar × precio de lectura de caché, a la mitad"),
        "confidence": tr("medium", "media"),
        "evidence": tr(f"{n} tool result(s) over ~5K tokens in {len(sessions)} session(s). Each one is read again on every later "
                       f"turn: ~{money(total, 2)}. Biggest: {parts}.",
                       f"{n} resultado(s) de herramientas de más de ~5K tokens en {len(sessions)} sesión(es). Cada uno se vuelve a "
                       f"leer en cada turno siguiente: ~{money(total, 2)}. Los mayores: {parts}."),
        "action": tr("Ask for only what you need: filter commands (| tail -n 50, grep, --quiet), have scripts print a short "
                     "summary instead of everything, and send long searches to a subagent so only its conclusion comes back." + tip,
                     "Pide solo lo que necesitas: filtra los comandos (| tail -n 50, grep, --quiet), haz que los scripts impriman "
                     "un resumen corto en vez de todo y manda las búsquedas largas a un subagente para que solo vuelva la conclusión." + tip),
        "verify": tr("This card's count and amount drop.", "El número y el importe de esta tarjeta bajan."),
        "tradeoff": tr("Sometimes the full output is what you need; then it is worth it.",
                       "A veces la salida entera es lo que hace falta; entonces compensa."),
        "metric": n, "metric_label": tr("outputs over ~5K tokens", "salidas de más de ~5K tokens"),
        "sample": {"sessions": len(sessions), "days": len(days)},
        "tools": [] if (limit or not bash) else [{"n": "bashOutputMaxChars", "k": tr("setting", "ajuste"),
                  "why": tr("caps what a Bash command can return", "pone tope a lo que devuelve un comando de Bash"),
                  "cost": tr("free", "gratis")}],
        "prompt": tr("Add to my CLAUDE.md a short rule: filter command output (tail, grep, --quiet) and have scripts print a "
                     "summary, never whole logs. Show me the diff before saving.",
                     "Añade a mi CLAUDE.md una regla corta: filtrar la salida de los comandos (tail, grep, --quiet) y que los "
                     "scripts impriman un resumen, nunca logs enteros. Enséñame el diff antes de guardar."),
    }


def price_of(prices):
    """Per-model price lookup in Python, with the same matching as cost_terms (longest name first)."""
    default = {"input": 4, "output": 20, "cache_read": 0.2, "cache_write_5m": 5, "cache_write_1h": 8, **prices.get("_default", {})}
    models = sorted(((m, {**default, **p}) for m, p in prices.items() if not m.startswith("_") and isinstance(p, dict)),
                    key=lambda x: -len(x[0]))
    return lambda model: next((p for m, p in models if m in (model or "")), default)


def ttl_rec(con, chain, prices, cfg):
    """Which cache duration pays off, measured on the user's own pauses. A 1-hour cache costs more to write but keeps
    the conversation warm through pauses of 5 to 60 minutes; a 5-minute one is cheaper to write and is rebuilt after
    each such pause. Sessions are split by the duration they used; the other one is simulated on the same requests,
    and only the tokens the previous request had in context are counted as kept or rebuilt."""
    price = price_of(prices)
    one = {"extra": 0.0, "kept": 0.0, "pauses": 0, "sessions": set(), "days": set()}
    five = {"extra": 0.0, "lost": 0.0, "pauses": 0, "sessions": set(), "days": set()}
    for rows in chain.values():
        h1 = sum(r["cw1h"] for r in rows) > sum(r["cw5"] for r in rows)
        d = one if h1 else five
        for r in rows:
            p = price(r["model"])
            d["extra"] += r["cw"] * (p["cache_write_1h"] - p["cache_write_5m"]) / 1e6
        for prev, r in zip(rows, rows[1:]):
            if not (prev["t"] and r["t"]) or not 300 < (r["t"] - prev["t"]).total_seconds() <= 3600:
                continue
            p = price(r["model"])
            if h1:
                one["kept"] += min(r["cr"], prev["ctx"]) * (p["cache_write_5m"] - p["cache_read"]) / 1e6
            else:
                five["lost"] += min(r["cw"], prev["ctx"]) * (p["cache_write_1h"] - p["cache_read"]) / 1e6
            d["pauses"] += 1
            d["sessions"].add(r["session"])
            d["days"].add(r["day"])
    done = {r["id"]: r["day"] for r in q(con, "SELECT id, day FROM applied WHERE id IN ('ttl-1h', 'ttl-5m')")}
    to5 = one["extra"] - one["kept"] if one["extra"] > one["kept"] and cfg.get("promptCacheTtl") != "5m" else 0
    to1 = five["lost"] - five["extra"] if five["lost"] > five["extra"] and cfg.get("promptCacheTtl") != "1h" else 0
    if not (to5 or to1 or done):
        return None
    rid = "ttl-5m" if (to5 >= to1 if (to5 or to1) else "ttl-5m" in done) else "ttl-1h"
    save = to5 if rid == "ttl-5m" else to1
    d = one if rid == "ttl-5m" else five
    status = "todo" if not done.get(rid) else ("warn" if save else "ok")
    if rid == "ttl-5m":
        evidence = tr(f"Your sessions use the 1-hour cache: writing it cost {money(one['extra'], 1)} more than a 5-minute one, and it "
                      f"only saved {money(one['kept'], 1)} across {one['pauses']} pause(s) of 5 to 60 minutes. Net: {money(save, 1)} lost.",
                      f"Tus sesiones usan la caché de 1 hora: escribirla costó {money(one['extra'], 1)} más que la de 5 minutos, y solo "
                      f"ahorró {money(one['kept'], 1)} en {one['pauses']} pausa(s) de 5 a 60 minutos. Neto: {money(save, 1)} perdidos.")
        action = tr('Set "promptCacheTtl": "5m": you rarely stop between 5 and 60 minutes, so the longer cache doesn\'t pay for itself.',
                    'Pon "promptCacheTtl": "5m": casi nunca paras entre 5 y 60 minutos, así que la caché larga no compensa.')
    else:
        evidence = tr(f"Your sessions use the 5-minute cache and were rebuilt after {five['pauses']} pause(s) of 5 to 60 minutes: "
                      f"{money(five['lost'], 1)}. A 1-hour cache would have cost {money(five['extra'], 1)} more to write. Net: {money(save, 1)} lost.",
                      f"Tus sesiones usan la caché de 5 minutos y se rehicieron tras {five['pauses']} pausa(s) de 5 a 60 minutos: "
                      f"{money(five['lost'], 1)}. La de 1 hora habría costado {money(five['extra'], 1)} más de escribir. Neto: {money(save, 1)} perdidos.")
        action = tr('Set "promptCacheTtl": "1h": you often stop for a few minutes, and each time the whole conversation is written again.',
                    'Pon "promptCacheTtl": "1h": paras a menudo unos minutos y cada vez se vuelve a escribir toda la conversación.')
    if done.get(rid):
        evidence = tr(f"Applied on {done[rid]}. ", f"Aplicado el {done[rid]}. ") + (evidence if save else tr(
            "The sessions in this period no longer lose money on the cache duration.",
            "Las sesiones de este periodo ya no pierden dinero por la duración de la caché."))
    return {
        "id": rid, "title": tr("The cache duration doesn't fit how you pause", "La duración de la caché no encaja con tus pausas"),
        "status": status, "impact": save,
        "impact_note": tr("simulated on your own requests with the other duration, at prices.json",
                          "simulado sobre tus propias peticiones con la otra duración, con prices.json"),
        "confidence": tr("high", "alta"),
        "evidence": evidence, "action": action,
        "verify": tr("A week later this card no longer finds a net loss.", "Una semana después esta tarjeta ya no encuentra pérdida neta."),
        "tradeoff": tr("On a subscription this changes how fast you use your limits, not a bill. Subagents have their own "
                       "setting (subagentPromptCacheTtl), and the CLAUDE_CODE_PROMPT_CACHE_TTL variable wins over this one.",
                       "Con suscripción esto cambia lo rápido que gastas tus límites, no una factura. Los subagentes tienen su "
                       "propio ajuste (subagentPromptCacheTtl), y la variable CLAUDE_CODE_PROMPT_CACHE_TTL manda sobre este."),
        "metric": round(save, 2), "metric_money": True, "metric_label": tr(f"{sym()} lost net", f"{sym()} perdidos netos"),
        "sample": {"sessions": len(d["sessions"]), "days": len(d["days"])} if not done.get(rid) else None,
        "tools": [], "prompt": "",
    }


def error_loops_rec(con, w, a, C):
    """A tool that fails three or more times in a row in the same conversation: each retry is a full request.
    Counted on the main conversation; the cost is that of the requests that issued the retries (not the first try)."""
    rows = q(con, f"""SELECT t.session, t.name, t.is_error, t.day, {C.replace('model', 'm.model')} cost FROM tools t
        JOIN msgs m ON m.id = t.msg_id WHERE t.sidechain=0 AND {w.replace('day', 't.day').replace('client', 't.client')}
        ORDER BY t.session, m.ts""", a)
    loops = {}
    run = []

    def close():
        if len(run) >= 3:
            b = loops.setdefault(run[0]["name"], {"n": 0, "tries": 0, "cost": 0.0, "sessions": set(), "days": set()})
            b["n"] += 1
            b["tries"] += len(run)
            b["cost"] += sum(x["cost"] or 0 for x in run[1:])
            b["sessions"].add(run[0]["session"])
            b["days"].add(run[0]["day"])
    for r in rows:
        if run and (r["session"] != run[-1]["session"] or r["name"] != run[-1]["name"] or not r["is_error"]):
            close()
            run = []
        if r["is_error"]:
            run.append(r)
    close()
    if not loops:
        return None
    total = sum(b["cost"] for b in loops.values())
    n = sum(b["n"] for b in loops.values())
    top = sorted(loops.items(), key=lambda x: -x[1]["cost"])[:3]
    parts = "; ".join(f"{k}: {b['n']} ({b['tries']} tries, {money(b['cost'], 2)})" for k, b in top)
    parts_es = "; ".join(f"{k}: {b['n']} ({b['tries']} intentos, {money(b['cost'], 2)})" for k, b in top)
    sessions = set().union(*(b["sessions"] for b in loops.values()))
    days = set().union(*(b["days"] for b in loops.values()))
    return {
        "id": "error-loops", "title": tr("A tool kept failing and was retried again and again",
                                         "Una herramienta fallaba y se reintentaba una y otra vez"),
        "status": "todo", "impact": total,
        "impact_note": tr("measured: the requests that issued the retries, not the first try",
                          "medido: las peticiones de los reintentos, sin contar el primer intento"),
        "confidence": tr("high for the amount, medium for how much is avoidable", "alta en el importe, media en cuánto se puede evitar"),
        "evidence": tr(f"{n} run(s) of 3 or more failures in a row in {len(sessions)} session(s): {money(total, 2)}. {parts}.",
                       f"{n} racha(s) de 3 o más fallos seguidos en {len(sessions)} sesión(es): {money(total, 2)}. {parts_es}."),
        "action": tr("When the same tool fails twice, stop and change the approach: read the error, check the path, the options or "
                     "the environment, or ask. For scripts and browser automation, keep the ones that work and reuse them.",
                     "Cuando la misma herramienta falla dos veces, para y cambia de enfoque: lee el error, revisa la ruta, las "
                     "opciones o el entorno, o pregunta. Con scripts y automatización del navegador, guarda los que funcionan y reutilízalos."),
        "verify": tr("This card's runs and amount drop.", "Las rachas y el importe de esta tarjeta bajan."),
        "tradeoff": tr("None to speak of.", "Ninguna relevante."),
        "metric": n, "metric_label": tr("runs of 3+ failures", "rachas de 3+ fallos"),
        "sample": {"sessions": len(sessions), "days": len(days)},
        "tools": [],
        "prompt": tr("Add to my ~/.claude/CLAUDE.md a short rule: if the same tool fails twice in a row, stop, explain the error and "
                     "change the approach instead of retrying. Show me the diff before saving.",
                     "Añade a mi ~/.claude/CLAUDE.md una regla corta: si la misma herramienta falla dos veces seguidas, parar, explicar "
                     "el error y cambiar de enfoque en vez de reintentar. Enséñame el diff antes de guardar."),
    }


def claude_md_rec(fx):
    """The global CLAUDE.md (with its @imports) is read on every turn of every session. Above ~2.5K tokens, the part
    over 2K is what could move to skills or files read on demand; half of it is counted, to stay on the safe side."""
    md = next((p["tokens"] for p in fx["parts"] if p["k"] == "claude_md"), 0)
    if md <= 2500 or not fx["base"]:
        return None
    save = fx["cost"] * (md - 2000) / fx["base"] * 0.5
    return {
        "id": "claude-md", "title": tr("Your CLAUDE.md is read on every single turn", "Tu CLAUDE.md se lee en cada turno"),
        "status": "todo", "impact": save,
        "impact_note": tr("its share of the fixed cost, only the part over 2K tokens, halved",
                          "su parte del coste fijo, solo lo que pasa de 2K tokens, a la mitad"),
        "confidence": tr("medium", "media"),
        "evidence": tr(f"~/.claude/CLAUDE.md and its @imports take ~{fmt_tok(md)} tokens and go into every request of "
                       f"{fx['sessions']} session(s). Trimming it to ~2K would save about {money(save, 2)} in the period.",
                       f"~/.claude/CLAUDE.md y sus @imports ocupan ~{fmt_tok(md)} tokens y van en cada petición de "
                       f"{fx['sessions']} sesión(es). Dejarlo en ~2K ahorraría unos {money(save, 2)} en el periodo."),
        "action": tr("Keep in CLAUDE.md only what applies to almost every task. Move procedures and long reference to skills "
                     "(loaded only when used) or to files you point to, and import project-specific files only in that project.",
                     "Deja en CLAUDE.md solo lo que aplica a casi todas las tareas. Pasa procedimientos y referencias largas a "
                     "skills (se cargan solo al usarlas) o a ficheros que se citen, e importa lo de cada proyecto solo en ese proyecto."),
        "verify": tr("The fixed cost card shows CLAUDE.md smaller.", "La tarjeta de coste fijo muestra CLAUDE.md más pequeño."),
        "tradeoff": tr("An instruction moved to a skill is only followed when the skill loads.",
                       "Una instrucción que pasa a una skill solo se sigue cuando la skill se carga."),
        "metric": md, "metric_label": tr("tokens in CLAUDE.md", "tokens en CLAUDE.md"),
        "tools": [],
        "prompt": tr("Read my ~/.claude/CLAUDE.md and its @imports and propose, section by section, what to keep, what to move to "
                     "a skill and what to drop, aiming at under 2K tokens. Don't change anything until I approve the diff.",
                     "Lee mi ~/.claude/CLAUDE.md y sus @imports y propón, sección a sección, qué dejar, qué pasar a una skill y qué "
                     "quitar, para que quede por debajo de 2K tokens. No cambies nada hasta que apruebe el diff."),
    }


# Recommendations the dashboard can apply itself: always a fixed change decided here, never a value sent by the page
APPLY = {
    "ctx": {"autoCompactWindow": 300000},
    "subagent-model": {"env.CLAUDE_CODE_SUBAGENT_MODEL": "sonnet"},
    "ttl-5m": {"promptCacheTtl": "5m"},
    "ttl-1h": {"promptCacheTtl": "1h"},
}
_ABSENT = {"__tokenometro__": "absent"}


def _dig(d, path, value=None, mode="get"):
    keys = path.split(".")
    for k in keys[:-1]:
        if mode == "set" and not isinstance(d.get(k), dict):
            d[k] = {}
        d = d.get(k) if isinstance(d.get(k), dict) else {}
    if mode == "get":
        return d.get(keys[-1], _ABSENT)
    if mode == "set":
        d[keys[-1]] = value
    else:
        d.pop(keys[-1], None)


def change_settings(fn):
    """Edit ~/.claude/settings.json the safe way: a dated backup first, then write aside and swap."""
    path = os.path.join(HOME, ".claude", "settings.json")
    s = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            s = json.load(f)
    if not isinstance(s, dict):
        raise ValueError("settings.json is not a JSON object")
    backup = ""
    if os.path.exists(path):
        backup = path + ".bak-tokenometro-" + datetime.now().strftime("%Y%m%d%H%M%S")
        shutil.copy2(path, backup)
    fn(s)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp-tokenometro"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(s, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    return backup


def apply_rec(rid):
    if rid not in APPLY:
        raise ValueError("this recommendation can't be applied from the dashboard")
    with _lock:
        con = connect()
        try:
            if con.execute("SELECT 1 FROM applied WHERE id=?", (rid,)).fetchone():
                return
            prev = {}

            def fn(s):
                for k, v in APPLY[rid].items():
                    prev[k] = _dig(s, k)
                    _dig(s, k, v, "set")
            backup = change_settings(fn)
            now = datetime.now()
            con.execute("INSERT INTO applied VALUES(?,?,?,?,?)",
                        (rid, now.isoformat(timespec="seconds"), now.strftime("%Y-%m-%d"), json.dumps(prev), backup))
            con.commit()
        finally:
            con.close()


def undo_rec(rid):
    """Put back what was there before, key by key, only where the value is still the one we set:
    anything you changed by hand afterwards is yours and stays."""
    with _lock:
        con = connect()
        try:
            row = con.execute("SELECT prev FROM applied WHERE id=?", (rid,)).fetchone()
            if not row or rid not in APPLY:
                raise ValueError("nothing to undo")
            prev = json.loads(row[0])

            def fn(s):
                for k, v in APPLY[rid].items():
                    if _dig(s, k) == v:
                        _dig(s, k, mode="del") if prev.get(k) == _ABSENT else _dig(s, k, prev.get(k), "set")
            change_settings(fn)
            con.execute("DELETE FROM applied WHERE id=?", (rid,))
            con.commit()
        finally:
            con.close()


def attach_applied(con, recs):
    """Mark what can be applied with one click and, for what was, compare its metric before and after that day."""
    done = {r["id"]: r for r in q(con, "SELECT id, day FROM applied")}
    for x in recs:
        a = done.get(x["id"])
        x["can_apply"] = x["id"] in APPLY and not a and x["status"] == "todo"
        if x["id"] in APPLY:
            x["change"] = ", ".join(f"{k.split('.')[-1]}: {json.dumps(v)}" for k, v in APPLY[x["id"]].items())
        if a:
            m = q(con, """SELECT AVG(CASE WHEN day < ? THEN metric END) before, AVG(CASE WHEN day >= ? THEN metric END) after,
                SUM(day >= ?) days FROM recs_hist WHERE id=? AND day >= date(?, '-14 days')""",
                  [a["day"], a["day"], a["day"], x["id"], a["day"]])[0]
            x["applied"] = {"day": a["day"], "before": m["before"], "after": m["after"], "days": m["days"] or 0}


def session_detail(sid):
    con = connect()
    prices = load_prices()
    C, T = cost_expr(prices), cost_terms(prices)
    T_think = T["out"].replace("out*", "think*")
    ctx = "(inp+cw5+cw1h+cr)"
    r = {"session": sid}
    r["by_model"] = q(con, f"""SELECT model, sidechain, COUNT(*) turns, SUM({C}) cost FROM msgs WHERE session=?
        GROUP BY model, sidechain ORDER BY cost DESC""", [sid])
    r["tools"] = q(con, """SELECT name, COUNT(*) calls, SUM(result_chars) chars, SUM(is_error) errors FROM tools
        WHERE session=? GROUP BY name ORDER BY chars DESC LIMIT 8""", [sid])
    r["big_calls"] = q(con, """SELECT name, input_head, result_chars, sidechain FROM tools WHERE session=?
        ORDER BY result_chars DESC LIMIT 8""", [sid])
    ag = q(con, f"""SELECT agent, GROUP_CONCAT(DISTINCT model) models, COUNT(*) turns, SUM({C}) cost FROM msgs
        WHERE session=? AND sidechain=1 AND agent IS NOT NULL GROUP BY agent ORDER BY cost DESC LIMIT 10""", [sid])
    for x in ag:
        m = agent_meta(sid, x["agent"])
        x.update(type=m.get("agentType"), description=m.get("description"))
    r["agents"] = ag
    # segments between compactions (compacts has no timestamp: detected by the context drop)
    rows = con.execute(f"SELECT ts, sidechain, {ctx}, {C} FROM msgs WHERE session=? ORDER BY ts", [sid]).fetchall()
    segs, cur, prev = [], None, None
    for ts, side, cx, co in rows:
        if cur is None or (not side and prev is not None and cx < prev * 0.5 and prev > 100_000):
            cur = {"from": ts, "turns": 0, "cost": 0.0, "peak": 0}
            segs.append(cur)
        cur["turns"] += 1
        cur["cost"] += co or 0
        if not side:
            cur["peak"] = max(cur["peak"], cx)
            prev = cx
    r["segments"] = segs
    con.close()
    return r


def read_config():
    """Only the keys that affect cost; the whole settings.json is never returned."""
    cfg = {"autoCompactWindow": None, "bashOutputMaxChars": None, "subagent_model": None, "effort": None,
           "opus_plugin_agents": [], "read_rule": False, "read_guard": False, "gap_hook": False,
           "statusline": False, "caveman": False, "cbm": False}
    try:
        st = read_json(os.path.join(HOME, ".claude", "settings.json"))
    except Exception:
        st = {}
    cfg["autoCompactWindow"] = st.get("autoCompactWindow")
    cfg["promptCacheTtl"] = st.get("promptCacheTtl")
    hooks = st.get("hooks") or {}
    hook_sets = [hooks]
    for key, on in (st.get("enabledPlugins") or {}).items():
        if on and "@" in key:
            name, mkt = key.split("@", 1)
            vers = sorted(glob.glob(os.path.join(HOME, ".claude", "plugins", "cache", mkt, name, "*")), key=os.path.getmtime)
            try:
                hook_sets.append(read_json(os.path.join(vers[-1], "hooks", "hooks.json")).get("hooks") or {})
            except Exception:
                pass
    cfg["read_guard"] = any("Read" in (m.get("matcher") or "") for h in hook_sets for m in h.get("PreToolUse") or [])
    cfg["gap_hook"] = any(h.get("UserPromptSubmit") for h in hook_sets)
    cfg["statusline"] = bool(st.get("statusLine"))
    # A plugin (e.g. context-meter) can show the context without touching statusLine
    cfg["ctx_plugins"] = [k.split("@")[0] for k, on in (st.get("enabledPlugins") or {}).items()
                          if on and re.search(r"context[-_]?meter|status[-_]?line|ctx[-_]?bar", k.split("@")[0], re.I)]
    cfg["caveman"] = os.path.isdir(os.path.join(HOME, ".claude", "skills", "caveman"))
    cfg["cbm"] = os.path.exists(os.path.join(HOME, ".local", "bin", "codebase-memory-mcp"))
    cfg["bashOutputMaxChars"] = st.get("bashOutputMaxChars")
    cfg["subagent_model"] = (st.get("env") or {}).get("CLAUDE_CODE_SUBAGENT_MODEL")
    cfg["effort"] = ((st.get("modelSettings") or {}).get("claude-opus-5-5") or {}).get("effortLevel") or st.get("effortLevel")
    for key, on in (st.get("enabledPlugins") or {}).items():
        if not on or "@" not in key:
            continue
        name, mkt = key.split("@", 1)
        vers = sorted(glob.glob(os.path.join(HOME, ".claude", "plugins", "cache", mkt, name, "*")), key=os.path.getmtime)
        for f in glob.glob(os.path.join(vers[-1], "agents", "*.md")) if vers else []:
            try:
                head = read_head(f, 1500)
            except OSError:
                continue
            m = re.search(r"^model:\s*(\S+)", head, re.M)
            if m and "opus" in m.group(1):
                cfg["opus_plugin_agents"].append(f"{name}:{os.path.basename(f)[:-3]}")
    try:
        cfg["read_rule"] = "offset" in read_head(os.path.join(HOME, ".claude", "CLAUDE.md"), -1)
    except OSError:
        pass
    return cfg


EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
DOC_EXT = re.compile(r"\.(md|markdown|txt|json|ya?ml|csv|toml|ini)\s*$", re.I)


def _price_of(prices, word):
    keys = [k for k in prices if not k.startswith("_") and word in k]
    return prices[sorted(keys)[-1]] if keys else None


def model_fit(con, w, a, C):
    """Which model would have been enough in each session, judged by what it did (not what was asked).
    Rule: no edits -> Sonnet (or Haiku if short); few edits and few errors -> opusplan;
    many edits or debugging -> Opus. The saving reprices the same tokens: a smaller model
    may need more turns, so it is an upper bound."""
    prices = load_prices()
    son, hai = _price_of(prices, "sonnet"), _price_of(prices, "haiku")
    out = {"by_session": {}, "rec": None}
    if not son:
        return out
    rows = q(con, f"""SELECT session, SUM(CASE WHEN sidechain=0 THEN 1 ELSE 0 END) turns, SUM({C}) cost,
        SUM(CASE WHEN model LIKE '%opus%' THEN {C} ELSE 0 END) opus_cost,
        SUM(CASE WHEN model LIKE '%opus%' THEN inp ELSE 0 END) inp, SUM(CASE WHEN model LIKE '%opus%' THEN out ELSE 0 END) out,
        SUM(CASE WHEN model LIKE '%opus%' THEN cr ELSE 0 END) cr, SUM(CASE WHEN model LIKE '%opus%' THEN cw5 ELSE 0 END) cw5,
        SUM(CASE WHEN model LIKE '%opus%' THEN cw1h ELSE 0 END) cw1h
        FROM msgs WHERE {w} GROUP BY session""", a)
    ph = ",".join("?" * len(EDIT_TOOLS))
    tl = {}
    for r in q(con, f"""SELECT session, name, input_head, is_error FROM tools WHERE {w} AND (name IN ({ph}) OR name='Bash')""",
               a + list(EDIT_TOOLS)):
        t = tl.setdefault(r["session"], {"edits": 0, "errs": 0, "docs": 0})
        if r["name"] == "Bash":
            t["errs"] += r["is_error"] or 0
        elif DOC_EXT.search(r["input_head"] or ""):
            t["docs"] += 1  # docs, notes, memory or config: does not need the big model
        else:
            t["edits"] += 1
    reprice = lambda p, r: (r["inp"] * p["input"] + r["out"] * p["output"] + r["cr"] * p["cache_read"]
                            + r["cw5"] * p["cache_write_5m"] + r["cw1h"] * p["cache_write_1h"]) / 1e6
    tot = {"sonnet": [0, 0.0], "opusplan": [0, 0.0], "haiku": [0, 0.0], "opus": [0, 0.0]}
    for r in rows:
        if not r["opus_cost"]:
            continue
        t = tl.get(r["session"]) or {"edits": 0, "errs": 0, "docs": 0}
        ed, er = t["edits"] or 0, t["errs"] or 0
        s_save = r["opus_cost"] - reprice(son, r)
        if ed == 0 and r["turns"] <= 10 and r["opus_cost"] < 2 and hai:
            m, save, why = "haiku", r["opus_cost"] - reprice(hai, r), tr("short session, no files touched", "sesión corta sin tocar ficheros")
        elif ed == 0:
            m, save, why = "sonnet", s_save, (tr("only edited docs or config", "solo editó documentación o configuración") if t.get("docs")
                                        else tr("edited no files", "no editó ningún fichero")) + tr(": read, search, review or document", ": leer, buscar, revisar o documentar")
        elif ed < 10 and er < 5:
            m, save, why = "opusplan", s_save / 2, tr(
                f"{ed} code edit(s) and {er} error(s): Opus plans and Sonnet executes (saving estimated at half)",
                f"{ed} edición(es) de código y {er} error(es): Opus planifica y Sonnet ejecuta (ahorro estimado a la mitad)")
        else:
            m, save, why = "opus", 0.0, tr(
                f"{ed} code edits and {er} Bash errors: long work or debugging",
                f"{ed} ediciones de código y {er} errores de Bash: trabajo largo o depuración")
        out["by_session"][r["session"]] = {"model": m, "save": round(save, 2), "why": why, "edits": ed, "errs": er}
        tot[m][0] += 1
        tot[m][1] += save
    saving = tot["sonnet"][1] + tot["opusplan"][1] + tot["haiku"][1]
    if saving >= 5:
        parts = [f"{n} {tr('with', 'con')} {k} (~{money(v, 0)})" for k, (n, v) in tot.items() if n and k != "opus"]
        out["rec"] = {
            "id": "model-fit", "title": tr("Use a smaller model when the task allows it", "Usa un modelo menor cuando la tarea lo permita"),
            "status": "todo", "impact": saving,
            "impact_note": tr("upper bound: same tokens at the smaller model's price; a smaller model may need more turns",
                              "techo: mismos tokens con el precio del modelo menor; un modelo menor puede necesitar más turnos"),
            "confidence": tr("medium (the rule looks at what the session did, not how hard it was)",
                             "media (la regla mira lo que hizo la sesión, no lo difícil que era)"),
            "evidence": tr("Judging by what each Opus session actually did (files edited, errors), these would have been enough: ",
                           "Según lo que hizo cada sesión con Opus (ficheros editados, errores), habrían bastado: ") + ", ".join(parts)
                        + tr(f". {tot['opus'][0]} did need Opus. See the «Cost-effective model» column in «Costliest sessions».",
                             f". {tot['opus'][0]} sí necesitaban Opus. Columna «Modelo rentable» de «Sesiones más caras»."),
            "action": tr("At the start: /model sonnet to review, research, document or take notes; /model opusplan for "
                         "bounded changes; Opus only for debugging and big changes. You can switch mid-session.",
                         "Al empezar: /model sonnet para revisar, investigar, documentar o notas; /model opusplan para cambios "
                         "acotados; Opus solo para depuración y cambios grandes. Se puede cambiar a mitad de sesión."),
            "verify": tr("This card's saving drops week by week and Sonnet's cost grows in «By model».",
                         "El ahorro de esta tarjeta baja semana a semana y crece el coste de Sonnet en «Por modelo»."),
            "tradeoff": tr("Sonnet gets hard code right less often: if you have to redo it, the saving is lost.",
                           "Sonnet acierta menos en código difícil: si toca repetir, el ahorro se pierde."),
            "metric": round(saving, 1),
            "metric_label": tr(f"{sym()} that could have run on a smaller model", f"{sym()} que podían ir con un modelo menor"),
            "metric_money": True,
            "tools": [{"n": "/model", "k": tr("command", "comando"),
                       "why": tr("switches the model of the running session", "cambia el modelo de la sesión en curso"),
                       "cost": tr("free", "gratis")}],
            "prompt": tr("Look at what this task involves and tell me in one line whether Sonnet is enough, whether opusplan "
                         "is better, or whether it needs Opus, with the reason. If a smaller one is enough, remind me of the "
                         "/model command to switch.",
                         "Mira en qué consiste esta tarea y dime en una línea si basta con Sonnet, si conviene opusplan o si "
                         "necesita Opus, con el motivo. Si basta uno menor, recuérdame el comando /model para cambiarlo."),
        }
    return out


SUBMETER_GLOB = os.path.join(HOME, ".claude", "plugins", "store", "sub-meter_*.json")
PLAN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plan.json")  # written by statusline.py


def plan_info(con, C, tot, live):
    """Plan usage from the latest sub-meter reading (5 h and week), and the estimated cap in $ eq.
    The cap is spent/percentage; it includes usage outside Claude Code (claude.ai), so it is approximate."""
    now = datetime.now(timezone.utc)
    iso = lambda t: t.strftime("%Y-%m-%dT%H:%M:%S")
    span = {"five_hour": timedelta(hours=5), "seven_day": timedelta(days=7)}
    fs = sorted(glob.glob(SUBMETER_GLOB) + glob.glob(PLAN_FILE), key=os.path.getmtime)
    d = None
    try:
        d = read_json(fs[-1]) if fs else None
        at = datetime.fromtimestamp(d["at"] / 1000, timezone.utc) if d else None
    except Exception:
        d = None
    def rolling(kind):
        # no valid plan %: only what was spent in the rolling window, which comes from the transcripts
        sp = span[kind]
        r = q(con, f"SELECT COALESCE(SUM({C}),0) c, COALESCE(SUM({tot}),0) t FROM msgs WHERE ts >= ?", [iso(now - sp)])[0]
        for x in live:
            sc = q(con, f"SELECT COALESCE(SUM({C}),0) c FROM msgs WHERE session=? AND ts >= ?", [x["session"], iso(now - sp)])[0]["c"]
            x.setdefault("plan", {})[kind] = {"cost": sc, "pct": None}
        return {"kind": kind, "pct": None, "cost": r["c"], "tokens": r["t"]}

    if not d:
        return {"source": None, "windows": [rolling(k) for k in span]}
    res = {"source": "statusLine" if fs[-1] == PLAN_FILE else "sub-meter", "at": at.astimezone().strftime("%H:%M"), "age_min": int((now - at).total_seconds() // 60), "windows": []}
    for lim in d.get("limits") or []:
        kind = lim.get("kind")
        if kind not in span:
            continue
        try:
            reset = datetime.fromisoformat(lim["resetsAt"].replace("Z", "+00:00"))
            if reset.tzinfo is None:
                reset = reset.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        start, pct = reset - span[kind], float(lim.get("percentUsed") or 0)
        at_cost = q(con, f"SELECT COALESCE(SUM({C}),0) c FROM msgs WHERE ts >= ? AND ts <= ?", [iso(start), iso(at) + "Z"])[0]["c"]
        try:
            con.execute("INSERT OR IGNORE INTO plan_hist VALUES(?,?,?,?)", [iso(at), kind, pct, at_cost])
            con.commit()
        except sqlite3.OperationalError:
            pass  # database busy (long first ingest): this reading is stored next time
        if reset <= now:
            # the reading belongs to a window that already reset: its % no longer says anything about now
            res["windows"].append(rolling(kind))
            continue
        now_ = q(con, f"SELECT COALESCE(SUM({C}),0) c, COALESCE(SUM({tot}),0) t FROM msgs WHERE ts >= ? AND ts < ?", [iso(start), iso(reset)])[0]
        caps = sorted(r[0] * 100 / r[1] for r in con.execute(
            "SELECT cost, pct FROM plan_hist WHERE kind=? AND pct >= 10 AND at >= ? ORDER BY at DESC LIMIT 15",
            [kind, iso(now - timedelta(days=30))]))
        cap = caps[len(caps) // 2] if caps else None
        w = {"kind": kind, "pct": pct, "reset": reset.astimezone().strftime("%d-%m %H:%M"),
             "reset_min": int((reset - now).total_seconds() // 60), "cost": now_["c"], "tokens": now_["t"],
             "cap": cap, "cap_n": len(caps), "left": None, "eta": None, "pct_now": None}
        if cap:
            w["left"] = max(cap - now_["c"], 0)
            w["pct_now"] = min(100 * now_["c"] / cap, 100)
            hours = max((now - start).total_seconds() / 3600, 0.25)
            rate = now_["c"] / hours
            if rate > 0 and w["left"] > 0:
                eta = now + timedelta(hours=w["left"] / rate)
                w["eta"] = eta.astimezone().strftime("%d-%m %H:%M") if eta < reset else None
        res["windows"].append(w)
        for x in live:
            sc = q(con, f"SELECT COALESCE(SUM({C}),0) c FROM msgs WHERE session=? AND ts >= ?", [x["session"], iso(start)])[0]["c"]
            x.setdefault("plan", {})[kind] = {"cost": sc, "pct": 100 * sc / cap if cap else None}
    return res


REPORTS = os.path.join(HOME, ".claude", "reports")


def recs_file_path(day=None):
    # the default language decides the name (the hourly job writes it in that language); es keeps the old name
    name = "mejoras" if LANG_DEFAULT == "es" else "improvements"
    return os.path.join(REPORTS, f"{name}-{day or datetime.now().strftime('%Y-%m-%d')}.md")


def write_recs_file(recs, kpi):
    """Same place and style as /informe-semanal: one .md per day in ~/.claude/reports, rewritten every hour."""
    st = {"todo": tr("Pending", "Pendiente"), "warn": tr("Applied, review", "Aplicada, revisar"),
          "ok": tr("Applied, working", "Aplicada, funciona"), "info": tr("Informational", "Informativa")}
    now = datetime.now()
    lines = [tr("# Claude Code usage improvements", "# Mejoras de consumo de Claude Code") + f" — {now.strftime('%Y-%m-%d')}", "",
             tr(f"Last 7 days, all clients: {money(kpi['cost'], 0)} eq. Generated by the tracker "
                f"({BASE.replace(HOME, '~')}) at {now.strftime('%H:%M')}; rewritten every hour.",
                f"Últimos 7 días, todos los clientes: {money(kpi['cost'], 0)} eq. Generado por el tracker "
                f"({BASE.replace(HOME, '~')}) a las {now.strftime('%H:%M')}; se reescribe cada hora."),
             tr(f"To apply one: copy its prompt into Claude Code. Dashboard: http://127.0.0.1:{PORT}",
                f"Para aplicar una: copia su prompt en Claude Code. Panel: http://127.0.0.1:{PORT}"), ""]
    for n, x in enumerate(recs, 1):
        imp = f" · ≈{money(x['impact'], 0)}" if x.get("impact") else ""
        lines += [f"## {n}. {x['title']} · {st[x['status']]}{imp}", "",
                  f"- **{tr('Evidence', 'Qué he visto')}:** {x['evidence']}", f"- **{tr('What to do', 'Qué hacer')}:** {x['action']}",
                  f"- **{tr('How to check', 'Cómo comprobarlo')}:** {x['verify']}", f"- **{tr('Trade-off', 'Contrapartida')}:** {x['tradeoff']}"]
        for t in x.get("tools") or []:
            lines.append(f"- **{tr('With', 'Con qué')}:** {t['n']} ({t['k']}): {t['why']} · {t['cost']}")
        if x.get("prompt"):
            lines += ["", tr("Prompt for Claude:", "Prompt para Claude:"), "", "```text", x["prompt"], "```"]
        lines.append("")
    os.makedirs(REPORTS, exist_ok=True)
    tmp = recs_file_path() + ".tmp"
    # it contains project paths and prompts: readable only by you
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    os.replace(tmp, recs_file_path())


def digest_path(monday):
    y, wk, _ = monday.isocalendar()
    name = "semana" if LANG_DEFAULT == "es" else "week"
    return os.path.join(REPORTS, f"tokenometro-{name}-{y}-W{wk:02d}.md")


def digest(monday):
    """Weekly summary (Monday to Sunday) as Markdown: spend against the week before, where it went, what changed."""
    sun, prev = monday + timedelta(days=6), monday - timedelta(days=7)
    con = connect()
    try:
        prices = load_prices()
        C, T = cost_expr(prices), cost_terms(prices)
        w, a = "day >= ? AND day <= ?", [monday.isoformat(), sun.isoformat()]
        cost = lambda ww, aa: q(con, f"SELECT COALESCE(SUM({C}),0) c, COUNT(DISTINCT session) s FROM msgs WHERE {ww}", aa)[0]
        now_, before = cost(w, a), cost(w, [prev.isoformat(), (monday - timedelta(days=1)).isoformat()])
        top = q(con, f"""SELECT m.session, COALESCE(t.title,'') title,
            (SELECT x.project FROM msgs x WHERE x.session=m.session ORDER BY x.ts LIMIT 1) project, SUM({C}) cost FROM msgs m
            LEFT JOIN titles t ON t.session=m.session WHERE m.day >= ? AND m.day <= ? GROUP BY m.session ORDER BY cost DESC LIMIT 5""", a)
        br = branches(con, w, a, C)[:5]
        fx = fixed_cost(con, w, a, T, now_["c"])
        st = {}
        for d, rid, status in con.execute("SELECT day, id, status FROM recs_hist WHERE day <= ? ORDER BY day", [sun.isoformat()]):
            st.setdefault(rid, {})["after" if d >= monday.isoformat() else "before"] = status
        models = q(con, f"SELECT model, SUM({C}) cost FROM msgs WHERE {w} GROUP BY model", a)
    finally:
        con.close()
    diff = now_["c"] - before["c"]
    pct = f" ({'+' if diff >= 0 else ''}{100 * diff / before['c']:.0f}%)" if before["c"] else ""
    names = {"todo": tr("pending", "pendiente"), "warn": tr("applied, review", "aplicada, revisar"),
             "ok": tr("applied, working", "aplicada, funciona"), "info": tr("informational", "informativa")}
    L = [tr(f"# Tokenometro · week {monday.isocalendar()[1]} ({monday} to {sun})",
            f"# Tokenometro · semana {monday.isocalendar()[1]} (del {monday} al {sun})"), "",
         tr(f"**{money(now_['c'], 2)} eq.** in {now_['s']} sessions; the week before, {money(before['c'], 2)}{pct}.",
            f"**{money(now_['c'], 2)} eq.** en {now_['s']} sesiones; la semana anterior, {money(before['c'], 2)}{pct}."), ""]
    if top:
        L += [tr("## Costliest sessions", "## Sesiones más caras"), ""]
        L += [f"- {money(r['cost'], 2)} · {r['title'] or r['session'][:8]} · {short_path(r['project'] or '')}" for r in top] + [""]
    if br:
        L += [tr("## By branch / task", "## Por rama / tarea"), ""]
        L += [f"- {money(r['cost'], 2)} · {r['task'] or r['branch']} · {', '.join(r['repos'])}" for r in br] + [""]
    if fx["measured"]:
        L += [tr("## Fixed cost per session", "## Coste fijo por sesión"), "",
              tr(f"~{fmt_tok(fx['base'])} tokens before the conversation starts: {money(fx['cost'], 2)} ({fx['share']:.0f}% of the week).",
                 f"~{fmt_tok(fx['base'])} tokens antes de empezar la conversación: {money(fx['cost'], 2)} ({fx['share']:.0f}% de la semana)."), ""]
    changed = [(rid, v) for rid, v in st.items() if v.get("after") and v.get("after") != v.get("before")]
    if changed:
        L += [tr("## Recommendations that changed", "## Recomendaciones que cambiaron"), ""]
        L += [f"- {rid}: {names.get(v.get('before'), '-')} → {names.get(v['after'], v['after'])}" for rid, v in changed] + [""]
    miss = unpriced(models, prices)
    if miss:
        L += [tr("## Models without a price", "## Modelos sin precio"), ""]
        L += [tr(f"- {m['model']}: valued at _default; add it to prices.json.", f"- {m['model']}: valorado a _default; añádelo a prices.json.")
              for m in miss] + [""]
    L.append(tr(f"Dashboard: http://127.0.0.1:{PORT}", f"Panel: http://127.0.0.1:{PORT}"))
    return "\n".join(L) + "\n"


def write_digest(monday=None):
    """Last week's summary, once: written on the first run after the week ends, never rewritten."""
    if monday is None:
        today = date.today()
        monday = today - timedelta(days=today.weekday() + 7)
    path = digest_path(monday)
    if os.path.exists(path):
        return path
    set_lang(LANG_DEFAULT)
    text = digest(monday)
    os.makedirs(REPORTS, exist_ok=True)
    with os.fdopen(os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(path + ".tmp", path)
    return path


def snapshot_recs():
    """Daily snapshot of the recommendations (7 days, all clients): what lets you see whether they improve."""
    set_lang(LANG_DEFAULT)
    r = data(7, ALL)
    con = connect()
    day = datetime.now().strftime("%Y-%m-%d")
    con.executemany("INSERT OR REPLACE INTO recs_hist VALUES(?,?,?,?,?)",
                    [(day, x["id"], x["status"], x["impact"], x.get("metric")) for x in r["recs"]])
    con.commit()
    con.close()
    write_recs_file(r["recs"], r["kpi_range"])


STUDY_DAYS, STUDY_SESSIONS, STUDY_SHARE, STUDY_FLOOR, STUDY_CAP = 3, 3, 0.01, 0.25, 2.0


def funnel(con, recs, span, week_cost):
    """A pending card is only recommended once it holds up: seen on STUDY_DAYS of the last 7 daily snapshots, saving
    at least STUDY_SHARE of this user's weekly spend (between STUDY_FLOOR and STUDY_CAP USD a week), and, when the
    detector says how many sessions it saw, in at least STUDY_SESSIONS of them on two different days. Relative so a
    light user still gets advice; capped so a heavy user isn't told that 8 USD a month is too little to matter. What falls short waits under «Under study» saying what
    is missing. Applied cards are always shown: they are being tracked. A dismissed card stays hidden until its
    weekly cost doubles."""
    week = (date.today() - timedelta(days=6)).isoformat()
    seen = dict(con.execute("""SELECT id, COUNT(*) FROM recs_hist WHERE day >= ? AND status IN ('todo', 'warn')
                               GROUP BY id""", [week]).fetchall())
    last = dict(con.execute("""SELECT id, impact FROM recs_hist h WHERE day = (SELECT MAX(day) FROM recs_hist
                               WHERE id = h.id)""").fetchall())
    gone = dict(con.execute("SELECT id, impact FROM dismissed").fetchall())
    floor = max(STUDY_FLOOR, min(STUDY_SHARE * week_cost, STUDY_CAP))
    for x in recs:
        x["missing"], x["stage"] = [], "shown"
        if x["status"] != "todo":
            continue
        if x["id"] in gone:
            was, now = gone[x["id"]], last.get(x["id"])
            if not (was and now and now >= 2 * was):
                x["stage"] = "dismissed"
                continue
            con.execute("DELETE FROM dismissed WHERE id=?", (x["id"],))
            con.commit()
        n = seen.get(x["id"], 0)
        if n < STUDY_DAYS:
            x["missing"].append(tr(f"seen on {n} of the last 7 days; needs {STUDY_DAYS}",
                                   f"vista {n} de los últimos 7 días; necesita {STUDY_DAYS}"))
        if x["impact"] is not None:
            wk = x["impact"] / span * 7
            if wk < floor:
                x["missing"].append(tr(f"saves about {money(wk, 2)}/week; needs {money(floor, 2)}/week for your spend",
                                       f"ahorra unos {money(wk, 2)}/semana; con tu gasto necesita {money(floor, 2)}/semana"))
        smp = x.get("sample") or {}
        if smp.get("sessions") is not None and (smp["sessions"] < STUDY_SESSIONS or smp.get("days", 0) < 2):
            x["missing"].append(tr(f"found in {smp['sessions']} session(s) on {smp.get('days', 0)} day(s); needs {STUDY_SESSIONS} on 2 days",
                                   f"encontrada en {smp['sessions']} sesión(es) y {smp.get('days', 0)} día(s); necesita {STUDY_SESSIONS} en 2 días"))
        if x["missing"]:
            x["stage"] = "study"
    return recs


def _rec_id(rid):
    if not re.fullmatch(r"[a-z0-9-]{1,40}", str(rid)):
        raise ValueError("unknown recommendation")
    return str(rid)


def dismiss_rec(rid):
    """«Not for me»: hidden until its cost doubles; the last snapshot's impact is the reference."""
    rid = _rec_id(rid)
    con = connect()
    try:
        row = con.execute("SELECT impact FROM recs_hist WHERE id=? ORDER BY day DESC LIMIT 1", (rid,)).fetchone()
        con.execute("INSERT OR REPLACE INTO dismissed VALUES (?, ?, ?)",
                    (rid, date.today().isoformat(), row[0] if row else None))
        con.commit()
    finally:
        con.close()


def restore_rec(rid):
    rid = _rec_id(rid)
    con = connect()
    try:
        con.execute("DELETE FROM dismissed WHERE id=?", (rid,))
        con.commit()
    finally:
        con.close()


def attach_history(con, recs):
    rows = con.execute("SELECT id, day, status, impact, metric FROM recs_hist ORDER BY day").fetchall()
    by = {}
    for i, d, st, imp, met in rows:
        by.setdefault(i, []).append({"d": d, "s": st, "i": imp, "m": met})
    today = datetime.now().strftime("%Y-%m-%d")
    week = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    pend = ("todo", "warn")
    for x in recs:
        h = by.get(x["id"], [])
        x["hist"] = [{"d": e["d"], "s": e["s"]} for e in h[-14:]]
        since = None
        for e in reversed(h):
            if (e["s"] in pend) != (x["status"] in pend):
                break
            since = e["d"]
        x["since"] = since
        old = next((e for e in h if week <= e["d"] < today), None)
        x["trend"] = {"d": old["d"], "m": old["m"], "i": old["i"]} if old else None
    return recs


def recommendations(con, w, a, cut, k, C, T, ctx):
    """Improvements based on the period data and the real configuration. Ordered: pending ones by impact."""
    cfg = read_config()
    recs = []
    cost = k["cost"] or 0
    limit = cfg["autoCompactWindow"] or CTX_LIMIT

    # 1) Context above the limit / autocompact
    after_big = q(con, f"""SELECT COUNT(DISTINCT session) n FROM msgs WHERE sidechain=0 AND day >= ? AND {ctx} > ? AND {w}""",
                  [cut, limit * 1.1] + a)[0]["n"]
    applied = bool(cfg["autoCompactWindow"])
    seen = (["statusLine"] if cfg["statusline"] else []) + cfg["ctx_plugins"]
    ss = f" AND session IN (SELECT DISTINCT session FROM msgs WHERE {w})"
    cmp_ = q(con, f"""SELECT SUM(day >= ?) af, SUM(day < ?) bf FROM compacts WHERE 1=1 {ss}""", [cut, cut] + a)[0]
    dd = q(con, f"SELECT COUNT(DISTINCT CASE WHEN day >= ? THEN day END) af, COUNT(DISTINCT CASE WHEN day < ? THEN day END) bf FROM msgs WHERE {w}",
           [cut, cut] + a)[0]
    n_af = dd["af"] or 0
    rate = lambda n, d: f"{fnum((n or 0)/d, 1)}{tr('/day', '/día')}" if d else "-"
    recs.append({
        "id": "ctx", "title": tr(f"Long conversations cost extra (context above {fmt_tok(limit)})",
                                 f"Las conversaciones largas cuestan de más (contexto sobre {fmt_tok(limit)})"),
        "status": ("ok" if not after_big else "warn") if applied else "todo",
        "impact": k["over"],
        "impact_note": tr("upper bound: cost of the resent excess; compacting costs a summary and rewriting the cache",
                          "techo: coste del exceso reenviado; compactar cuesta un resumen y reescribir la caché"),
        "confidence": tr("medium", "media"),
        "evidence": tr(f"{k['big_sessions']} session(s) grew past {fmt_tok(limit)} tokens. Every reply resends the whole conversation, "
                       f"so the excess cost ~{money(k['over'], 0)} ({100*k['over']/cost if cost else 0:.0f}% of the period). "
                       f"Since {cut}: {after_big} session(s) still went above {fmt_tok(limit*1.1)}, because autocompact only steps in there. "
                       f"Compactions (Claude summarising the chat): {rate(cmp_['bf'], dd['bf'])} before the cut → {rate(cmp_['af'], n_af)} after; "
                       "each one costs a summary and a cache rewrite.",
                       f"{k['big_sessions']} sesión(es) pasaron de {fmt_tok(limit)} tokens. Cada respuesta reenvía toda la conversación, "
                       f"así que el exceso costó ~{money(k['over'], 0)} ({100*k['over']/cost if cost else 0:.0f}% del periodo). "
                       f"Desde el {cut}: {after_big} sesión(es) aún pasaron de {fmt_tok(limit*1.1)}, porque el autocompact solo actúa ahí. "
                       f"Compactaciones (Claude resume la charla): {rate(cmp_['bf'], dd['bf'])} antes del corte → {rate(cmp_['af'], n_af)} después; "
                       "cada una cuesta un resumen y reescribir la caché.")
                    + (tr(f" You see the context with {', '.join(seen)}.", f" Ves el contexto con {', '.join(seen)}.") if seen else ""),
        "action": (tr(f"You already have autoCompactWindow={cfg['autoCompactWindow']}", f"Ya tienes autoCompactWindow={cfg['autoCompactWindow']}")
                   + (tr(f" and {', '.join(seen)}: when it turns yellow", f" y {', '.join(seen)}: cuando se ponga en amarillo") if seen
                      else tr(". Before reaching the limit", ". Antes de llegar al límite")) +
                   tr(", save the state to memory and /clear, instead of letting it compact.",
                      ", guarda el estado en memoria y /clear, en vez de dejar que compacte.")
                   if applied else tr('"autoCompactWindow": 300000 in ~/.claude/settings.json and /clear when switching tasks.',
                                      '"autoCompactWindow": 300000 en ~/.claude/settings.json y /clear al cambiar de tarea.')),
        "verify": tr("Compactions per day go down: every timely /clear is one compaction fewer.",
                     "Las compactaciones por día bajan: cada /clear a tiempo es una compactación menos."),
        "tradeoff": tr("Every compaction loses conversation detail: it may force re-reading files.",
                       "Cada compactación pierde detalle de la conversación: puede obligar a releer ficheros."),
        "metric": k["compacts"], "metric_label": tr("compactions in the period", "compactaciones en el periodo"), "sample_days": n_af,
        "tools": [{"n": "/context", "k": tr("command", "comando"),
                   "why": tr("see how much the session takes before continuing", "ver cuánto ocupa la sesión antes de seguir"),
                   "cost": tr("free", "gratis")}],
        "prompt": tr("Before continuing, save this task's state to Auto Memory (done, decisions, files touched, "
                     "tests and their result, next step) in the task's file, without duplicating what is already there. "
                     "Tell me in one line where it was saved so I can /clear and resume from there.",
                     "Antes de seguir, guarda en Auto Memory el estado de esta tarea (hecho, decisiones, ficheros tocados, "
                     "pruebas y su resultado, siguiente paso) en el fichero de la tarea, sin duplicar lo que ya esté. "
                     "Dime en una línea dónde quedó para hacer /clear y retomarla desde ahí.") if applied else
                  tr("With the update-config skill, add \"autoCompactWindow\": 300000 to ~/.claude/settings.json without touching "
                     "any other key. Show me the diff before saving.",
                     "Con la skill update-config, añade \"autoCompactWindow\": 300000 a ~/.claude/settings.json sin tocar "
                     "ninguna otra clave. Enséñame el diff antes de guardar."),
    })

    # 2) Long pauses: the cache expires and is rewritten in full
    def pauses(where, args):
        rows = con.execute(f"""SELECT session, ts, {T['cw']} FROM msgs WHERE sidechain=0 AND {where} ORDER BY session, ts""",
                           args).fetchall()
        n, cost, prev = 0, 0.0, None
        for sid, ts, cwc in rows:
            t = local(ts)
            if prev and prev[0] == sid and t and prev[1] and (t - prev[1]).total_seconds() > 3600:
                n += 1
                cost += cwc or 0
            prev = (sid, t)
        return n, cost

    gaps, gcost = pauses(w, a)
    hook_ts = seen_since(con, "gap_hook", cfg["gap_hook"])
    if hook_ts:
        # The hook doesn't stop pauses, it stops paying for them: what counts is the weekly cost of pauses after
        # it compared with the four weeks before. Judged after a full week, so one bad day doesn't decide it.
        on = local(hook_ts)
        hook_day = on.strftime("%Y-%m-%d")
        days_on = (datetime.now(timezone.utc) - on).total_seconds() / 86400
        n_after, c_after = pauses("ts >= ?", [hook_ts])
        first = local(con.execute("SELECT MIN(ts) FROM msgs WHERE sidechain=0").fetchone()[0] or hook_ts)
        span = min(28.0, (on - first).total_seconds() / 86400)
        frm = (on - timedelta(days=span)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        _, c_before = pauses("ts >= ? AND ts < ?", [frm, hook_ts]) if span >= 1 else (0, 0.0)
        wk_after = c_after / max(days_on, 1) * 7
        wk_before = c_before / span * 7 if span >= 1 else 0.0
        good = wk_after <= max(wk_before * 0.5, 0.5)
        ready = (on + timedelta(days=7)).strftime("%Y-%m-%d")
        status = "ok" if days_on >= 7 and good else "warn"
        if days_on < 7:
            verdict = tr(f"It will count as working on {ready} if the weekly cost of pauses stays under half of before "
                         f"(or under {money(0.5, 2)}).",
                         f"Se dará por funcionando el {ready} si el coste semanal de las pausas queda por debajo de la mitad "
                         f"que antes (o de {money(0.5, 2)}).")
        elif good:
            verdict = tr("Working: pauses now cost less than half of before.",
                         "Funciona: las pausas cuestan ahora menos de la mitad que antes.")
        else:
            verdict = tr("Still high: when the warning shows, save the state and /clear instead of resending.",
                         "Sigue alto: cuando salte el aviso, guarda el estado y haz /clear en vez de reenviar.")
        evidence = tr(f"Hook on since {hook_day}. Since then, {n_after} pause(s) over 1 h cost {money(c_after, 1)} "
                      f"({money(wk_after, 1)}/week; before, {money(wk_before, 1)}/week). {verdict}",
                      f"Hook puesto desde el {hook_day}. Desde entonces, {n_after} pausa(s) de más de 1 h costaron "
                      f"{money(c_after, 1)} ({money(wk_after, 1)}/semana; antes, {money(wk_before, 1)}/semana). {verdict}")
        metric, label, impact = n_after, tr("pauses since the hook", "pausas desde el hook"), c_after
    else:
        status, metric, label, impact = "todo", gaps, tr("pauses over 1 h", "pausas de más de 1 h"), gcost
        evidence = tr(f"{gaps} time(s) you resumed a session after more than 1 h idle. The cache had expired, so the first reply had to "
                      f"save the whole conversation again: {money(gcost, 1)}.",
                      f"{gaps} vez(es) retomaste una sesión tras más de 1 h parada. La caché había caducado, así que la primera respuesta "
                      f"tuvo que guardar otra vez toda la conversación: {money(gcost, 1)}.")
    if gaps or hook_ts:
        recs.append({
            "id": "gaps", "title": tr("Pauses over 1 h make the cache expire", "Las pausas de más de 1 h hacen caducar la caché"),
            "status": status, "impact": impact,
            "impact_note": tr("measured: cache write of the first turn after each pause",
                              "medido: escritura de caché del primer turno tras cada pausa"),
            "confidence": tr("high", "alta"),
            "evidence": evidence,
            "action": tr("Before leaving (lunch, meeting, end of day): /compact, or save the state to memory and /clear when you come back. "
                         "Resuming a huge session the next day is the most expensive case.",
                         "Antes de irte (comida, reunión, fin del día): /compact, o guarda el estado en memoria y al volver /clear. "
                         "Retomar una sesión enorme al día siguiente es lo más caro."),
            "verify": tr("A week after adding the hook, the weekly cost of pauses is under half of the four weeks before.",
                         "Una semana después de poner el hook, el coste semanal de las pausas queda en menos de la mitad que en las "
                         "cuatro semanas anteriores."),
            "tradeoff": tr("None to speak of; /clear requires leaving the state written down.",
                           "Ninguna relevante; /clear exige dejar el estado apuntado."),
            "metric": metric, "metric_label": label,
            "tools": [] if hook_ts else [{"n": "Hook UserPromptSubmit", "k": "hook",
                      "why": tr("warns you when you return from a long pause, before you pay for the cache rewrite",
                                "te avisa al volver de una pausa larga, antes de pagar la reescritura de la caché"),
                      "cost": tr("0 tokens: runs locally", "0 tokens: corre en local")}],
            "prompt": "" if hook_ts else tr(
                "With the update-config skill, create a UserPromptSubmit hook in ~/.claude/settings.json (script in "
                "~/.claude/hooks/, only bash or standard-library python3). It must read transcript_path from the input "
                "JSON, check the timestamp of the last assistant reply (the current prompt may already be written) and, if more than 60 minutes have passed and the transcript "
                "is over 1 MB, block the message ONCE per session (exit 2, marker in a temp file) with: "
                "\"The cache has expired. Save the state and /clear, or resend the message to continue\". On any "
                "error, exit 0 with no output. Test it with a sample JSON before enabling it and show me the diff.",
                "Con la skill update-config, crea un hook UserPromptSubmit en ~/.claude/settings.json (script en "
                "~/.claude/hooks/, solo bash o python3 de la biblioteca estándar). Debe leer transcript_path del JSON de "
                "entrada, mirar la marca de tiempo de la última respuesta del asistente (el prompt actual puede estar ya escrito) y, si han pasado más de 60 minutos y el transcript "
                "pesa más de 1 MB, bloquear el mensaje UNA sola vez por sesión (exit 2, marca en un fichero temporal) con: "
                "«La caché ha caducado. Guarda el estado y haz /clear, o reenvía el mensaje para seguir». En cualquier "
                "error, exit 0 sin salida. Pruébalo con un JSON de ejemplo antes de activarlo y enséñame el diff."),
        })

    # 3) Subagents on Opus despite CLAUDE_CODE_SUBAGENT_MODEL
    opus = q(con, f"""SELECT agent, session, SUM({C}) cost FROM msgs WHERE sidechain=1 AND model LIKE '%opus%' AND day >= ? AND {w}
                      GROUP BY agent""", [cut] + a)
    by_type = {}
    for x in opus:
        t = agent_meta(x["session"], x["agent"]).get("agentType") or "?"
        by_type[t] = by_type.get(t, 0) + (x["cost"] or 0)
    if cfg["subagent_model"] and (by_type or cfg["opus_plugin_agents"]):
        tot = sum(by_type.values())
        recs.append({
            "id": "opus-agents", "title": tr("Helper agents still use Opus, the priciest model", "Hay subagentes que siguen usando Opus, el modelo más caro"),
            "status": "todo" if by_type else "info", "impact": tot * 0.5,
            "impact_note": tr("Sonnet costs half of Opus (prices.json)", "Sonnet cuesta la mitad que Opus (prices.json)"),
            "confidence": tr("medium", "media"),
            "evidence": ((tr(f"Since {cut}: ", f"Desde el {cut}: ") + ", ".join(f"{t} {money(c, 1)}" for t, c in sorted(by_type.items(), key=lambda x: -x[1]))
                         + ". ") if by_type else "") +
                        tr(f"These agents set model: opus in their own definition, which overrides CLAUDE_CODE_SUBAGENT_MODEL={cfg['subagent_model']}: ",
                           f"Estos agentes fijan model: opus en su definición, lo que anula CLAUDE_CODE_SUBAGENT_MODEL={cfg['subagent_model']}: ")
                        + ", ".join(cfg["opus_plugin_agents"][:6]) + ".",
            "action": tr("Do not edit the plugin cache (it is overwritten on update). Either ask in the global CLAUDE.md to pass "
                         "model: sonnet when launching those agents, or copy the agent to ~/.claude/agents/ with model: sonnet and use the copy.",
                         "No editar la caché del plugin (se pisa al actualizar). O bien pedir en el CLAUDE.md global que al lanzar "
                         "esos agentes se pase model: sonnet, o copiar el agente a ~/.claude/agents/ con model: sonnet y usar la copia."),
            "verify": tr("This card's amount drops to 0 and «Costliest subagents» shows them on Sonnet.",
                         "El importe de esta tarjeta baja a 0 y «Subagentes más caros» los muestra con Sonnet."),
            "tradeoff": tr("These are code reviews: Sonnet may find fewer issues. Keep Opus only on the final reviewer if you value it more.",
                           "Son revisiones de código: Sonnet puede encontrar menos. Dejar Opus solo en el revisor final si se valora más."),
            "metric": round(tot, 2),
            "metric_label": tr(f"{sym()} of subagents on Opus since the cut", f"{sym()} de subagentes con Opus desde el corte"),
            "metric_money": True,
            "tools": [{"n": "~/.claude/agents/", "k": tr("own agent", "agente propio"),
                       "why": tr("a copy with model: sonnet is not overwritten when the plugin updates",
                                 "una copia con model: sonnet no se pisa al actualizar el plugin"),
                       "cost": tr("half per call", "la mitad por llamada")}],
            "prompt": tr("Copy these agents to ~/.claude/agents/ from the latest version of the plugin cache: "
                         + ", ".join(cfg["opus_plugin_agents"][:6]) + ". In each copy change model: opus to "
                         "model: sonnet in the frontmatter and add the suffix -sonnet to the name. Do not touch the plugin cache. "
                         "Then add a line to ~/.claude/CLAUDE.md so the -sonnet copies are used unless I ask for Opus, and tell me how to invoke them.",
                         "Copia a ~/.claude/agents/ estos agentes desde la última versión de la caché de plugins: "
                         + ", ".join(cfg["opus_plugin_agents"][:6]) + ". En cada copia cambia en el frontmatter model: opus por "
                         "model: sonnet y añade el sufijo -sonnet al name. No toques la caché del plugin. Después añade una "
                         "línea a ~/.claude/CLAUDE.md para que se usen las copias -sonnet salvo que pida Opus, y dime cómo invocarlas."),
        })

    # 3b) No default model for subagents: they inherit the main one, often Opus
    mine = con.execute("SELECT day FROM applied WHERE id='subagent-model'").fetchone()
    if (not cfg["subagent_model"] and by_type) or mine:
        since = mine[0] if mine else cut
        after = q(con, f"""SELECT COALESCE(SUM({C}),0) c FROM msgs WHERE sidechain=1 AND model LIKE '%opus%' AND day >= ? AND {w}""",
                  [since] + a)[0]["c"] if mine else sum(by_type.values())
        recs.append({
            "id": "subagent-model", "title": tr("Subagents run on the main model (Opus) by default",
                                                "Los subagentes usan por defecto el modelo principal (Opus)"),
            "status": ("ok" if after < 0.5 else "warn") if mine else "todo", "impact": after * 0.5,
            "impact_note": tr("Sonnet costs half of Opus (prices.json)", "Sonnet cuesta la mitad que Opus (prices.json)"),
            "confidence": tr("medium", "media"),
            "evidence": tr(f"Since {since}, subagents on Opus cost {money(after, 2)}. Without CLAUDE_CODE_SUBAGENT_MODEL each subagent "
                           "uses the model of the session that launched it, even for searching or reading files.",
                           f"Desde el {since}, los subagentes con Opus costaron {money(after, 2)}. Sin CLAUDE_CODE_SUBAGENT_MODEL cada subagente "
                           "usa el modelo de la sesión que lo lanza, aunque solo sea para buscar o leer ficheros."),
            "action": tr('Set "env": {"CLAUDE_CODE_SUBAGENT_MODEL": "sonnet"} in ~/.claude/settings.json; an agent that needs Opus can still ask for it.',
                         'Pon "env": {"CLAUDE_CODE_SUBAGENT_MODEL": "sonnet"} en ~/.claude/settings.json; un agente que necesite Opus puede seguir pidiéndolo.'),
            "verify": tr("«Costliest subagents» shows Sonnet and this card's amount drops.",
                         "«Subagentes más caros» muestra Sonnet y el importe de esta tarjeta baja."),
            "tradeoff": tr("Subagents doing reviews or hard research may do a bit worse.",
                           "Los subagentes que revisan código o investigan algo difícil pueden hacerlo algo peor."),
            "metric": round(after, 2), "metric_money": True,
            "metric_label": tr(f"{sym()} of subagents on Opus", f"{sym()} de subagentes con Opus"),
            "tools": [],
            "prompt": tr('With the update-config skill, add "env": {"CLAUDE_CODE_SUBAGENT_MODEL": "sonnet"} to ~/.claude/settings.json '
                         "without touching any other key. Show me the diff before saving.",
                         'Con la skill update-config, añade "env": {"CLAUDE_CODE_SUBAGENT_MODEL": "sonnet"} a ~/.claude/settings.json '
                         "sin tocar ninguna otra clave. Enséñame el diff antes de guardar."),
        })

    # 4) Large and repeated reads of the same file
    rr = q(con, f"""SELECT session, input_head f, COUNT(*) n, SUM(result_chars) ch FROM tools
                    WHERE name='Read' AND result_chars > 20000 AND input_head IS NOT NULL AND {w}
                    GROUP BY session, input_head HAVING n > 1 ORDER BY ch DESC""", a)
    big = q(con, f"SELECT COUNT(*) n, COALESCE(SUM(result_chars),0) ch FROM tools WHERE name='Read' AND result_chars > 40000 AND {w}", a)[0]
    if rr or big["n"]:
        rep_tok = sum(x["ch"] for x in rr) / 4
        ex = rr[0] if rr else None
        recs.append({
            "id": "reads", "title": tr("Reading whole files fills the context", "Leer ficheros enteros llena el contexto"),
            "status": "warn" if cfg["read_rule"] else "todo", "impact": None,
            "impact_note": tr("cannot be converted to money exactly: it fills the context and brings compaction forward",
                              "no se puede pasar a dinero con exactitud: llena el contexto y adelanta la compactación"),
            "confidence": tr("high", "alta"),
            "evidence": tr(f"{big['n']} read(s) of over ~10K tokens (~{fmt_tok(big['ch']/4)} in total); each one stays in the conversation and is resent on every reply. "
                           f"{len(rr)} large file(s) were read several times in the same session (~{fmt_tok(rep_tok)})"
                           + (f", e.g. {os.path.basename(ex['f'])} ×{ex['n']}" if ex else "") + "."
                           + (" The read-by-range rule is already in your global CLAUDE.md and it still happens." if cfg["read_rule"] else ""),
                           f"{big['n']} lectura(s) de más de ~10K tokens (~{fmt_tok(big['ch']/4)} en total); cada una se queda en la conversación y se reenvía en cada respuesta. "
                           f"{len(rr)} fichero(s) grandes se leyeron varias veces en la misma sesión (~{fmt_tok(rep_tok)})"
                           + (f", p. ej. {os.path.basename(ex['f'])} ×{ex['n']}" if ex else "") + "."
                           + (" La regla de leer por rangos ya está en tu CLAUDE.md global y aun así pasa." if cfg["read_rule"] else "")),
            "action": tr("The written rule is not enough: a PreToolUse hook on Read that rejects (exit 2) reading files over ~400 lines "
                         "without a limit, with the message «use Grep, then offset/limit».",
                         "La regla escrita no basta: un hook PreToolUse sobre Read que rechace (exit 2) leer sin limit "
                         "ficheros de más de ~400 líneas, con el mensaje «usa Grep y luego offset/limit»."),
            "verify": tr("«Calls that returned the most text» no longer has Reads of over 10K tokens.",
                         "«Llamadas que más texto devolvieron» deja de tener Read de más de 10K tokens."),
            "tradeoff": tr("The occasional legitimate whole-file read will have to be done in chunks.",
                           "Algún caso legítimo de leer el fichero entero tendrá que hacerse por tramos."),
            "metric": big["n"], "metric_label": tr("reads of over ~10K tokens", "lecturas de más de ~10K tokens"),
            "tools": ([] if cfg["read_guard"] else [{"n": tr("PreToolUse hook on Read", "Hook PreToolUse sobre Read"), "k": "hook",
                       "why": tr("stops the whole-file read before it enters the context", "corta la lectura entera antes de que entre en el contexto"),
                       "cost": tr("0 tokens: runs locally", "0 tokens: corre en local")}])
                     + ([{"n": "codebase-memory-mcp", "k": tr("MCP (already installed)", "MCP (ya instalado)"),
                          "why": tr("get_code_snippet returns only the function you want, not the file",
                                    "get_code_snippet devuelve solo la función que buscas, no el fichero"),
                          "cost": tr("fewer tokens per query", "menos tokens por consulta")}]
                        if cfg["cbm"] else [])
                     + [{"n": "Explore", "k": tr("subagent", "subagente"),
                         "why": tr("«where is X used» sweeps stay in its context and only the conclusion reaches you",
                                   "los barridos «dónde se usa X» se quedan en su contexto y a ti solo llega la conclusión"),
                         "cost": tr(f"runs on {cfg['subagent_model'] or 'the default model'}",
                                    f"va con {cfg['subagent_model'] or 'el modelo por defecto'}")}],
            "prompt": "" if cfg["read_guard"] else tr(
                "With the update-config skill, create a PreToolUse hook with matcher Read in ~/.claude/settings.json (script in "
                "~/.claude/hooks/, only standard bash or python3). If the call has no limit and the file has more than 400 "
                "lines, exit with code 2 and the message \"File of N lines: locate with Grep and read with offset/limit\". "
                "It must not affect images, PDFs or files under 400 lines, and on any error it must exit 0. "
                "Test it with two sample JSONs (short and long file) and show me the diff before enabling it.",
                "Con la skill update-config, crea un hook PreToolUse con matcher Read en ~/.claude/settings.json (script en "
                "~/.claude/hooks/, solo bash o python3 estándar). Si la llamada no trae limit y el fichero tiene más de 400 "
                "líneas, que salga con exit 2 y el mensaje «Fichero de N líneas: localiza con Grep y lee con offset/limit». "
                "Que no afecte a imágenes, PDF ni ficheros de menos de 400 líneas, y que ante cualquier error salga con exit 0. "
                "Pruébalo con dos JSON de ejemplo (fichero corto y largo) y enséñame el diff antes de activarlo."),
        })

    # 5) See the context while working: without this the limit is only noticed after it has cost money
    if not seen and (k["big_sessions"] or k["over"]):
        recs.append({
            "id": "statusline", "title": tr("You cannot see how full the context is while you work", "No ves cuánto contexto llevas mientras trabajas"),
            "status": "todo", "impact": None,
            "impact_note": tr("does not save by itself: it makes it visible when to /clear", "no ahorra por sí sola: hace visible cuándo hacer /clear"),
            "confidence": tr("medium", "media"),
            "evidence": tr(f"You have no statusLine (the bar at the bottom of Claude Code) and {k['big_sessions']} session(s) went over {fmt_tok(limit)} tokens in the period. "
                           "Today you only find out with /context or on this dashboard, after the cost is paid.",
                           f"No tienes statusLine (la barra inferior de Claude Code) y {k['big_sessions']} sesión(es) pasaron de {fmt_tok(limit)} tokens en el periodo. "
                           "Hoy solo lo notas con /context o en este panel, cuando el coste ya está pagado."),
            "action": tr("A statusLine with the context % of the limit and the session cost.",
                         "Una statusLine con el % de contexto sobre el límite y el coste de la sesión."),
            "verify": tr("New sessions stop showing up in «Over 300K».", "Las sesiones nuevas dejan de aparecer en «Sobre 300K»."),
            "tradeoff": tr("None: it is a local command, it does not spend model tokens.",
                           "Ninguna: es un comando local, no gasta tokens del modelo."),
            "metric": k["big_sessions"], "metric_label": tr("sessions above the limit", "sesiones por encima del límite"),
            "tools": [{"n": "statusline-setup", "k": tr("Claude Code agent", "agente de Claude Code"),
                       "why": tr("configures it for you", "la configura por ti"), "cost": tr("one call, once", "una llamada, una vez")}],
            "prompt": tr(f"Use the statusline-setup agent to configure a statusLine that shows the model, the context tokens of "
                         f"the current session and its % of {limit} (amber from 70 %, red from 90 %). "
                         "Local script in ~/.claude/, no new dependencies; show me the settings.json diff before saving.",
                         f"Usa el agente statusline-setup para configurar una statusLine que muestre el modelo, los tokens de "
                         f"contexto de la sesión actual y su % sobre {limit} (en ámbar desde el 70 %, en rojo desde el 90 %). "
                         "Script local en ~/.claude/, sin dependencias nuevas; enséñame el diff de settings.json antes de guardar."),
        })

    # 6) Expensive output: only if it really weighs
    if cost and k["c_out"] / cost > 0.25:
        recs.append({
            "id": "output", "title": tr("Long answers: what the model writes is a big part of the cost", "Respuestas largas: lo que escribe el modelo pesa mucho en el coste"),
            "status": "todo", "impact": k["c_out"] * 0.3,
            "impact_note": tr("assuming 30 % less output", "suponiendo un 30 % menos de salida"), "confidence": tr("low", "baja"),
            "evidence": tr(f"Output (what the model writes, including its reasoning) is {100*k['c_out']/cost:.0f}% of the period's cost. It is the most expensive kind of token.",
                           f"La salida (lo que escribe el modelo, con su razonamiento) es el {100*k['c_out']/cost:.0f}% del coste del periodo. Es el tipo de token más caro."),
            "action": tr("Ask for shorter answers in the global CLAUDE.md or enable a compressed-reply mode.",
                         "Pedir respuestas más cortas en el CLAUDE.md global o activar un modo de respuesta comprimida."),
            "verify": tr("The output % in the daily chart drops below 25 %.", "El % de salida del gráfico diario baja de 25 %."),
            "tradeoff": tr("Less explained answers.", "Respuestas menos explicadas."),
            "metric": round(100 * k["c_out"] / cost, 1), "metric_label": tr("% of cost on output", "% del coste en salida"),
            "tools": [{"n": "caveman", "k": tr("skill (already installed)", "skill (ya instalada)"),
                       "why": tr("cuts output a lot", "recorta mucho la salida"), "cost": tr("readability is lost", "se pierde legibilidad")}]
                     if cfg["caveman"] else [],
            "prompt": tr("Review ~/.claude/CLAUDE.md and propose a short rule so answers are briefer without losing what is needed.",
                         "Revisa ~/.claude/CLAUDE.md y propón una regla corta para que las respuestas sean más breves sin perder lo necesario."),
        })

    return sorted(recs, key=lambda x: ({"todo": 0, "warn": 1, "info": 2, "ok": 3}[x["status"]], -(x["impact"] or 0)))


def fmt_tok(n):
    n = n or 0
    return (f"{fnum(n/1e9, 1)}B" if n >= 1e9 else f"{fnum(n/1e6, 1)}M" if n >= 1e6
            else f"{n/1e3:.0f}K" if n >= 1e3 else str(int(n)))


def insights(r):
    """Warnings ordered by weight in the cost. pct=None: not money (goes last)."""
    k, out = r["kpi_range"], []
    cost = k["cost"] or 0
    P = lambda x: 100 * x / cost if cost else 0
    if k["over"] > 0.5 or k["big_sessions"]:
        b = k["big_top"]
        top = (tr(f"; the largest reached {fmt_tok(b['peak'])} ({b['title'] or b['session'][:8]})",
                  f"; la mayor llegó a {fmt_tok(b['peak'])} ({b['title'] or b['session'][:8]})") if b else "")
        out.append({"pct": P(k["over"]), "lvl": "warn" if P(k["over"]) >= 10 else "info",
                    "text": tr(f"Context above {fmt_tok(CTX_LIMIT)}: ~{money(k['over'], 0)}. {k['big_sessions']} session(s) "
                               f"went over that size{top}. It is the cost of resending that excess on every turn; "
                               f"compacting has its own cost (summary + rewriting the cache), so it is not a net saving.",
                               f"Contexto por encima de {fmt_tok(CTX_LIMIT)}: ~{money(k['over'], 0)}. {k['big_sessions']} sesión(es) "
                               f"pasaron de ese tamaño{top}. Es el coste de reenviar ese exceso en cada turno; "
                               f"compactar tiene su propio coste (resumen + reescribir la caché), así que no es ahorro neto.")})
    if k["c_cw"]:
        tw = (k["cw1h"] + k["cw5"]) or 1
        out.append({"pct": P(k["c_cw"]), "lvl": "warn" if P(k["c_cw"]) >= 20 else "info",
                    "text": tr(f"Cache write: {money(k['c_cw'], 0)} with only {fnum(100*tw/(k['ctx'] or 1), 1)}% of the tokens "
                               f"(1 h: {100*k['cw1h']/tw:.0f}%, costs 2× input). It is paid on compaction ({k['compacts']} times), "
                               f"on opening a session ({k['sessions']}), on switching model and after more than 1 h idle.",
                               f"Escritura de caché: {money(k['c_cw'], 0)} con solo el {fnum(100*tw/(k['ctx'] or 1), 1)}% de los tokens "
                               f"(1 h: {100*k['cw1h']/tw:.0f}%, cuesta 2× la entrada). Se paga al compactar ({k['compacts']} veces), "
                               f"al abrir sesión ({k['sessions']}), al cambiar de modelo y tras más de 1 h parado.")})
    if k["c_cr"]:
        out.append({"pct": P(k["c_cr"]), "lvl": "info",
                    "text": tr(f"Cache read: {money(k['c_cr'], 0)} ({k['cache_pct']:.0f}% of the context comes from cache). "
                               f"It is the price of resending the conversation: it drops with shorter conversations, not with more cache.",
                               f"Lectura de caché: {money(k['c_cr'], 0)} (el {k['cache_pct']:.0f}% del contexto sale de caché). "
                               f"Es el precio de reenviar la conversación: baja con conversaciones más cortas, no con más caché.")})
    if k["sub_cost"]:
        oa = r.get("opus_agents") or {}
        cut = r["compare"]["cut"]
        extra = (tr(f" Note: {oa['n']} subagent(s) on Opus since {cut} ({money(oa['cost'], 1)}), "
                    f"despite CLAUDE_CODE_SUBAGENT_MODEL=sonnet.",
                    f" Ojo: {oa['n']} subagente(s) con Opus desde el {cut} ({money(oa['cost'], 1)}), "
                    f"pese a CLAUDE_CODE_SUBAGENT_MODEL=sonnet.") if oa.get("n")
                 else tr(f" None on Opus since {cut}.", f" Ninguno con Opus desde el {cut}."))
        out.append({"pct": P(k["sub_cost"]), "lvl": "warn" if oa.get("n") else "info",
                    "text": tr(f"Subagents: {money(k['sub_cost'], 0)}.{extra}", f"Subagentes: {money(k['sub_cost'], 0)}.{extra}")})
    if k["c_out"]:
        out.append({"pct": P(k["c_out"]), "lvl": "info",
                    "text": tr(f"Model output: {money(k['c_out'], 0)}, of which thinking is "
                               f"{100*k['think']/(k['out'] or 1):.0f}% (≈{P(k['c_think']):.0f}% of the total cost).",
                               f"Salida del modelo: {money(k['c_out'], 0)}, de la que el thinking es el "
                               f"{100*k['think']/(k['out'] or 1):.0f}% (≈{P(k['c_think']):.0f}% del coste total).")})
    if k["prompts"]:
        out.append({"pct": None, "lvl": "info",
                    "text": tr(f"Each message of yours: {money(k['cost_per_prompt'], 2)} eq., {fnum(k['main_turns']/k['prompts'], 1)} turns "
                               f"of the main conversation + {fnum(k['sub_turns']/k['prompts'], 1)} from subagents.",
                               f"Cada mensaje tuyo: {money(k['cost_per_prompt'], 2)} eq., {fnum(k['main_turns']/k['prompts'], 1)} turnos "
                               f"de la conversación principal + {fnum(k['sub_turns']/k['prompts'], 1)} de subagentes.")})
    c = r.get("compare")
    if c and c["before"]["prompts"] and c["after"]["prompts"]:
        b, af = c["before"], c["after"]
        d = 100 * (af["cost_per_prompt"] - b["cost_per_prompt"]) / b["cost_per_prompt"] if b["cost_per_prompt"] else 0
        short = af["days"] < 5
        out.append({"pct": None, "lvl": "info",
                    "text": tr(f"Since {c['cut']}: {money(af['cost_per_prompt'], 2)} per message versus {money(b['cost_per_prompt'], 2)} "
                               f"in the previous {b['days']} days ({d:+.0f}%)."
                               + (f" Short sample ({af['days']} day(s) per side): inconclusive, depends on the type of task." if short else ""),
                               f"Desde el {c['cut']}: {money(af['cost_per_prompt'], 2)} por mensaje frente a {money(b['cost_per_prompt'], 2)} "
                               f"los {b['days']} días anteriores ({d:+.0f}%)."
                               + (f" Muestra corta ({af['days']} día(s) por lado): no concluyente, depende del tipo de tarea." if short else ""))})
    tools = r["tools"]
    if tools and k["tool_chars"]:
        t = tools[0]
        out.append({"pct": None, "lvl": "info",
                    "text": tr(f"{t['name']} is the tool that brings in the most text: {100*(t['chars'] or 0)/k['tool_chars']:.0f}% "
                               f"(~{fmt_tok((t['chars'] or 0)/4)} tokens). Not a direct cost (it is re-read from cache): it fills the context sooner.",
                               f"{t['name']} es la herramienta que más texto mete: {100*(t['chars'] or 0)/k['tool_chars']:.0f}% "
                               f"(~{fmt_tok((t['chars'] or 0)/4)} tokens). No es coste directo (se relee de caché): llena antes el contexto.")})
    return sorted(out, key=lambda x: -1 if x["pct"] is None else x["pct"], reverse=True)


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Tokenometro</title>
<meta name="color-scheme" content="light dark">
<script>try{const t=localStorage.getItem('tk-theme');if(t==='light'||t==='dark')document.documentElement.dataset.theme=t}catch(e){}</script>
<style>
:root{color-scheme:light;
 --bg:#EDF0F6;--card:#FFFFFF;--sunk:#E3E8F1;--fg:#18213A;--mut:#545E78;--line:#CDD4E2;
 --a:#8A5C00;--b:#0A7A67;--c:#3B55CF;--d:#AD2F72;--e:#6D49D6;--f:#C0372A;--g:#136F9E;--h:#A9530F;
 --reel:#1B2338;--reel-fg:#F4F1E6;--dec:#B4352C;
 --fd:"Bahnschrift","DIN Alternate","Barlow Semi Condensed","Roboto Condensed",system-ui,sans-serif;
 --fb:"Segoe UI Variable Text","Segoe UI",system-ui,-apple-system,sans-serif;
 --ease:cubic-bezier(.2,.8,.2,1)}
/* Light by default; dark follows the system unless the user forced light, or when forced dark. */
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){color-scheme:dark;
 --bg:#151C2E;--card:#1C2539;--sunk:#121828;--fg:#E7EBF5;--mut:#8D97B0;--line:#2B3550;
 --a:#F2B33D;--b:#3CC6B0;--c:#7B93FF;--d:#E86BA8;--e:#B79CFF;--f:#FF7A6B;--g:#5BC8F5;--h:#F59E5B;
 --reel:#0D1220;--reel-fg:#F4F1E6;--dec:#C9423A}}
:root[data-theme=dark]{color-scheme:dark;
 --bg:#151C2E;--card:#1C2539;--sunk:#121828;--fg:#E7EBF5;--mut:#8D97B0;--line:#2B3550;
 --a:#F2B33D;--b:#3CC6B0;--c:#7B93FF;--d:#E86BA8;--e:#B79CFF;--f:#FF7A6B;--g:#5BC8F5;--h:#F59E5B;
 --reel:#0D1220;--reel-fg:#F4F1E6;--dec:#C9423A}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 var(--fb);font-variant-numeric:tabular-nums;-webkit-font-smoothing:antialiased}
:focus-visible{outline:2px solid var(--a);outline-offset:2px;border-radius:4px}
header.top{display:flex;flex-wrap:wrap;gap:12px 20px;align-items:flex-end;justify-content:space-between;padding:22px 28px 6px;max-width:1680px;margin:0 auto}
h1{font:600 26px/1.1 var(--fd);margin:0;letter-spacing:.01em}
#gen{margin:4px 0 0;color:var(--mut);font-size:12px}
header.top>div:first-child{flex:1 1 320px;min-width:0}
.topbar{position:sticky;top:0;z-index:20;background:color-mix(in srgb,var(--bg) 88%,transparent);-webkit-backdrop-filter:blur(10px);backdrop-filter:blur(10px);border-bottom:1px solid transparent;transition:border-color .2s}
.topbar.stuck{border-bottom-color:var(--line)}
.topbar-in{display:flex;flex-wrap:wrap;gap:8px 20px;align-items:center;justify-content:space-between;padding:10px 28px;max-width:1680px;margin:0 auto}
.nav{display:flex;gap:2px;overflow-x:auto;scrollbar-width:none;min-width:0;flex:1 1 420px}.nav::-webkit-scrollbar{display:none}
.nav a{flex:none;color:var(--mut);text-decoration:none;font-weight:600;font-size:13px;padding:6px 10px;border-radius:8px;display:inline-flex;align-items:center;gap:6px;transition:color .15s,background .15s}
.nav a:hover{color:var(--fg);background:var(--sunk)}
.nav a[aria-current]{color:var(--fg);background:var(--card);box-shadow:inset 0 -2px 0 var(--a)}
.nav .n{font-size:11px;line-height:1;padding:3px 6px;border-radius:9px;background:var(--a);color:var(--reel)}
main>section{scroll-margin-top:calc(var(--barh,56px) + 12px)}
@media (prefers-reduced-motion:no-preference){html{scroll-behavior:smooth}}
.ctl{display:flex;gap:8px;flex-wrap:wrap}
select,button,input[type=date]{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:6px 10px;font:inherit;font-size:13px}
button{cursor:pointer;font-weight:600;transition:border-color .15s,background .15s}button:hover{border-color:var(--a)}
button.busy{opacity:.6;pointer-events:none}
main{padding:16px 28px 40px;display:grid;gap:18px;grid-template-columns:repeat(auto-fit,minmax(min(100%,470px),1fr));max-width:1680px;margin:0 auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px 18px;overflow:auto;min-width:0}
.wide{grid-column:1/-1}
@media (min-width:900px){main{grid-template-columns:repeat(2,minmax(0,1fr))}}  /* two fixed columns: the half-width pair never leaves a hole */
h2{font:600 16px/1.25 var(--fd);margin:0 0 12px;letter-spacing:.01em;display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px}
.chip.odd{--k:var(--f)}.chips:empty{display:none}.chips{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0 0}
.chip{--k:var(--c);display:inline-flex;align-items:center;gap:7px;padding:3px 10px;border-radius:999px;font-size:12px;color:var(--fg);text-decoration:none;
 background:color-mix(in srgb,var(--k) 12%,transparent);border:1px solid color-mix(in srgb,var(--k) 35%,transparent);transition:background .15s}
.chip:hover{background:color-mix(in srgb,var(--k) 20%,transparent)}.chip:focus-visible{outline:2px solid var(--k);outline-offset:2px}
.csv{display:flex;flex-wrap:wrap;gap:8px 12px;align-items:center;justify-content:space-between;margin-top:12px;font-size:12px}
.csvc{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.btn{display:inline-block;padding:5px 10px;border:1px solid var(--line);border-radius:8px;color:var(--fg);text-decoration:none;font-weight:600;font-size:12px}
.btn:hover{border-color:var(--c)}.btn:focus-visible,#month:focus-visible{outline:2px solid var(--c);outline-offset:2px}
.mut{color:var(--mut)}.note{font-size:12px;margin:10px 0 0;color:var(--mut)}

/* header: counter + gauges */
.hero{grid-column:1/-1;display:grid;grid-template-columns:minmax(300px,.9fr) minmax(0,1.5fr);gap:18px}
.meter{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:22px 24px;display:flex;flex-direction:column;justify-content:center;gap:10px}
.meter .lbl{margin:0;color:var(--mut);font-size:13px}
.odo{display:flex;align-items:flex-end;gap:5px;font:600 84px/1 var(--fd);font-stretch:87.5%}
.odo .dg{position:relative;display:inline-block;width:.62em;height:1.15em;overflow:hidden;background:var(--reel);color:var(--reel-fg);border-radius:6px;
 box-shadow:inset 0 10px 12px -8px rgba(0,0,0,.65),inset 0 -10px 12px -8px rgba(0,0,0,.65)}
.odo .dg.dec{background:var(--dec)}
.odo .reel{display:flex;flex-direction:column;align-items:center;transition:transform 1.4s var(--ease)}
.odo .reel span{height:1.15em;line-height:1.15em}
.odo .sep{width:.22em;text-align:center;color:var(--mut)}.odo .cur{margin-left:.15em;font-size:.55em;color:var(--mut);align-self:center}
.spark{display:grid;grid-template-columns:repeat(24,1fr);gap:3px;align-items:end;height:64px;margin-top:8px}
.spark i{display:block;background:color-mix(in srgb,var(--a) 75%,transparent);border-radius:3px 3px 1px 1px;min-height:2px;transform-origin:50% 100%}
.spark i.now{background:var(--b)}.spark i.z{background:var(--sunk)}.spark.grow i{animation:grow .7s var(--ease) both}
.spark-x{display:flex;justify-content:space-between;font-size:10px;color:var(--mut)}
.meter .sub{margin:0;color:var(--mut);font-size:13px}.meter .sub b{color:var(--fg);font-weight:600}
.now{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:18px 22px;min-width:0}
.now h2{margin-bottom:2px}.now #livehint{margin:0 0 12px;font-size:12px}
.pulse{width:9px;height:9px;border-radius:50%;background:var(--b);display:none}
.pulse.on{display:inline-block;animation:pulse 2s ease-out infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 color-mix(in srgb,var(--b) 60%,transparent)}100%{box-shadow:0 0 0 10px transparent}}
.gauge{padding:10px 0;border-top:1px solid var(--line)}.gauge:first-child{border-top:0;padding-top:2px}
.gh{display:flex;align-items:baseline;gap:10px;min-width:0}.gh b{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.gh .p{color:var(--mut);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0;flex:1}
.gh .v{font:600 20px/1 var(--fd);white-space:nowrap}
.track{position:relative;height:10px;margin:8px 0 6px;background:var(--sunk);border-radius:6px;overflow:visible}
.track .fill{position:absolute;inset:0 auto 0 0;border-radius:6px;background:linear-gradient(90deg,var(--b),var(--a));width:var(--w,0%);transition:width 1.2s var(--ease)}
.gauge.over .track .fill{background:linear-gradient(90deg,var(--a),var(--f))}
.track .mark{position:absolute;top:-4px;bottom:-4px;width:2px;background:var(--fg);opacity:.55;border-radius:1px}
.track .mark::after{content:attr(data-l);position:absolute;top:-16px;left:50%;transform:translateX(-50%);font-size:10px;color:var(--mut);white-space:nowrap}
.gm{font-size:12px;color:var(--mut)}.gm .w{color:var(--f);font-weight:600}
.empty{color:var(--mut);margin:6px 0}
.fxbar{display:flex;height:12px;border-radius:6px;overflow:hidden;margin:10px 0 12px;background:var(--line)}
.fxbar i{display:block;height:100%}
.fxl{display:grid;grid-template-columns:auto 1fr auto;gap:4px 10px;font-size:13px;align-items:center}
.fxl i{width:10px;height:10px;border-radius:3px;display:inline-block}.fxl .n{text-align:right;font-variant-numeric:tabular-nums}
.tag{display:inline-block;font-size:11px;padding:0 6px;border-radius:8px;border:1px solid var(--line);color:var(--mut);margin-left:4px}

/* KPI strip */
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:0}
.k{padding:4px 16px 6px;border-left:1px solid var(--line);display:flex;flex-direction:column;gap:2px}
.k b{font:600 24px/1.15 var(--fd)}.k span{color:var(--mut);font-size:12px}.k span:first-child{color:var(--fg);font-size:13px}

/* tables */
table{width:100%;border-collapse:collapse;font-size:13px}
td,th{padding:7px 6px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
tr:last-child>td{border-bottom:0}
th{color:var(--mut);font-weight:600;font-size:12px;border-bottom-color:color-mix(in srgb,var(--fg) 25%,transparent)}
td:first-child,th:first-child{text-align:left;white-space:normal;overflow-wrap:anywhere;min-width:110px}
th.s{cursor:pointer;user-select:none}th.s:hover{color:var(--fg)}
th.s:focus-visible,tr.click:focus-visible{outline:2px solid var(--c);outline-offset:-2px}tr.click{cursor:pointer}tr.click>td{transition:background .15s}tr.click:hover>td{background:color-mix(in srgb,var(--a) 9%,transparent)}
.bar{height:6px;background:var(--a);border-radius:3px}
.sbar{display:flex;width:84px;height:8px;border-radius:4px;overflow:hidden;background:var(--sunk)}.sbar i{display:block;height:100%}
.code{font-family:ui-monospace,"Cascadia Code",Consolas,monospace;font-size:12px;text-align:left;white-space:normal;word-break:break-all;max-width:560px}
.badge{display:inline-flex;align-items:center;gap:4px;padding:2px 8px;border-radius:999px;font:500 11px/1.4 var(--fb);background:var(--sunk);color:var(--mut)}
.warn{color:var(--f);font-weight:600}
.det>td{padding:0;background:color-mix(in srgb,var(--a) 4%,transparent);text-align:left;white-space:normal}
.detin{display:grid;grid-template-rows:0fr;transition:grid-template-rows .45s var(--ease)}
.detin.open{grid-template-rows:1fr}.detin>div{overflow:hidden;min-height:0}
.det .g{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px;padding:14px 10px}
.det h3{font:600 13px/1.3 var(--fd);margin:0 0 6px}

/* recommendations */
.recs{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,360px),1fr));gap:12px}
.rec{background:var(--bg);border-radius:12px;padding:12px 14px 10px;font-size:13px;min-width:0;overflow-wrap:anywhere;position:relative;border:1px solid var(--line)}
.rec::before{content:"";position:absolute;left:0;top:12px;bottom:12px;width:3px;border-radius:0 3px 3px 0;background:var(--mut)}
.rec.st-todo::before{background:var(--f)}.rec.st-warn::before{background:var(--a)}.rec.st-ok::before{background:var(--b)}.rec.st-info::before{background:var(--c)}
.rec h3{margin:0 0 6px;font:600 15px/1.3 var(--fd);display:flex;justify-content:space-between;align-items:flex-start;gap:10px}
.rec h3>span:first-child{min-width:0}.rec h3 .badge{margin-left:6px;vertical-align:2px}
.rec .imp{font:600 18px/1.2 var(--fd);white-space:nowrap;color:var(--f)}.rec .imp.mut{color:var(--mut);font-size:14px}
.rec.st-todo .badge{background:color-mix(in srgb,var(--f) 13%,transparent);color:color-mix(in srgb,var(--f) 70%,var(--fg))}
.rec.st-warn .badge{background:color-mix(in srgb,var(--a) 13%,transparent);color:color-mix(in srgb,var(--a) 70%,var(--fg))}
.rec.st-ok .badge{background:color-mix(in srgb,var(--b) 13%,transparent);color:color-mix(in srgb,var(--b) 70%,var(--fg))}
.rec.st-info .badge{background:color-mix(in srgb,var(--c) 13%,transparent);color:color-mix(in srgb,var(--c) 70%,var(--fg))}
.rec dl{margin:6px 0 0;display:grid;grid-template-columns:84px minmax(0,1fr);gap:4px 10px}.rec dt{color:var(--mut);font-size:12px}.rec dd{margin:0}
.rec .hist{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:0 0 8px;font-size:12px;color:var(--mut)}
.rec .hist .sq{display:inline-flex;gap:3px;flex:none}.rec .hist>span:last-child{flex:1;min-width:0}.rec .hist i{width:9px;height:9px;border-radius:2px;background:var(--line)}
.rec .hist i.h-todo{background:var(--f)}.rec .hist i.h-warn{background:var(--a)}.rec .hist i.h-ok{background:var(--b)}.rec .hist i.h-info{background:var(--c)}
.rec dd.tools{display:flex;flex-direction:column;gap:3px}.rec dd.tools b{font-weight:600}.rec dd.tools em{font-style:normal;color:var(--mut);font-size:12px}
.rec div.pr{margin-top:10px;border:1px solid var(--line);border-radius:10px;padding:6px 10px;display:flex;justify-content:space-between;align-items:center;gap:10px;font-size:12px;color:var(--mut)}
.rec div.pr code{font-size:11.5px;color:var(--fg,inherit);overflow-wrap:anywhere}.rec div.pr .cp,.rec div.pr .ap{flex:none}
.rec div.ap-row{border-color:var(--a);color:var(--fg,inherit)}
.pager{display:flex;justify-content:flex-end;align-items:center;gap:10px;margin-top:8px;font-size:12px;color:var(--mut)}
.pager button{font:600 14px/1 var(--fb);width:28px;height:26px;border:1px solid var(--line);background:var(--card);color:inherit;border-radius:8px;cursor:pointer}
.pager button:disabled{opacity:.35;cursor:default}.pager button:not(:disabled):hover{border-color:var(--a)}
.fit{font-weight:600}.fit-sonnet{color:var(--b)}.fit-haiku{color:var(--g)}.fit-opusplan{color:var(--a)}.fit-opus{color:var(--mut)}
.plan{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:6px 28px}.plan .pl{padding:6px 0}
.plan .gh .p{color:var(--mut);font-size:12px}.plan .gh .v{margin-left:auto;padding-left:14px;flex:none;font:600 20px/1 var(--fd)}
.plan .pl.amb .fill{background:linear-gradient(90deg,var(--a),var(--h))}.plan .pl.red .fill{background:linear-gradient(90deg,var(--h),var(--f))}
.plan .gm .w{color:var(--f)}
.rec details.pr{margin-top:10px;border:1px solid var(--line);border-radius:10px;padding:6px 10px}
.rec details.pr summary{margin:0;display:flex;justify-content:space-between;align-items:center;gap:10px;color:var(--fg,inherit)}
.rec details.pr pre{white-space:pre-wrap;font:12px/1.55 var(--fb);margin:8px 0 2px;color:var(--mut)}
.rec .cp,.rec .ap{font:600 12px var(--fb);border:1px solid var(--line);background:var(--card);color:inherit;border-radius:8px;padding:3px 10px;cursor:pointer}
.rec .cp:hover,.rec .ap:hover{border-color:var(--a)}.rec .ap:not([data-u]){background:var(--a);border-color:var(--a);color:var(--card)}.rec .ap:disabled{opacity:.5;cursor:wait}.rec .cp.ok{border-color:var(--b);color:var(--b)}
.rec.chg{animation:chg 2.4s var(--ease)}@keyframes chg{0%,30%{box-shadow:0 0 0 2px var(--a)}100%{box-shadow:0 0 0 0 transparent}}
.rec .dm{position:absolute;right:14px;bottom:9px;font:12px var(--fb);background:none;border:0;color:var(--mut);cursor:pointer;padding:2px 4px;border-radius:6px}
.rec .dm:hover{color:var(--f)}.rec .dm:disabled,#recs_more .rs:disabled{opacity:.5;cursor:wait}
#recs_more details.more{margin-top:12px;border:1px solid var(--line);border-radius:12px;padding:8px 14px;font-size:13px}
#recs_more summary{cursor:pointer;font-weight:600}#recs_more p{margin:6px 0;font-size:12px}
#recs_more ul{margin:4px 0 2px;padding-left:18px;display:flex;flex-direction:column;gap:6px}
#recs_more .rs{font:600 12px var(--fb);border:1px solid var(--line);background:var(--card);color:inherit;border-radius:8px;padding:2px 9px;cursor:pointer;margin-left:6px}
#recs_more .rs:hover{border-color:var(--a)}
.rec details summary{cursor:pointer;color:var(--mut);font-size:12px;margin-top:8px}.rec details[open] dl{animation:fade .3s var(--ease)}
@keyframes fade{from{opacity:0;transform:translateY(-3px)}}

/* charts and legends */
.leg{display:flex;flex-wrap:wrap;gap:6px 14px;margin:0 0 8px}
.leg span{display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--mut)}.leg i{width:10px;height:10px;border-radius:3px;display:inline-block}
svg text{fill:var(--mut);font-size:10px;font-family:var(--fb)}
svg.grow rect{transform-box:fill-box;transform-origin:50% 100%;animation:grow .8s var(--ease) both}
@keyframes grow{from{transform:scaleY(0)}}
.cmp td:nth-child(4){font-weight:600}
ul{margin:0;padding-left:18px}li{margin:4px 0}

@media (max-width:820px){.hero{grid-template-columns:1fr}.odo{font-size:48px}header.top,main,.topbar-in{padding-left:16px;padding-right:16px}.topbar .ctl{flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none;flex:1 1 100%}.topbar .ctl>*{flex:none}}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important}}
</style></head><body>
<header class="top"><div><h1 data-i18n="title">Tokenometro</h1><p id="gen"></p><div id="alerts" class="chips" role="status" aria-live="polite"></div></div></header>
<div class="topbar" id="topbar"><div class="topbar-in"><nav class="nav" id="nav" data-i18n-aria="nav_label" aria-label="Sections">
<a href="#s-now" data-i18n="nav_now">Now</a><a href="#plancard" id="nav_plan" hidden data-i18n="nav_plan">Plan</a><a href="#s-summary" data-i18n="nav_summary">Summary</a><a href="#s-recs"><span data-i18n="nav_recs">Improvements</span><span class="n" id="nav_recs_n" hidden></span></a><a href="#s-daily" data-i18n="nav_daily">By day</a><a href="#s-ba" data-i18n="nav_ba">Before/after</a><a href="#s-breakdown" data-i18n="nav_breakdown">Breakdown</a><a href="#s-sessions" data-i18n="nav_sessions">Sessions</a></nav>
<div class="ctl"><select id="days" data-i18n-aria="period" aria-label="Period"><option value="1" data-i18n="today">Today</option><option value="7" selected data-i18n="d7">7 days</option><option value="30" data-i18n="d30">30 days</option><option value="90" data-i18n="d90">90 days</option></select>
<select id="client" data-i18n-aria="client" aria-label="Client"><option value="all" data-i18n="allclients">All clients</option></select>
<select id="cur" data-i18n-aria="currency" aria-label="Currency"><option value="EUR">€</option><option value="USD">$</option></select>
<select id="lang" data-i18n-aria="language" aria-label="Language"><option value="en">EN</option><option value="es">ES</option></select>
<select id="theme" data-i18n-aria="theme" aria-label="Theme"><option value="system" data-i18n="th_system">Auto</option><option value="light" data-i18n="th_light">Light</option><option value="dark" data-i18n="th_dark">Dark</option></select>
<button id="re" data-i18n="refresh">Refresh</button></div></div></div>
<main>
<section class="hero" id="s-now">
 <div class="meter"><p class="lbl" data-i18n="spent_today">Spent today, at API rates</p><div class="odo" id="odo" role="img"></div><p class="sub" id="todaysub"></p><p class="sub" id="pace"></p>
  <div class="spark" id="spark" data-i18n-aria="spark_aria" aria-label="Spend today by hour"></div><div class="spark-x"><span>0 h</span><span>6 h</span><span>12 h</span><span>18 h</span><span>23 h</span></div></div>
 <div class="now"><h2><span data-i18n="right_now">Right now</span> <span class="pulse" id="pulse" data-i18n-title="active_title" title="There are active sessions"></span></h2><p class="mut" id="livehint"></p><div id="live"></div></div>
</section>
<section class="card wide" id="plancard" hidden><h2><span data-i18n="your_plan">Your plan</span> <span class="badge" id="planage"></span></h2><div class="plan" id="plan"></div>
<p class="mut" style="font-size:12px;margin:8px 0 0" data-i18n="plan_note"></p></section>
<section class="card wide" id="s-summary"><h2 data-i18n="summary">Period summary</h2><div class="kpis" id="kpis"></div>
<p class="note"><span id="eqn"></span> <span data-i18n="eq_rest"></span></p></section>
<section class="card wide" id="s-recs"><h2><span data-i18n="recs_title">Recommended improvements</span> <span class="badge" data-i18n="recs_badge"></span></h2><div class="recs" id="recs"></div><div id="recs_more"></div>
<p class="mut" style="font-size:12px;margin:8px 0 0" data-i18n="recs_legend"></p></section>
<section class="card wide" id="s-daily"><h2 data-i18n="daily_title"></h2><div class="leg" id="leg"></div><div id="daily"></div>
<p class="mut" style="font-size:12px;margin:6px 0 0" data-i18n="daily_note"></p></section>
<section class="card wide" id="s-ba"><h2><span data-i18n="before_after"></span> <input type="date" id="cut" data-i18n-aria="cut_label"></h2><div id="cmp"></div>
<p class="mut" style="font-size:12px;margin:6px 0 0" data-i18n="ba_note"></p></section>
<section class="card" id="s-breakdown"><h2 data-i18n="by_model"></h2><div id="models"></div></section>
<section class="card"><h2 data-i18n="by_client"></h2><div id="clients"></div>
<div class="csv"><span class="mut" data-i18n="csv_hint"></span><span class="csvc"><input type="month" id="month" data-i18n-aria="month_label"><a class="btn" id="csv_s" data-i18n="csv_sessions"></a><a class="btn" id="csv_c" data-i18n="csv_clients"></a></span></div></section>
<section class="card"><h2><span data-i18n="by_branch"></span> <span class="badge" data-i18n="branch_badge"></span></h2><div id="branches"></div>
<p class="mut" style="font-size:12px;margin:6px 0 0" data-i18n="branch_note"></p></section>
<section class="card"><h2 data-i18n="fixed_title"></h2><div id="fixed"></div>
<p class="mut" style="font-size:12px;margin:6px 0 0" data-i18n="fixed_note"></p></section>
<section class="card wide" id="s-sessions"><h2><span data-i18n="costliest_sessions"></span> <span class="badge" data-i18n="sess_badge"></span></h2><div class="leg" id="sleg"></div><div id="sessions"></div></section>
<section class="card wide"><h2 data-i18n="costliest_agents"></h2><div id="agents"></div></section>
</main>
<script>
const I18N={
en:{title:'Tokenometro',pace:'This month <b>{m}</b> · at the pace of the last 7 days it ends near <b>{p}</b>',pace_last:' (last month {l})',odd:'Unusual day: {d}',odd_tip:'{d} cost {c}, {x} times your usual day ({m}). Most of it: «{s}» ({sc}). See Sessions.',nav_label:'Sections',nav_now:'Now',nav_plan:'Plan',nav_summary:'Summary',nav_recs:'Improvements',nav_daily:'By day',nav_ba:'Before/after',nav_breakdown:'Breakdown',nav_sessions:'Sessions',period:'Period',today:'Today',d7:'7 days',d30:'30 days',d90:'90 days',client:'Client',allclients:'All clients',others:'Others',currency:'Currency',language:'Language',theme:'Theme',th_system:'◐ Auto (system)',th_light:'☀ Light',th_dark:'☾ Dark',
 refresh:'Refresh',refreshing:'Refreshing…',spent_today:'Spent today, at API prices (equivalent cost)',spark_aria:'Spend today by hour',right_now:'Right now',active_title:'There are active sessions',
 your_plan:'Your plan',plan_note:'The % is calculated by Claude Code and includes what you use in claude.ai. The ceiling is an estimate: what you have spent here divided by the % used.',
 summary:'Period summary',eq_rest:'With a subscription (Pro, Max, Team…) you do not pay this amount, but it lets you compare periods and habits; with the API it is roughly your real bill.',
 eq_usd:'"$ eq." means equivalent cost: what these tokens would cost at the public API prices (prices.json).',eq_eur:'"€ eq." means equivalent cost: what these tokens would cost at the public API prices (prices.json, in dollars), converted to euros at {rate} €/$',
 recs_title:'Recommended improvements',recs_badge:'based on your data and your settings.json',recs_legend:'Orange: pending · yellow: applied but no clear effect · green: applied and noticeable · blue: informational. The impact is for the selected period.',
 daily_title:'Equivalent cost per day and token type',before_after:'Before / after',ba_note:'Same number of days on each side of the date. It follows the client filter, not the period one. Use it to see whether a change you made (e.g. a new setting) moved the numbers.',
 by_branch:'By branch / task',branch_badge:'from git',branch:'Branch',task:'Task',last_day:'Last',branch_note:'Sessions grouped by the git branch they ran on. If the branch name has a ticket (feature/7067, fix/ABC-123) it shows as the task. Sessions outside a git repo are not listed.',no_branches:'No sessions inside a git repository in this period.',fixed_title:'Fixed cost per session',fx_head:'Every reply carries ~{n} tokens before your conversation even starts. In this period that cost {c} ({p}% of the total) across {s} sessions.',fx_system:'System prompt',fx_builtin:'Built-in tools',fx_claude_md:'Your global CLAUDE.md',fx_rest:'Project CLAUDE.md, memory, skills and first message',fx_mcp:'MCP server {n}',fx_deferred:'{n} more tools load on demand (tool search) and barely count.',fx_none:'Not measured yet: it appears after a new session in this period.',fixed_note:'Measured from the smallest context each session had and the request shape Claude Code logs. It is cached, so each turn pays the cache-read price for it, plus one cache write per session.',by_model:'By model',by_client:'By client',costliest_sessions:'Costliest sessions',sess_badge:'click a row for details · click a header to sort',costliest_agents:'Costliest subagents',
 no_data:'No data',sort:'Sort',prev:'Previous',next:'Next',of:'of',last5:'in the last 5 h',of_plan:'of the plan',w5:'5 h',wk:'week',
 live_empty:'No session has activity right now. As soon as Claude answers, it shows up here.',ctx_aria:'Context',accumulated:'spent in this session',messages:'messages',compactions:'compactions',compaction:'compaction',subagents_lc:'subagents',
 over_compact:'over {n} tokens: good time to /compact (summarise) or /clear',plan_nolecture:'no plan reading: only what was spent',last5h:'Last 5 hours',last7d:'Last 7 days',
 plan_nopct:'{n} tokens. The plan % only exists with a subscription (with the API there is no 5 h or weekly limit) and Claude Code only gives it to the statusLine and plugins: enable statusline.py (see README) or the sub-meter plugin and the % and the estimated cap will show up here.',
 plan_src:'Your plan usage (from {src}, read at {at})',plan_ago:', {t} ago',five_h:'5 hours',week:'Week',
 eta:'At the current pace you reach 100 % on {eta}, before it resets.',noeta:'At the current pace you will not run out before it resets ({reset}).',resets:'resets in {t} ({at})',
 copied:'Copied',al_update:'v{n} available',al_update_tip:'You have {c} and {n} is out. To update: {h}',apply:'Apply',undo:'Undo',apply_hint:'Tokenometro can do it for you: sets {c} in ~/.claude/settings.json (a backup is saved first).',applied_on:'Applied from here on {d} ({c}).',applied_ba:'Before: {a} · after: {b} ({n} day(s)).',applied_wait:'The before/after appears from tomorrow.',undo_confirm:'Put back the previous value in ~/.claude/settings.json?',apply_failed:'Could not change settings.json',copy_prompt:'Copy prompt',prompt_for:'Prompt for Claude in',model:'Model',cost:'Cost',turns:'Turns',subag:'(subagent)',segments:'Segments (between compactions)',from:'From',max_ctx:'Max. context',
 al_unp:'{n} model(s) without a price',
 al_unpriced:'Estimated prices for <code>{m}</code>: not in prices.json, so they are valued at the <code>_default</code> price. Add them to prices.json for exact figures.',
 csv_hint:'Monthly report to share or bill: tokens and cost per session or per client.',csv_sessions:'CSV by session',csv_clients:'CSV by client',month_label:'Month of the CSV report',
 load_err:'Could not refresh: is Tokenometro still running? Showing the last data received.',cut_label:'Cut-off date for before / after',
 tools:'Tools',tool:'Tool',tok_h:'~Tokens',calls:'Calls',errors:'Errors',subagents:'Subagents',subagent:'Subagent',biggest_calls:'Biggest calls',what:'What',tokens:'Tokens',sessions:'Sessions',
 updated:'Updated at {t} · refreshes every minute',live_hint:'Sessions with activity in the last {n} min. The bar is the size of the conversation (context) that is resent on every reply; the mark is the autocompact limit, where Claude summarises it.',
 today_sub:'<b>{tok}</b> tokens and <b>{n}</b> messages today · in the period, <b>{c}</b>',period_k:'Period',per_msg:'Per message of yours',turns_plus:'Each message triggers {a} model replies ("turns") plus {b} from subagents',of_cost:'of the cost goes to subagents, helper agents Claude launches for side tasks',
 cache_write:'Cache write',pct_cost_comp:'Saving new context to the cache costs more than reading it. {p}% of cost; rises after each compaction ({n} in the period).',over_k:'Over {n}',over_sub:'What it cost to resend context above {n} tokens on every reply ({p}% of cost). Shorter sessions or an earlier /compact save it.',
 cache_read:'Cache read',input_nc:'Uncached input',output_think:'Output (incl. thinking)',output:'Output',thinking:'Thinking',
 session:'Session',best_model:'Cost-effective model',went_on:'Went on',top_tool:'Top tool',messages_h:'Messages',compact_h:'Compact.',
 st_todo:'Pending',st_warn:'Applied · review',st_ok:'Applied · working',st_info:'Informational',pending_since:'pending since {d}',fine_since:'fine since {d}',current_since:'current since {d}',
 same_as:'same as {d}',trend:'{a} → {b} since {d}',short_sample:'short sample: {n} day(s) since the cut, 7 needed',hist_start:'history starts today',observing:'Under observation',before_approx:'before ≈',
 evidence:'Evidence',what_to_do:'What to do',with_:'With',check:'Check',tradeoff_h:'Trade-off, calculation and confidence',tradeoff:'Trade-off',calc:'Calculation',confidence:'Confidence',nothing:'Nothing to recommend in this period.',nothing_yet:'Nothing proven yet: {n} under study, below.',study_h:'Under study ({n})',study_hint:'Found in your data but not proven yet. They move up on their own once they repeat on 3 days and save enough for you.',dismissed_h:'Dismissed ({n})',dismissed_hint:'Hidden by you. The ones with an amount come back on their own if it doubles.',dismiss:'Not for me',dismiss_tip:'Hide this card; it comes back if its cost doubles',restore:'Restore',
 cost_day:'Cost per day',cost_msg:'Cost per message',turns_msg:'Turns per message',avg_ctx:'Average context',change:'Change',short_warn:'Short sample: {n} day(s) per side. Inconclusive (it depends a lot on the type of task).',
 win_cap:'You have used <b>{c}</b> eq. ({tok} tokens) of the ≈<b>{cap}</b> this window seems to allow; ≈<b>{left}</b> left{now}.',win_now:' (≈{n} % if counted up to this minute)',win_nocap:'You have used <b>{c}</b> eq. ({tok} tokens) in this window. The allowance is only estimated once you pass 10 % of the plan.',period_sub:'{n} tokens in total',help_period:'Total equivalent cost of the selected period, and the tokens (pieces of text) processed.',help_msg:'Average cost of each message you send. A turn is one reply of the model; a message can trigger several.',help_sub:'Subagents are helper agents Claude launches on its own (searching, reviewing…). Their cost is part of the total.',help_cw:'Cache write: saving the conversation so it is cheap to resend. It is paid on a new session, after a compaction, when switching model and after a long pause.',help_over:'Context over {n}: when a conversation grows past that size, every reply resends the excess. A compaction is Claude summarising the chat to shrink it.',help_best:'Cheapest model that would have been enough given what the session did; the figure is the estimated saving.',help_went:'What most of the cost of this session went to.',help_compact:'Times Claude summarised the conversation because it was too long.',help_msgs:'Messages you sent in the session.',daily_note:'Cache read: resending the conversation from the cache (cheap per token). Cache write: saving new context (pricier). Uncached input: new text sent without caching. Output: what the model writes, including its reasoning (thinking).',odo_eur:'euros today',odo_usd:'dollars today',h_title:'{h} h: {v}'},
es:{title:'Tokenómetro',pace:'Este mes <b>{m}</b> · al ritmo de los últimos 7 días acabará en unos <b>{p}</b>',pace_last:' (el mes pasado, {l})',odd:'Día fuera de lo normal: {d}',odd_tip:'El {d} costó {c}, {x} veces tu día normal ({m}). Sobre todo: «{s}» ({sc}). Míralo en Sesiones.',nav_label:'Secciones',nav_now:'Ahora',nav_plan:'Plan',nav_summary:'Resumen',nav_recs:'Mejoras',nav_daily:'Por día',nav_ba:'Antes/después',nav_breakdown:'Desglose',nav_sessions:'Sesiones',period:'Periodo',today:'Hoy',d7:'7 días',d30:'30 días',d90:'90 días',client:'Cliente',allclients:'Todos los clientes',others:'Otros',currency:'Moneda',language:'Idioma',theme:'Tema',th_system:'◐ Auto (sistema)',th_light:'☀ Claro',th_dark:'☾ Oscuro',
 refresh:'Actualizar',refreshing:'Actualizando…',spent_today:'Gastado hoy, a precios de API (coste equivalente)',spark_aria:'Gasto de hoy por horas',right_now:'Ahora mismo',active_title:'Hay sesiones activas',
 your_plan:'Tu plan',plan_note:'El % lo calcula Claude Code e incluye lo que uses en claude.ai. El techo es una estimación: lo que llevas gastado dividido entre el % usado.',
 summary:'Resumen del periodo',eq_rest:'Con suscripción (Pro, Max, Team…) no pagas esta cantidad, pero sirve para comparar periodos y hábitos; con la API es, aproximadamente, tu factura real.',
 eq_usd:'«$ eq.» es el coste equivalente: lo que costarían estos tokens a los precios públicos de la API (prices.json).',eq_eur:'«€ eq.» es el coste equivalente: lo que costarían estos tokens a los precios públicos de la API (prices.json, en dólares), pasado a euros a {rate} €/$',
 recs_title:'Mejoras recomendadas',recs_badge:'según tus datos y tu settings.json',recs_legend:'Naranja: pendiente · amarillo: aplicada pero sin efecto claro · verde: aplicada y se nota · azul: informativa. El impacto es del periodo elegido.',
 daily_title:'Coste equivalente por día y tipo de token',before_after:'Antes / después del',ba_note:'Mismo número de días a cada lado de la fecha. Respeta el filtro de cliente, no el de periodo. Sirve para ver si un cambio que hiciste (p. ej. un ajuste nuevo) movió las cifras.',
 by_branch:'Por rama / tarea',branch_badge:'de git',branch:'Rama',task:'Tarea',last_day:'Último',branch_note:'Sesiones agrupadas por la rama de git en la que se trabajó. Si el nombre lleva un ticket (feature/7067, fix/ABC-123) sale como tarea. Las sesiones fuera de un repo git no aparecen.',no_branches:'No hay sesiones dentro de un repositorio git en este periodo.',fixed_title:'Coste fijo por sesión',fx_head:'Cada respuesta lleva ~{n} tokens antes incluso de empezar la conversación. En este periodo eso costó {c} ({p}% del total) en {s} sesiones.',fx_system:'Prompt de sistema',fx_builtin:'Herramientas integradas',fx_claude_md:'Tu CLAUDE.md global',fx_rest:'CLAUDE.md del proyecto, memoria, skills y primer mensaje',fx_mcp:'Servidor MCP {n}',fx_deferred:'Otras {n} herramientas se cargan bajo demanda (búsqueda de herramientas) y apenas cuentan.',fx_none:'Aún sin medir: aparece tras una sesión nueva en este periodo.',fixed_note:'Medido con el contexto más pequeño de cada sesión y la forma de la petición que registra Claude Code. Va en caché: cada turno lo paga a precio de lectura de caché, más una escritura de caché por sesión.',by_model:'Por modelo',by_client:'Por cliente',costliest_sessions:'Sesiones más caras',sess_badge:'pincha una fila para el detalle · pincha una cabecera para ordenar',costliest_agents:'Subagentes más caros',
 no_data:'Sin datos',sort:'Ordenar',prev:'Anterior',next:'Siguiente',of:'de',last5:'en las últimas 5 h',of_plan:'del plan',w5:'5 h',wk:'semana',
 live_empty:'No hay ninguna sesión con actividad ahora. En cuanto Claude responda, aparece aquí.',ctx_aria:'Contexto',accumulated:'gastado en esta sesión',messages:'mensajes',compactions:'compactaciones',compaction:'compactación',subagents_lc:'subagentes',
 over_compact:'pasa de {n} tokens: buen momento para /compact (resumir) o /clear',plan_nolecture:'sin lectura del plan: solo lo gastado',last5h:'Últimas 5 horas',last7d:'Últimos 7 días',
 plan_nopct:'{n} tokens. El % del plan solo existe con suscripción (con API no hay límite de 5 h ni semanal) y Claude Code solo se lo da a la statusLine y a los plugins: activa statusline.py (ver README) o el plugin sub-meter y aquí saldrá el % y el techo estimado.',
 plan_src:'Uso de tu plan (dato de {src}, leído a las {at})',plan_ago:', hace {t}',five_h:'5 horas',week:'Semana',
 eta:'Al ritmo actual llegarás al 100 % el {eta}, antes de que se reinicie.',noeta:'Al ritmo actual no lo agotarás antes de que se reinicie ({reset}).',resets:'se reinicia en {t} ({at})',
 copied:'Copiado',al_update:'v{n} disponible',al_update_tip:'Tienes la {c} y ya está la {n}. Para actualizar: {h}',apply:'Aplicar',undo:'Deshacer',apply_hint:'Tokenometro puede hacerlo por ti: pone {c} en ~/.claude/settings.json (antes guarda una copia).',applied_on:'Aplicada desde aquí el {d} ({c}).',applied_ba:'Antes: {a} · después: {b} ({n} día(s)).',applied_wait:'El antes/después sale a partir de mañana.',undo_confirm:'¿Volver a poner el valor anterior en ~/.claude/settings.json?',apply_failed:'No se pudo cambiar settings.json',copy_prompt:'Copiar prompt',prompt_for:'Prompt para Claude en',model:'Modelo',cost:'Coste',turns:'Turnos',subag:'(subag.)',segments:'Tramos (entre compactaciones)',from:'Desde',max_ctx:'Contexto máx.',
 al_unp:'{n} modelo(s) sin precio',
 al_unpriced:'Precios estimados para <code>{m}</code>: no están en prices.json y se valoran al precio de <code>_default</code>. Añádelos a prices.json para tener cifras exactas.',
 csv_hint:'Informe mensual para compartir o repercutir: tokens y coste por sesión o por cliente.',csv_sessions:'CSV por sesión',csv_clients:'CSV por cliente',month_label:'Mes del informe CSV',
 load_err:'No se pudo actualizar: ¿sigue en marcha Tokenometro? Se muestran los últimos datos recibidos.',cut_label:'Fecha de corte para antes / después',
 tools:'Herramientas',tool:'Herramienta',tok_h:'~Tokens',calls:'Llamadas',errors:'Errores',subagents:'Subagentes',subagent:'Subagente',biggest_calls:'Llamadas más gordas',what:'Qué',tokens:'Tokens',sessions:'Sesiones',
 updated:'Actualizado a las {t} · se refresca cada minuto',live_hint:'Sesiones con actividad en los últimos {n} min. La barra es el tamaño de la conversación (contexto), que se reenvía en cada respuesta; la marca es el límite de autocompact, donde Claude la resume.',
 today_sub:'<b>{tok}</b> tokens y <b>{n}</b> mensajes hoy · en el periodo, <b>{c}</b>',period_k:'Periodo',per_msg:'Por mensaje tuyo',turns_plus:'Cada mensaje provoca {a} respuestas del modelo («turnos») más {b} de subagentes',of_cost:'del coste va a subagentes, agentes auxiliares que Claude lanza para tareas aparte',
 cache_write:'Escritura de caché',pct_cost_comp:'Guardar contexto nuevo en la caché cuesta más que leerlo. {p}% del coste; sube tras cada compactación ({n} en el periodo).',over_k:'Sobre {n}',over_sub:'Lo que costó reenviar en cada respuesta el contexto por encima de {n} tokens ({p}% del coste). Con sesiones más cortas o /compact antes, se ahorra.',
 cache_read:'Lectura de caché',input_nc:'Input sin caché',output_think:'Salida (incl. thinking)',output:'Salida',thinking:'Thinking',
 session:'Sesión',best_model:'Modelo rentable',went_on:'Se fue en',top_tool:'Herramienta top',messages_h:'Mensajes',compact_h:'Compact.',
 st_todo:'Pendiente',st_warn:'Aplicada · revisar',st_ok:'Aplicada · funciona',st_info:'Informativa',pending_since:'pendiente desde el {d}',fine_since:'bien desde el {d}',current_since:'vigente desde el {d}',
 same_as:'igual que el {d}',trend:'{a} → {b} desde el {d}',short_sample:'muestra corta: {n} día(s) desde el corte, hacen falta 7',hist_start:'el historial empieza hoy',observing:'En observación',before_approx:'antes ≈',
 evidence:'Evidencia',what_to_do:'Qué hacer',with_:'Con qué',check:'Comprobar',tradeoff_h:'Contrapartida, cálculo y confianza',tradeoff:'Contrapartida',calc:'Cálculo',confidence:'Confianza',nothing:'Nada que recomendar en este periodo.',nothing_yet:'Nada probado todavía: {n} en estudio, aquí debajo.',study_h:'En estudio ({n})',study_hint:'Detectadas en tus datos pero aún sin probar. Suben solas cuando se repiten 3 días y ahorran lo suficiente para ti.',dismissed_h:'Descartadas ({n})',dismissed_hint:'Ocultadas por ti. Las que tienen importe vuelven solas si se duplica.',dismiss:'No aplica',dismiss_tip:'Oculta esta tarjeta; vuelve si su coste se duplica',restore:'Recuperar',
 cost_day:'Coste por día',cost_msg:'Coste por mensaje',turns_msg:'Turnos por mensaje',avg_ctx:'Contexto medio',change:'Cambio',short_warn:'Muestra corta: {n} día(s) por lado. No concluyente (depende mucho del tipo de tarea).',
 win_cap:'Llevas <b>{c}</b> eq. ({tok} tokens) de unos <b>{cap}</b> que parece permitir esta ventana; te quedan ≈<b>{left}</b>{now}.',win_now:' (≈{n} % contando hasta este minuto)',win_nocap:'Llevas <b>{c}</b> eq. ({tok} tokens) en esta ventana. El límite solo se estima cuando pasas del 10 % del plan.',period_sub:'{n} tokens en total',help_period:'Coste equivalente total del periodo elegido y los tokens (trozos de texto) procesados.',help_msg:'Coste medio de cada mensaje que envías. Un turno es una respuesta del modelo; un mensaje puede provocar varios.',help_sub:'Los subagentes son agentes auxiliares que Claude lanza solo (buscar, revisar…). Su coste forma parte del total.',help_cw:'Escritura de caché: guardar la conversación para reenviarla barato. Se paga al abrir sesión, tras compactar, al cambiar de modelo y tras una pausa larga.',help_over:'Contexto sobre {n}: cuando la conversación pasa de ese tamaño, cada respuesta reenvía el exceso. Compactar es que Claude resuma la charla para reducirla.',help_best:'Modelo más barato que habría bastado según lo que hizo la sesión; la cifra es el ahorro estimado.',help_went:'En qué se fue la mayor parte del coste de esta sesión.',help_compact:'Veces que Claude resumió la conversación por ser demasiado larga.',help_msgs:'Mensajes que enviaste en la sesión.',daily_note:'Lectura de caché: reenviar la conversación desde la caché (barato por token). Escritura de caché: guardar contexto nuevo (más caro). Input sin caché: texto nuevo enviado sin cachear. Salida: lo que escribe el modelo, incluido su razonamiento (thinking).',odo_eur:'euros hoy',odo_usd:'dólares hoy',h_title:'{h} h: {v}'}};
let LANGSEL='';try{LANGSEL=localStorage.getItem('tk-lang')||''}catch(e){}
const DEFLANG='__DEFLANG__';
let LANG=(LANGSEL==='en'||LANGSEL==='es')?LANGSEL:DEFLANG;
const t=(k,v)=>{let s=(I18N[LANG]||I18N.en)[k];if(s===undefined)s=I18N.en[k];if(s===undefined)s=k;if(v)for(const x in v)s=s.split('{'+x+'}').join(v[x]);return s};
const LOC=()=>LANG=='es'?'es-ES':'en-US';
const $=id=>document.getElementById(id);
function applyI18n(){document.documentElement.lang=LANG;document.title=t('title');
 document.querySelectorAll('[data-i18n]').forEach(e=>e.textContent=t(e.dataset.i18n));
 document.querySelectorAll('[data-i18n-aria]').forEach(e=>e.setAttribute('aria-label',t(e.dataset.i18nAria)));
 document.querySelectorAll('[data-i18n-title]').forEach(e=>e.title=t(e.dataset.i18nTitle));
 $('lang').value=LANG;const o=$('client').querySelector('[value=otros]');if(o)o.textContent=t('others')}
const num=(v,d)=>(v||0).toLocaleString(LOC(),{minimumFractionDigits:d,maximumFractionDigits:d,useGrouping:false});
const tok=n=>{n=n||0;return n>=999.95e6?num(n/1e9,1)+'B':n>=999.5e3?num(n/1e6,1)+'M':n>=1e3?num(n/1e3,0)+'K':Math.round(n)+''};
let CUR={code:'USD',sym:'$',rate:1};
const usd=n=>{const v=(n||0)*CUR.rate;return num(v,v<10?2:0)+'\u00a0'+CUR.sym};
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const short=p=>esc((p||'').replace(/^\/(home|Users)\/[^/]+/,'~'));
const css=v=>getComputedStyle(document.documentElement).getPropertyValue(v).trim();
function table(rows,cols){if(!rows.length)return '<p class="mut">'+t('no_data')+'</p>';
 const max=Math.max(...rows.map(r=>cols[1].v(r)||0),1);
 return '<table><tr>'+cols.map(c=>'<th>'+c.h+'</th>').join('')+'</tr>'+rows.map(r=>'<tr>'+cols.map((c,i)=>'<td>'+(i==1&&c.bar!==false?'<div style="display:flex;gap:6px;align-items:center;justify-content:flex-end">'+c.f(c.v(r))+'<div class="bar" style="width:'+Math.max(2,44*(c.v(r)||0)/max)+'px"></div></div>':c.f(c.v(r),r))+'</td>').join('')+'</tr>').join('')+'</table>'}
const onKey=e=>e.onkeydown=ev=>{if(ev.key=='Enter'||ev.key==' '){ev.preventDefault();e.click()}};
function fixed(el,f){if(!f.measured){el.innerHTML='<p class="empty">'+t('fx_none')+'</p>';return}
 const C=['--a','--b','--c','--d','--e'],tot=f.parts.reduce((s,p)=>s+p.tokens,0)||1;
 const N={system:t('fx_system'),builtin:t('fx_builtin'),claude_md:t('fx_claude_md'),rest:t('fx_rest')};
 const nm=k=>k.startsWith('mcp:')?t('fx_mcp',{n:esc(k.slice(4))}):(N[k]||esc(k));
 el.innerHTML='<p style="margin:4px 0 0">'+t('fx_head',{n:tok(f.base),c:usd(f.cost),p:f.share.toFixed(0),s:f.sessions})+'</p>'
  +'<div class="fxbar">'+f.parts.map((p,i)=>'<i style="width:'+(100*p.tokens/tot)+'%;background:'+css(C[i%C.length])+'" title="'+nm(p.k)+'"></i>').join('')+'</div>'
  +'<div class="fxl">'+f.parts.map((p,i)=>'<i style="background:'+css(C[i%C.length])+'"></i><span>'+nm(p.k)+'</span><span class="n">~'+tok(p.tokens)+'</span>').join('')+'</div>'
  +(f.deferred?'<p class="mut" style="font-size:12px;margin:8px 0 0">'+t('fx_deferred',{n:f.deferred})+'</p>':'')}
function tbl(el,rows,cols,opt){opt=opt||{};el._st=el._st||{k:null,d:-1};const PS=opt.page||8;el._pg=el._pg||0;
 const draw=()=>{let rs=rows.slice();const st=el._st;if(st.k!=null){const c=cols[st.k],f=c.s||c.v;rs.sort((a,b)=>{const x=f(a),y=f(b);return (typeof x=='string'?String(x).localeCompare(y):(x||0)-(y||0))*st.d})}
  const np=Math.max(1,Math.ceil(rs.length/PS));if(el._pg>=np)el._pg=np-1;const off=el._pg*PS,vis=rs.slice(off,off+PS);
  el.innerHTML=table(vis,cols)+(np>1?'<div class="pager"><button type="button" data-p="-1"'+(el._pg?'':' disabled')+' aria-label="'+t('prev')+'">‹</button><span>'+(off+1)+'–'+(off+vis.length)+' '+t('of')+' '+rs.length+'</span><button type="button" data-p="1"'+(el._pg<np-1?'':' disabled')+' aria-label="'+t('next')+'">›</button></div>':'');
  el.querySelectorAll('.pager button').forEach(b=>b.onclick=()=>{el._pg+=+b.dataset.p;draw();const n=el.querySelector('.pager button[data-p="'+b.dataset.p+'"]:not([disabled])')||el.querySelector('.pager button:not([disabled])');if(n)n.focus()});
  const ths=el.querySelectorAll('th');if(!ths.length)return;
  cols.forEach((c,i)=>{const f=c.s||c.v,t0=f(rows[0]);if(t0===null||typeof t0!='object'){const th=ths[i];th.classList.add('s');th.title=(c.help?c.help+' · ':'')+t('sort');th.tabIndex=0;
   th.setAttribute('aria-sort',st.k===i?(st.d<0?'descending':'ascending'):'none');
   if(st.k===i)th.textContent+=st.d<0?' ▾':' ▴';th.onclick=()=>{st.d=st.k===i?-st.d:-1;st.k=i;el._pg=0;draw();const n=el.querySelectorAll('th')[i];if(n)n.focus()};onKey(th)}});
  if(opt.click)el.querySelectorAll('tr').forEach((tr,i)=>{if(!i)return;tr.classList.add('click');tr.tabIndex=0;tr.setAttribute('aria-expanded','false');
   tr.onclick=()=>{const nx=tr.nextElementSibling,was=!!(nx&&nx.classList.contains('det'));tr.setAttribute('aria-expanded',was?'false':'true');opt.click(vis[i-1],tr,cols.length)};onKey(tr)})};
 draw()}
const RM=matchMedia('(prefers-reduced-motion: reduce)').matches;
function odometer(el,v){const txt=((v||0)*CUR.rate).toFixed(2),sep=LANG=='es'?',':'.';el.setAttribute('aria-label',txt.replace('.',sep)+' '+t(CUR.code=='EUR'?'odo_eur':'odo_usd'));if(el.dataset.cur!=CUR.sym||el.dataset.sep!=sep){el.dataset.len='';el.dataset.cur=CUR.sym;el.dataset.sep=sep}
 if(el.dataset.len!=txt.length){el.innerHTML=[...txt].map((ch,i)=>ch=='.'?'<span class="sep">'+sep+'</span>':'<span class="dg'+(i>txt.length-3?' dec':'')+'"><span class="reel">'+'0123456789'.split('').map(d=>'<span>'+d+'</span>').join('')+'</span></span>').join('')+'<span class="cur">'+CUR.sym+'</span>';el.dataset.len=txt.length}
 const reels=el.querySelectorAll('.reel'),ds=txt.replace('.','');
 requestAnimationFrame(()=>requestAnimationFrame(()=>reels.forEach((r,i)=>{r.style.transitionDelay=(RM?0:i*90)+'ms';r.style.transform='translateY(-'+(+ds[i]*1.15)+'em)'})))}
const GW={};
function planShare(x){const p=x.plan;if(!p)return '';if(p.five_hour&&p.five_hour.pct==null)return ' · '+usd(p.five_hour.cost)+' '+t('last5');const f=(k,n)=>p[k]&&p[k].pct!=null?n+' '+num(p[k].pct,p[k].pct<10?1:0)+' %':'';
 const a=[f('five_hour',t('w5')),f('seven_day',t('wk'))].filter(Boolean);return a.length?' · <b>'+a.join(' · ')+'</b> '+t('of_plan'):''}
function gauges(el,live,L){if(!live.length){el.innerHTML='<p class="empty">'+t('live_empty')+'</p>';return}
 const max=Math.max(L.ctx*4/3,...live.map(x=>x.ctx||0)),mk=100*L.ctx/max;
 el.innerHTML=live.map(x=>{const w=Math.min(100,100*(x.ctx||0)/max);return '<div class="gauge'+(x.warn_ctx?' over':'')+'" data-s="'+esc(x.session)+'" data-w="'+w+'">'+
  '<div class="gh"><b>'+sess(x.title,x.session)+'</b><span class="p">'+short(x.project)+'</span><span class="v">'+tok(x.ctx)+'</span></div>'+
  '<div class="track" role="meter" aria-valuemin="0" aria-valuemax="'+Math.round(max)+'" aria-valuenow="'+(x.ctx||0)+'" aria-label="'+t('ctx_aria')+'"><i class="fill" style="--w:'+(GW[x.session]??0)+'%"></i><span class="mark" style="left:'+mk+'%" data-l="'+tok(L.ctx)+'"></span></div>'+
  '<div class="gm"><span class="'+(x.warn_cost?'w':'')+'">'+usd(x.cost)+' '+t('accumulated')+'</span>'+planShare(x)+' · '+x.prompts+' '+t('messages')+' · '+x.compacts+' '+t(x.compacts==1?'compaction':'compactions')+' · '+t('subagents_lc')+' '+usd(x.sub_cost)+
  (x.warn_ctx?' · <span class="w">'+t('over_compact',{n:tok(L.ctx)})+'</span>':'')+'</div></div>'}).join('');
 requestAnimationFrame(()=>requestAnimationFrame(()=>el.querySelectorAll('.gauge').forEach(g=>{g.querySelector('.fill').style.setProperty('--w',g.dataset.w+'%');GW[g.dataset.s]=g.dataset.w})))}
let FIRST=true,PREV={},RECS=[];
const hm=m=>m<60?m+' min':m<1440?Math.floor(m/60)+' h '+(m%60)+' min':Math.floor(m/1440)+' d '+Math.floor(m%1440/60)+' h';
const roll=w=>'<div class="pl"><div class="gh"><b>'+(w.kind=='five_hour'?t('last5h'):t('last7d'))+'</b><span class="v">'+usd(w.cost)+'</span></div><div class="gm">'+t('plan_nopct',{n:tok(w.tokens)})+'</div></div>';
function plan(p){const c=$('plancard');c.hidden=!p;$('nav_plan').hidden=!p;if(!p)return;
 if(!p.source){$('planage').textContent=t('plan_nolecture');$('plan').innerHTML=p.windows.map(roll).join('');return}
 $('planage').textContent=t('plan_src',{src:p.source,at:p.at})+(p.age_min>5?t('plan_ago',{t:hm(p.age_min)}):'');
 $('plan').innerHTML=p.windows.map(w=>{if(w.pct==null)return roll(w);const n=w.kind=='five_hour'?t('five_h'):t('week'),lv=w.pct>=90?' red':w.pct>=75?' amb':'';
  const cap=w.cap?t('win_cap',{c:usd(w.cost),tok:tok(w.tokens),cap:usd(w.cap),left:usd(w.left),now:(w.pct_now!=null&&Math.abs(w.pct_now-w.pct)>=3?t('win_now',{n:w.pct_now.toFixed(0)}):'')})+' '+(w.eta?'<span class="w">'+t('eta',{eta:w.eta})+'</span>':t('noeta',{reset:w.reset})):t('win_nocap',{c:usd(w.cost),tok:tok(w.tokens)});
  return '<div class="pl'+lv+'"><div class="gh"><b>'+n+'</b><span class="p">'+t('resets',{t:hm(Math.max(0,w.reset_min)),at:w.reset})+'</span><span class="v">'+w.pct.toFixed(0)+' %</span></div>'+
   '<div class="track" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="'+w.pct+'" aria-label="'+n+'"><i class="fill" style="--w:'+Math.min(100,w.pct)+'%"></i></div>'+
   '<div class="gm">'+cap+'</div></div>'}).join('')}
const PTOK='__PAGETOKEN__';
document.addEventListener('click',async e=>{const b=e.target.closest('.ap');if(!b)return;e.preventDefault();e.stopPropagation();
 if(b.dataset.u&&!confirm(t('undo_confirm')))return;
 b.disabled=true;let res;try{res=await fetch(b.dataset.u?'/api/undo':'/api/apply',{method:'POST',headers:{'Content-Type':'application/json','X-Tokenometro-Page':PTOK},body:JSON.stringify({id:b.dataset.id})});res=await res.json()}catch(_){res={ok:false}}
 if(!res.ok){b.disabled=false;alert(t('apply_failed')+(res.error?': '+res.error:''));return}
 load()});
document.addEventListener('click',async e=>{const b=e.target.closest('.dm,.rs');if(!b)return;e.preventDefault();e.stopPropagation();
 b.disabled=true;let res;try{res=await fetch(b.classList.contains('dm')?'/api/dismiss':'/api/restore',{method:'POST',headers:{'Content-Type':'application/json','X-Tokenometro-Page':PTOK},body:JSON.stringify({id:b.dataset.id})});res=await res.json()}catch(_){res={ok:false}}
 if(!res.ok){b.disabled=false;alert(t('apply_failed')+(res.error?': '+res.error:''));return}
 load()});
document.addEventListener('click',async e=>{const b=e.target.closest('.cp');if(!b)return;e.preventDefault();e.stopPropagation();
 const x=(RECS[+b.dataset.i]||{}).prompt||'';try{await navigator.clipboard.writeText(x)}catch(_){const a=document.createElement('textarea');a.value=x;document.body.appendChild(a);a.select();document.execCommand('copy');a.remove()}
 const o=b.textContent;b.textContent=t('copied');b.classList.add('ok');setTimeout(()=>{b.textContent=o;b.classList.remove('ok')},1600)});
function stacked(el,rows,keys){const W=Math.max(el.clientWidth,320),H=180,P=28;
 if(!rows.length){el.innerHTML='<p class="mut">'+t('no_data')+'</p>';return}
 const tot=rows.map(r=>keys.reduce((s,k)=>s+(r[k.k]||0),0)),max=Math.max(...tot,1e-9),bw=(W-P)/rows.length;
 let s='<svg width="'+W+'" height="'+(H+20)+'"'+(FIRST?' class="grow"':'')+'>';
 rows.forEach((r,i)=>{let y=H;keys.forEach(k=>{const h=(H-10)*(r[k.k]||0)/max;y-=h;s+='<rect x="'+(P+i*bw+1)+'" y="'+y+'" width="'+Math.max(1,bw-2)+'" height="'+h+'" rx="'+(bw>12?2:0)+'" fill="'+css(k.c)+'" style="animation-delay:'+(i*40)+'ms"><title>'+r.day+' · '+k.n+': '+usd(r[k.k])+'</title></rect>'});
  if(rows.length<=31&&(i%Math.ceil(rows.length/10)==0))s+='<text x="'+(P+i*bw+bw/2)+'" y="'+(H+14)+'" text-anchor="middle">'+r.day.slice(5)+'</text>'});
 s+='<text x="0" y="12">'+usd(max)+'</text></svg>';el.innerHTML=s}
const pct=v=>(v||0).toFixed(0)+'%';
const FITN={opus:'Opus',opusplan:'opusplan',sonnet:'Sonnet',haiku:'Haiku'};
const fit=x=>{const f=x.fit;if(!f)return '<span class="mut">-</span>';return '<span class="fit fit-'+f.model+'" title="'+esc(f.why)+'">'+FITN[f.model]+'</span>'+(f.save>=0.5?'<br><span class="mut">−'+usd(f.save)+'</span>':'')};
const sess=(title,id)=>esc(title||(id||'').slice(0,8));
async function openSession(x,tr,n){const nx=tr.nextElementSibling;if(nx&&nx.classList.contains('det')){const di=nx.querySelector('.detin');di.classList.remove('open');setTimeout(()=>nx.remove(),RM?0:450);return}
 if(tr._busy)return;tr._busy=true;let d;
 try{const res=await fetch('/api/session?id='+encodeURIComponent(x.session));if(!res.ok)throw new Error(res.status);d=await res.json()}catch(e){d=null}finally{tr._busy=false}
 if(!d||!tr.isConnected){tr.setAttribute('aria-expanded','false');return}const tb=(rows,cols)=>table(rows,cols);
 const html='<div class="g"><div><h3>'+t('by_model')+'</h3>'+tb(d.by_model,[{h:t('model'),v:y=>y,f:y=>esc(y.model.replace('claude-',''))+(y.sidechain?' <span class="mut">'+t('subag')+'</span>':'')},{h:t('cost'),v:y=>y.cost,f:usd},{h:t('turns'),v:y=>y.turns,f:v=>v}])+
  '<h3 style="margin-top:10px">'+t('segments')+'</h3>'+tb(d.segments,[{h:t('from'),v:y=>y.from,f:v=>esc(v.slice(5,16).replace('T',' '))},{h:t('cost'),v:y=>y.cost,f:usd},{h:t('turns'),v:y=>y.turns,f:v=>v},{h:t('max_ctx'),v:y=>y.peak,f:tok}])+'</div>'+
  '<div><h3>'+t('tools')+'</h3>'+tb(d.tools,[{h:t('tool'),v:y=>y.name,f:esc},{h:t('tok_h'),v:y=>(y.chars||0)/4,f:tok},{h:t('calls'),v:y=>y.calls,f:v=>v},{h:t('errors'),v:y=>y.errors,f:v=>v}])+
  '<h3 style="margin-top:10px">'+t('subagents')+'</h3>'+tb(d.agents,[{h:t('subagent'),v:y=>y,f:y=>esc(y.description||y.agent.slice(0,10))+' <span class="mut">'+esc(y.type||'')+'</span>'},{h:t('cost'),v:y=>y.cost,f:usd},{h:t('model'),v:y=>y.models,f:v=>esc((v||'').replace(/claude-/g,''))},{h:t('turns'),v:y=>y.turns,f:v=>v}])+'</div>'+
  '<div><h3>'+t('biggest_calls')+'</h3>'+tb(d.big_calls,[{h:t('what'),v:y=>y,f:y=>'<div class="code"><b>'+esc(y.name)+'</b> '+esc((y.input_head||'').replace(/\/(home|Users)\/[^/\s]+/g,'~'))+'</div>'},{h:t('tok_h'),v:y=>(y.result_chars||0)/4,f:tok}])+'</div></div>';
 tr.insertAdjacentHTML('afterend','<tr class="det"><td colspan="'+n+'"><div class="detin"><div>'+html+'</div></div></td></tr>');
 const di=tr.nextElementSibling.querySelector('.detin');requestAnimationFrame(()=>requestAnimationFrame(()=>di.classList.add('open')))}
function alerts(r){const a=[];
 if(r.unpriced&&r.unpriced.length){const tip=t('al_unpriced',{m:r.unpriced.map(m=>m.model).join(', ')}).replace(/<[^>]+>/g,'');
  a.push('<span class="chip info" tabindex="0" title="'+esc(tip)+'" aria-label="'+esc(tip)+'">'+t('al_unp',{n:r.unpriced.length})+'</span>')}
 if(r.update){const tip=t('al_update_tip',{c:r.update.current,n:r.update.latest,h:r.update.how});
  a.push('<a class="chip" href="'+esc(r.update.url)+'" target="_blank" rel="noopener" title="'+esc(tip)+'" aria-label="'+esc(tip)+'">'+t('al_update',{n:esc(r.update.latest)})+'</a>')}
 const o=r.pace&&r.pace.odd;if(o){const d=o.day.slice(8,10)+'-'+o.day.slice(5,7),tip=t('odd_tip',{d:d,c:usd(o.cost),x:num(o.ratio,1),m:usd(o.median),s:o.title||(o.session||'').slice(0,8),sc:usd(o.session_cost)});
  a.push('<a class="chip odd" href="#s-sessions" title="'+esc(tip)+'" aria-label="'+esc(tip)+'">'+esc(t('odd',{d:d}))+'</a>')}
 $('alerts').innerHTML=a.join('')}
function csvLinks(){const d=new Date(),m=$('month').value||d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0'),c=$('client').value;
 const u=by=>'/api/report.csv?month='+m+'&by='+by+'&client='+encodeURIComponent(c)+'&cur='+CUR.code+'&lang='+LANG;$('csv_s').href=u('session');$('csv_c').href=u('client')}
let SEQ=0;
async function load(){const d=$('days').value,c=$('client').value,my=++SEQ;let r;
 // only the newest request draws: a slow older answer must not overwrite a newer one
 // a server that stopped or restarted mid-request must not leave "Updating..." on screen forever
 const ac=new AbortController(),tm=setTimeout(()=>ac.abort(),60000);
 try{const res=await fetch('/api/data?days='+d+'&client='+encodeURIComponent(c)+'&cut='+encodeURIComponent($('cut').value||'')+'&cur='+CURSEL+'&lang='+LANGSEL,{signal:ac.signal});if(!res.ok)throw new Error(res.status);r=await res.json()}
 catch(e){if(my===SEQ)$('gen').textContent=t('load_err');return}
 finally{clearTimeout(tm)}
 if(my!==SEQ)return;
 if(!LANGSEL&&r.lang&&r.lang!=LANG){LANG=r.lang;applyI18n()}
 CUR=r.currency||CUR;$('cur').value=CUR.code;$('eqn').textContent=CUR.code=='EUR'?t('eq_eur',{rate:num(CUR.eur_rate,2)})+(CUR.eur_date?' ('+CUR.eur_date+')':'')+'.':t('eq_usd');$('cur').querySelector('[value=EUR]').disabled=!(CUR.eur_rate>0);
 if(!$('cut').value)$('cut').value=r.compare.cut;
 const L=r.limits;$('livehint').textContent=t('live_hint',{n:L.live_min});
 {const hm={};r.today_hours.forEach(h=>hm[h.hour]=h.cost);const mx=Math.max(...Object.values(hm),1e-9),nh=new Date().getHours();
  $('spark').className='spark'+(FIRST?' grow':'');$('spark').innerHTML=[...Array(24)].map((_,h)=>{const v=hm[h]||0;return '<i class="'+(h==nh?'now':v?'':'z')+'" style="height:'+Math.max(3,100*v/mx)+'%;animation-delay:'+(h*25)+'ms" title="'+t('h_title',{h:String(h).padStart(2,'0'),v:usd(v)})+'"></i>'}).join('')}
 $('pulse').classList.toggle('on',r.live.length>0);plan(r.plan);alerts(r);csvLinks();gauges($('live'),r.live,L);odometer($('odo'),r.kpi_today.cost);
 const tt0=r.kpi_today,k=r.kpi_range;
 $('todaysub').innerHTML=t('today_sub',{tok:tok(tt0.tokens),n:tt0.prompts,c:usd(k.cost)});
 {const p=r.pace;$('pace').innerHTML=p&&p.month>0.01?t('pace',{m:usd(p.month),p:usd(p.proj)})+(p.last_month>0.01?t('pace_last',{l:usd(p.last_month)}):''):''}$('gen').textContent=t('updated',{t:r.generated.slice(11,16)});
 $('kpis').innerHTML=[[t('period_k'),usd(k.cost),t('period_sub',{n:tok(k.tokens)}),'help_period'],
  [t('per_msg'),usd(k.cost_per_prompt),t('turns_plus',{a:(k.prompts?num(k.main_turns/k.prompts,1):0),b:(k.prompts?num(k.sub_turns/k.prompts,1):0)}),'help_msg'],
  [t('subagents'),(k.cost?100*k.sub_cost/k.cost:0).toFixed(0)+'%',t('of_cost'),'help_sub'],
  [t('cache_write'),usd(k.c_cw),t('pct_cost_comp',{p:(k.cost?100*k.c_cw/k.cost:0).toFixed(0),n:k.compacts}),'help_cw'],[t('over_k',{n:tok(L.ctx)}),'~'+usd(k.over),t('over_sub',{p:(k.cost?100*k.over/k.cost:0).toFixed(0),n:tok(L.ctx)}),'help_over']].map(x=>'<div class="k" title="'+esc(t(x[3],{n:tok(L.ctx)}))+'"><span>'+x[0]+'</span><b>'+x[1]+'</b><span>'+x[2]+'</span></div>').join('');
 const keys=[{k:'c_cr',n:t('cache_read'),c:'--b'},{k:'c_cw',n:t('cache_write'),c:'--c'},{k:'c_inp',n:t('input_nc'),c:'--e'},{k:'c_out',n:t('output_think'),c:'--d'}];
 $('leg').innerHTML=keys.map(k=>'<span><i style="background:'+css(k.c)+'"></i>'+k.n+'</span>').join('');
 stacked($('daily'),r.daily,keys);FIRST=false;
 tbl($('models'),r.models,[{h:t('model'),v:x=>x.model,f:esc},{h:t('cost'),v:x=>x.cost,f:usd},{h:t('tokens'),v:x=>x.tokens,f:tok},{h:t('turns'),v:x=>x.turns,f:v=>v}]);
 if(r.branches.length)tbl($('branches'),r.branches,[{h:t('branch'),v:x=>x.branch,f:(v,x)=>'<b>'+esc(x.task||v)+'</b>'+(x.task?' <span class="mut">'+esc(v)+'</span>':'')+'<br><span class="mut">'+esc(x.repos.join(', '))+(x.clients.length?' · '+esc(x.clients.join(', ')):'')+'</span>'},{h:t('cost'),v:x=>x.cost,f:usd},{h:t('sessions'),v:x=>x.sessions,f:v=>v},{h:t('last_day'),v:x=>x.last,f:v=>esc(v)}]);
 else $('branches').innerHTML='<p class="empty">'+t('no_branches')+'</p>';
 fixed($('fixed'),r.fixed);
 tbl($('clients'),r.clients,[{h:t('client'),v:x=>x.client,f:v=>esc(v=='otros'?t('others'):v)},{h:t('cost'),v:x=>x.cost,f:usd},{h:t('tokens'),v:x=>x.tokens,f:tok},{h:t('sessions'),v:x=>x.sessions,f:v=>v}]);
 const sk=[{k:'c_cr',n:t('cache_read'),c:'--b'},{k:'c_cw',n:t('cache_write'),c:'--c'},{k:'c_inp',n:t('input_nc'),c:'--e'},{k:'c_out',n:t('output'),c:'--d'},{k:'c_think',n:t('thinking'),c:'--h'},{k:'sub_cost',n:t('subagents'),c:'--g'}];
 $('sleg').innerHTML=sk.map(k=>'<span><i style="background:'+css(k.c)+'"></i>'+k.n+'</span>').join('');
 const smax=Math.max(...r.sessions.map(x=>x.cost||0),1e-9);
 const sbar=x=>'<div style="display:flex;gap:6px;align-items:center;justify-content:flex-end">'+usd(x.cost)+'<div class="sbar" style="width:'+Math.max(4,84*(x.cost||0)/smax)+'px">'+sk.map(k=>'<i style="width:'+(x.cost?100*(x[k.k]||0)/x.cost:0)+'%;background:'+css(k.c)+'" title="'+k.n+': '+usd(x[k.k])+'"></i>').join('')+'</div></div>';
 const fue=x=>{if(!x.cost)return '-';const m=sk.reduce((b,k)=>(x[k.k]||0)>(x[b.k]||0)?k:b,sk[0]);return '<span style="display:inline-flex;align-items:center;gap:5px"><i style="width:8px;height:8px;border-radius:2px;background:'+css(m.c)+'"></i>'+m.n+' '+(100*(x[m.k]||0)/x.cost).toFixed(0)+'%</span>'};
 const tt=x=>x.top_tool?esc(x.top_tool.name)+' <span class="mut">~'+tok((x.top_tool.chars||0)/4)+' · '+x.top_tool.calls+'</span>':'-';
 tbl($('sessions'),r.sessions,[{h:t('session'),v:x=>x,f:(x)=>'<b>'+esc(x.title||x.session.slice(0,8))+'</b><br><span class="mut">'+short(x.project)+' · '+esc((x.first||'').slice(0,16).replace('T',' '))+'</span>'},
  {h:t('cost'),v:x=>x,s:x=>x.cost,f:sbar,bar:false},{h:t('best_model'),help:t('help_best'),v:x=>x,s:x=>x.fit?x.fit.save:-1,f:fit},{h:t('went_on'),help:t('help_went'),v:x=>x,f:fue},{h:t('top_tool'),v:x=>x,f:tt},{h:t('messages_h'),help:t('help_msgs'),v:x=>x.prompts,f:v=>v},{h:t('max_ctx'),v:x=>x.peak_ctx,f:tok},{h:t('over_k',{n:tok(L.ctx)}),help:t('help_over',{n:tok(L.ctx)}),v:x=>x.over,f:v=>v>0.5?'~'+usd(v):'-'},{h:t('compact_h'),help:t('help_compact'),v:x=>x.compacts,f:v=>v},{h:t('subagents'),v:x=>x,s:x=>x.cost?x.sub_cost/x.cost:0,f:x=>x.cost?(100*x.sub_cost/x.cost).toFixed(0)+'%':'-'}],{click:openSession});
 const stl={todo:t('st_todo'),warn:t('st_warn'),ok:t('st_ok'),info:t('st_info')};
 const openK=new Set([...document.querySelectorAll('#recs details[open],#recs_more details[open]')].map(d=>d.dataset.k));
 const dm=d=>d?d.slice(8,10)+'-'+d.slice(5,7):'';
 const fm=v=>v==null?'-':(Math.round(v*10)/10).toLocaleString(LOC());
 RECS=r.recs;const FILE=(r.recs_file||'').replace(/^\/home\/[^/]+/,'~');
 const SH=r.recs.filter(x=>x.stage!='study'&&x.stage!='dismissed'),ST=r.recs.filter(x=>x.stage=='study'),DM=r.recs.filter(x=>x.stage=='dismissed');
 const npend=SH.filter(x=>x.status=='todo'||x.status=='warn').length;$('nav_recs_n').textContent=npend;$('nav_recs_n').hidden=!npend;
 $('recs').innerHTML=SH.map(x=>{const ix=r.recs.indexOf(x);const sig=x.status+'|'+Math.round(x.impact||0)+'|'+x.metric;const chg=PREV[x.id]!=null&&PREV[x.id]!=sig;PREV[x.id]=sig;
  const pend=x.status=='todo'||x.status=='warn';const short=x.sample_days!=null&&x.sample_days<7&&x.status!='todo';
  const hist=(x.hist||[]).map(e=>'<i class="h-'+e.s+'" title="'+dm(e.d)+' · '+stl[e.s]+'"></i>').join('');
  const since=x.since?t(pend?'pending_since':x.status=='ok'?'fine_since':'current_since',{d:dm(x.since)}):'';
  const tr=x.trend&&x.trend.m!=null&&x.metric!=null?(x.trend.m==x.metric?t('same_as',{d:dm(x.trend.d)}):(x.trend.m>x.metric?'▼':'▲')+' '+t('trend',{a:fm(x.trend.m),b:fm(x.metric),d:dm(x.trend.d)})):'';
  return '<div class="rec st-'+x.status+(chg?' chg':'')+'"><h3><span>'+esc(x.title)+'<span class="badge">'+(short?t('observing'):stl[x.status])+'</span></span>'+(x.impact?'<span class="imp'+(x.status=='ok'?' mut':'')+'" title="'+esc(x.impact_note)+'">'+(x.status=='ok'?t('before_approx'):'≈')+usd(x.impact)+'</span>':'')+'</h3>'+
  '<div class="hist">'+(hist?'<span class="sq">'+hist+'</span>':'')+'<span>'+esc([x.metric!=null?fm(x.metric_money?x.metric*CUR.rate:x.metric)+' '+x.metric_label:'',since,tr,short?t('short_sample',{n:x.sample_days}):''].filter(Boolean).join(' · ')||t('hist_start'))+'</span></div>'+
  '<dl><dt>'+t('evidence')+'</dt><dd>'+esc(x.evidence)+'</dd><dt>'+t('what_to_do')+'</dt><dd>'+esc(x.action)+'</dd>'+
  ((x.tools||[]).length?'<dt>'+t('with_')+'</dt><dd class="tools">'+x.tools.map(o=>'<span><b>'+esc(o.n)+'</b> <em>'+esc(o.k)+'</em> '+esc(o.why)+' <em>· '+esc(o.cost)+'</em></span>').join('')+'</dd>':'')+
  '<dt>'+t('check')+'</dt><dd>'+esc(x.verify)+'</dd></dl>'+
  (x.can_apply?'<div class="pr ap-row"><span>'+t('apply_hint',{c:'<code>'+esc(x.change)+'</code>'})+'</span><button type="button" class="ap" data-id="'+esc(x.id)+'">'+t('apply')+'</button></div>':'')+
  (x.applied?'<div class="pr ap-row"><span>'+t('applied_on',{d:dm(x.applied.day),c:'<code>'+esc(x.change||'')+'</code>'})+(x.applied.before!=null&&x.applied.after!=null&&x.applied.days>0?' '+t('applied_ba',{a:fm(x.metric_money?x.applied.before*CUR.rate:x.applied.before),b:fm(x.metric_money?x.applied.after*CUR.rate:x.applied.after),n:x.applied.days}):' '+t('applied_wait'))+'</span><button type="button" class="ap" data-u="1" data-id="'+esc(x.id)+'">'+t('undo')+'</button></div>':'')+
  (x.prompt?'<div class="pr"><span>'+t('prompt_for')+' <code>'+esc(FILE)+'</code></span><button type="button" class="cp" data-i="'+ix+'">'+t('copy_prompt')+'</button></div>':'')+
  '<details data-k="d-'+x.id+'"'+(openK.has('d-'+x.id)?' open':'')+'><summary>'+t('tradeoff_h')+'</summary><dl><dt>'+t('tradeoff')+'</dt><dd>'+esc(x.tradeoff)+'</dd>'+(x.impact_note?'<dt>'+t('calc')+'</dt><dd>'+esc(x.impact_note)+'</dd>':'')+'<dt>'+t('confidence')+'</dt><dd>'+esc(x.confidence)+'</dd></dl></details>'+(x.status=='todo'?'<button type="button" class="dm" data-id="'+esc(x.id)+'" title="'+esc(t('dismiss_tip'))+'">'+t('dismiss')+'</button>':'')+'</div>'}).join('')||'<p class="mut">'+(ST.length?t('nothing_yet',{n:ST.length}):t('nothing'))+'</p>';
 const mo=(k,h,hint,list,row)=>list.length?'<details class="more" data-k="'+k+'"'+(openK.has(k)?' open':'')+'><summary>'+h+'</summary><p class="mut">'+hint+'</p><ul>'+list.map(row).join('')+'</ul></details>':'';
 $('recs_more').innerHTML=mo('m-study',t('study_h',{n:ST.length}),t('study_hint'),ST,x=>'<li><b>'+esc(x.title)+'</b>'+(x.impact?' <span class="mut">≈'+usd(x.impact)+'</span>':'')+'<br><span class="mut">'+esc((x.missing||[]).join(' · '))+'</span></li>')+
  mo('m-dismissed',t('dismissed_h',{n:DM.length}),t('dismissed_hint'),DM,x=>'<li><b>'+esc(x.title)+'</b> <button type="button" class="rs" data-id="'+esc(x.id)+'">'+t('restore')+'</button></li>');
 const B=r.compare.before,A=r.compare.after;
 const dl=(a,b,good)=>{if(!b)return '';const v=100*(a-b)/b;return '<span style="color:'+css(Math.abs(v)<0.5?'--mut':((v<0)==good)?'--b':'--f')+'">'+(v>0?'+':'')+v.toFixed(0)+'%</span>'};
 const cr=[[t('cost_day'),x=>x.cost_day,usd,1],[t('cost_msg'),x=>x.cost_per_prompt,usd,1],[t('turns_msg'),x=>x.turns_per_prompt,v=>num(v,1),1],
  [t('avg_ctx'),x=>x.avg_ctx,tok,1],[t('subagents'),x=>x.sub_pct,pct,1],[t('over_k',{n:tok(L.ctx)}),x=>x.over_pct,pct,1],[t('messages_h'),x=>x.prompts,v=>v,null]];
 $('cmp').innerHTML=(A.days<5?'<p class="warn" style="margin:0 0 6px;font-size:12px">'+t('short_warn',{n:A.days})+'</p>':'')+'<table class="cmp"><tr><th></th><th>'+B.frm.slice(5)+' → '+B.to.slice(5)+'</th><th>'+A.frm.slice(5)+' → '+A.to.slice(5)+'</th><th>'+t('change')+'</th></tr>'+cr.map(m=>'<tr><td>'+m[0]+'</td><td>'+m[2](m[1](B))+'</td><td>'+m[2](m[1](A))+'</td><td>'+(m[3]?dl(m[1](A),m[1](B),true):'')+'</td></tr>').join('')+'</table>';
 tbl($('agents'),r.agents,[{h:t('subagent'),v:x=>x,f:x=>'<b>'+esc(x.description||x.agent.slice(0,10))+'</b><br><span class="mut">'+esc(x.type||'?')+' · '+sess(x.title,x.session)+' · '+esc((x.first||'').slice(0,16).replace('T',' '))+'</span>'},
  {h:t('cost'),v:x=>x.cost,f:usd},{h:t('model'),v:x=>x.models,f:v=>esc((v||'').replace(/claude-/g,''))},{h:t('turns'),v:x=>x.turns,f:v=>v},{h:t('max_ctx'),v:x=>x.peak_ctx,f:tok},{h:t('tools'),v:x=>x,s:x=>x.chars,f:x=>x.calls+' <span class="mut">~'+tok((x.chars||0)/4)+'</span>'}]);
}
let CURSEL='';try{CURSEL=localStorage.getItem('tk-cur')||''}catch(e){}
$('cur').onchange=()=>{CURSEL=$('cur').value;try{localStorage.setItem('tk-cur',CURSEL)}catch(e){};load()};
$('theme').value=document.documentElement.dataset.theme||'system';
$('theme').onchange=()=>{const v=$('theme').value;if(v==='system')delete document.documentElement.dataset.theme;else document.documentElement.dataset.theme=v;try{v==='system'?localStorage.removeItem('tk-theme'):localStorage.setItem('tk-theme',v)}catch(e){};load()};
try{matchMedia('(prefers-color-scheme: dark)').addEventListener('change',()=>{if(!document.documentElement.dataset.theme)load()})}catch(e){}
$('lang').onchange=()=>{LANGSEL=$('lang').value;LANG=LANGSEL;try{localStorage.setItem('tk-lang',LANGSEL)}catch(e){};applyI18n();load()};
['days','client','cut'].forEach(i=>$(i).onchange=load);$('re').onclick=async()=>{$('re').classList.add('busy');$('re').textContent=t('refreshing');try{await load()}finally{$('re').classList.remove('busy');$('re').textContent=t('refresh')}};
applyI18n();
{const d=new Date();$('month').value=d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0');$('month').max=$('month').value;$('month').onchange=csvLinks}
fetch('/api/clients').then(r=>r.json()).then(cs=>cs.forEach(c=>$('client').insertAdjacentHTML('beforeend','<option value="'+esc(c)+'">'+esc(c)+'</option>'))).catch(()=>{}).then(()=>{applyI18n();load()});
// sticky bar: shadow line once it sticks, and the section you are reading marked in it
{const bar=$('topbar'),links=[...document.querySelectorAll('#nav a')];let tick=0;
 const mark=()=>{tick=0;const h=bar.offsetHeight;document.documentElement.style.setProperty('--barh',h+'px');
  bar.classList.toggle('stuck',bar.getBoundingClientRect().top<=0&&scrollY>0);
  let cur=links[0];for(const a of links){const s=a.hidden?null:document.querySelector(a.getAttribute('href'));if(s&&!s.hidden&&s.getBoundingClientRect().top<=h+40)cur=a}
  if(innerHeight+scrollY>=document.documentElement.scrollHeight-4)cur=links.filter(a=>!a.hidden).pop();
  links.forEach(a=>a===cur?a.setAttribute('aria-current','true'):a.removeAttribute('aria-current'));
  if(cur){const n=$('nav'),l=cur.offsetLeft-n.offsetLeft;if(l<n.scrollLeft||l+cur.offsetWidth>n.scrollLeft+n.clientWidth)n.scrollLeft=l-16}};
 const ask=()=>{if(!tick)tick=requestAnimationFrame(mark)};addEventListener('scroll',ask,{passive:true});addEventListener('resize',ask);mark()}
// the minute refresh waits while you have a session open or are moving through a table with the keyboard
setInterval(()=>{const a=document.activeElement;if(!document.querySelector('tr.det')&&!(a&&a.closest&&a.closest('table,.pager')))load()},60000);
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype, filename=None):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def _local(self):
        # a web page that points its own domain at 127.0.0.1 (DNS rebinding) would send its domain as Host
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0] if not (self.headers.get("Host") or "").startswith("[") \
            else (self.headers.get("Host") or "").split("]")[0] + "]"
        if host not in ("127.0.0.1", "localhost", "[::1]"):
            self.send_error(403, "Tokenometro only answers on 127.0.0.1 or localhost")
            return False
        return True

    def do_POST(self):
        """Apply, undo, dismiss or restore a recommendation. Needs the token embedded in our own page: another site can't read
        that page, and can't send the custom header without a CORS preflight we never answer."""
        if not self._local():
            return
        u = urlparse(self.path)
        ok = hmac.compare_digest((self.headers.get("X-Tokenometro-Page") or "").encode("utf-8", "replace"), _PAGE_TOKEN.encode())
        origin = self.headers.get("Origin")
        if not ok or (origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost", "::1")):
            return self.send_error(403)
        act = {"/api/apply": apply_rec, "/api/undo": undo_rec, "/api/dismiss": dismiss_rec, "/api/restore": restore_rec}
        if u.path not in act:
            return self.send_error(404)
        try:
            n = min(int(self.headers.get("Content-Length") or 0), 4096)
            rid = json.loads(self.rfile.read(n) or b"{}").get("id")
            act[u.path](str(rid))
            self._send(json.dumps({"ok": True}), "application/json")
        except (ValueError, OSError, AttributeError) as e:
            self.send_response(400)
            b = json.dumps({"ok": False, "error": str(e)}).encode()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

    def do_GET(self):
        if not self._local():
            return
        u = urlparse(self.path)
        if u.path == "/api/quit":
            # a custom header can't be sent cross-origin without a preflight we never answer, so only local tools reach this
            ok = _TOKEN and hmac.compare_digest((self.headers.get("X-Tokenometro-Token") or "").encode("utf-8", "replace"), _TOKEN.encode())
            if not ok:
                self.send_error(403)
                return
            self._send("bye", "text/plain; charset=utf-8")
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        try:
            if u.path == "/":
                self._send(PAGE.replace("__DEFLANG__", LANG_DEFAULT).replace("__PAGETOKEN__", _PAGE_TOKEN), "text/html; charset=utf-8")
            elif u.path == "/api/data":
                qs = parse_qs(u.query)
                days = max(1, min(365, int(qs.get("days", ["7"])[0])))
                client = qs.get("client", [ALL])[0]
                if client not in CLIENTS and client != OTHERS:
                    client = ALL
                cut = qs.get("cut", [CUT_DEFAULT])[0]
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", cut):
                    cut = CUT_DEFAULT
                set_currency(qs.get("cur", [""])[0].upper())
                set_lang(qs.get("lang", [""])[0].lower())
                self._send(json.dumps(data(days, client, cut)), "application/json")
            elif u.path == "/api/session":
                sid = parse_qs(u.query).get("id", [""])[0]
                if not re.fullmatch(r"[0-9a-f-]{36}", sid):
                    return self.send_error(400)
                self._send(json.dumps(session_detail(sid)), "application/json")
            elif u.path == "/api/report.csv":
                qs = parse_qs(u.query)
                month = qs.get("month", [datetime.now().strftime("%Y-%m")])[0]
                if not re.fullmatch(r"[0-9]{4}-[0-9]{2}", month):
                    return self.send_error(400)
                by = "client" if qs.get("by", [""])[0] == "client" else "session"
                client = qs.get("client", [ALL])[0]
                if client not in CLIENTS and client != OTHERS:
                    client = ALL
                set_currency(qs.get("cur", [""])[0].upper())
                set_lang(qs.get("lang", [""])[0].lower())
                self._send("\ufeff" + report(month, by, client), "text/csv; charset=utf-8",
                           f"tokenometro-{month}-{'clients' if by == 'client' else 'sessions'}.csv")
            elif u.path == "/api/clients":
                self._send(json.dumps(list(CLIENTS) + [OTHERS]), "application/json")
            elif u.path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
            elif u.path == "/health":
                self._send("ok", "text/plain")
            else:
                self.send_error(404)
        except Exception as e:  # the dashboard must not take the server down
            self.send_error(500, str(e)[:200])


def _ver(v):
    return tuple(int(x) for x in re.findall(r"[0-9]+", v or "")[:3])


def check_update(force=False):
    """Once a day, ask GitHub which release is the latest. Only that request leaves your machine, with no data
    of yours; TOKENOMETRO_UPDATE_CHECK=0 turns it off. Failures are remembered too, so it never insists."""
    if _env("UPDATE_CHECK", "1") == "0":
        return
    c = _small_json(UPDATE_FILE)
    if not force and time.time() - (c.get("checked") or 0) < 86400:
        return
    c["checked"] = time.time()
    try:
        req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases/latest",
                                     headers={"User-Agent": f"tokenometro/{VERSION}", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            d = json.loads(r.read(65536).decode("utf-8"))
        if isinstance(d.get("tag_name"), str) and _ver(d["tag_name"]):
            c.update(latest=d["tag_name"].lstrip("v"), url=d.get("html_url") or f"https://github.com/{REPO}/releases")
    except (OSError, ValueError):
        pass
    try:
        with open(UPDATE_FILE + ".tmp", "w", encoding="utf-8") as f:
            json.dump(c, f)
        os.replace(UPDATE_FILE + ".tmp", UPDATE_FILE)
    except OSError:
        pass


def update_info():
    """A newer release, and how to get it for the way this copy was installed."""
    if _env("UPDATE_CHECK", "1") == "0":
        return None
    c = _small_json(UPDATE_FILE)
    if _ver(c.get("latest")) <= _ver(VERSION):
        return None
    via = _small_json(os.path.join(BASE, "install.json")).get("via") or "git"
    how = {"plugin": "claude plugin update tokenometro@tokenometro",
           "pipx": "pipx upgrade tokenometro && tokenometro install",
           "git": "git pull && python3 install.py"}.get(via, "git pull && python3 install.py")
    return {"latest": c["latest"], "current": VERSION, "url": c.get("url") or f"https://github.com/{REPO}/releases", "how": how}


def loop():
    last_snap = 0
    while True:
        try:
            ingest()
        except Exception as e:
            print("ingest error:", e, file=sys.stderr)
        if time.time() - last_snap > 3600:
            try:
                snapshot_recs()
                last_snap = time.time()
            except Exception as e:
                print("snapshot error:", e, file=sys.stderr)
            try:
                write_digest()
            except Exception as e:
                print("digest error:", e, file=sys.stderr)
            check_update()
        time.sleep(INGEST_EVERY)


_TOKEN = ""
_PAGE_TOKEN = secrets.token_hex(16)  # for the dashboard's own buttons; the stop token never goes into the page


def write_control():
    global _TOKEN
    _TOKEN = secrets.token_hex(16)
    tmp = CONTROL + ".tmp"
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as f:
        json.dump({"port": PORT, "pid": os.getpid(), "token": _TOKEN}, f)
    os.replace(tmp, CONTROL)


def main():
    for stream in (sys.stdout, sys.stderr):  # a Windows console in cp1252 must not crash on € or …
        try:
            stream.reconfigure(errors="replace")
        except AttributeError:
            pass
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if cmd == "ingest":
        t0 = time.time()
        n = ingest()
        set_lang(LANG_DEFAULT)
        r = data(7, ALL)
        k = r["kpi_range"]
        print(tr(f"{n} new records in {time.time()-t0:.1f}s · 7 days: {fmt_tok(k['tokens'])} tokens, "
                 f"{money(k['cost'], 2)} eq., cache {k['cache_pct']:.0f}%, {k['prompts']} messages",
                 f"{n} registros nuevos en {time.time()-t0:.1f}s · 7 días: {fmt_tok(k['tokens'])} tokens, "
                 f"{money(k['cost'], 2)} eq., caché {k['cache_pct']:.0f}%, {k['prompts']} mensajes"))
        for i in r["insights"]:
            print(" -", (f"[{i['pct']:.0f}%] " if i["pct"] is not None else "") + i["text"])
        for m in r["unpriced"]:
            print(" !", tr(f"{m['model']} is not in prices.json: valued at the _default price.",
                           f"{m['model']} no está en prices.json: se valora al precio de _default."))
        return
    if cmd == "report":
        # tracker.py report [YYYY-MM] [--by client] [--client NAME] [--cur EUR]
        args = sys.argv[2:]
        opt = lambda k, d: args[args.index(k) + 1] if k in args and args.index(k) + 1 < len(args) else d
        month = next((x for x in args if re.fullmatch(r"[0-9]{4}-[0-9]{2}", x)), datetime.now().strftime("%Y-%m"))
        set_lang(LANG_DEFAULT)
        set_currency(opt("--cur", "").upper())
        ingest()
        sys.stdout.flush()
        sys.stdout.buffer.write(report(month, opt("--by", "session"), opt("--client", ALL)).encode("utf-8"))  # bytes: no \r\r\n on Windows
        return
    if cmd == "digest":
        # tracker.py digest [YYYY-MM-DD]: the week (Monday to Sunday) holding that day; last week by default
        set_lang(LANG_DEFAULT)
        ingest()
        day = next((x for x in sys.argv[2:] if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", x)), None)
        d = date.fromisoformat(day) if day else date.today() - timedelta(days=7)
        sys.stdout.flush()
        sys.stdout.buffer.write(digest(d - timedelta(days=d.weekday())).encode("utf-8"))
        return
    if cmd == "backfill":
        t0 = time.time()
        n = backfill()
        print(tr(f"{n} calls completed in {time.time()-t0:.0f}s", f"{n} llamadas completadas en {time.time()-t0:.0f}s"))
        return
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)  # bind first: a second copy fails here and leaves .control alone
    write_control()
    threading.Thread(target=loop, daemon=True).start()
    try:
        srv.serve_forever()
    finally:
        try:
            with open(CONTROL, encoding="utf-8") as f:
                mine = json.load(f).get("token") == _TOKEN
            if mine:
                os.remove(CONTROL)
        except (OSError, ValueError):
            pass


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Tokenometro installer for Linux, macOS, WSL and Windows (Python 3.8+, no dependencies).

  python3 install.py               -> install/update, register the SessionStart hook and start it
  python3 install.py --statusline  -> also use statusline.py as statusLine (to see plan usage %)
  python3 install.py --uninstall   -> remove the hook and our statusLine (keeps files and data)

On Windows use `py install.py` (or `python install.py`). It only reads ~/.claude/projects and listens on
127.0.0.1: nothing leaves your machine.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time

SRC = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SRC)
import start  # noqa: E402  (same folder: data_dir, listening, stop)

WIN = os.name == "nt"
HOME = os.path.expanduser("~")
DEST = start.data_dir()
OLD = os.path.join(HOME, ".local", "share", "claude-tokens")  # folder used by earlier versions
SETTINGS = os.path.join(HOME, ".claude", "settings.json")
PORT = start.port_from(sys.argv[1:])
FILES = ("tracker.py", "start.py", "start.sh", "statusline.py", "install.py", "install.sh", "clients.example.json")
DOCS = ("README.md", "README.es.md", "LICENSE")
# any hook or statusLine that runs one of these is ours (this and earlier versions, any OS)
OURS = re.compile(r"(tokenometro|claude-tokens)[/\\](start\.(sh|py)|statusline\.py)\"?(\s|$)")


def q(path):
    return '"' + path.replace("\\", "/") + '"'  # forward slashes: the hook runs in Git Bash or PowerShell on Windows


def commands():
    """(hook, statusLine). On Linux and macOS through $HOME, like earlier versions, so a synced settings.json
    still works on another machine; on Windows with absolute paths to this Python."""
    if WIN:
        py = q(sys.executable)
        hook, status = f"{py} {q(os.path.join(DEST, 'start.py'))}", f"{py} {q(os.path.join(DEST, 'statusline.py'))}"
    else:
        hook = 'python3 "$HOME/.local/share/tokenometro/start.py"'
        status = 'python3 "$HOME/.local/share/tokenometro/statusline.py"'
    if PORT != "7878":
        hook += f" --port {PORT}"  # a custom port has to travel with the hook
    return hook, status


def ours(h):
    return isinstance(h, dict) and isinstance(h.get("command"), str) and bool(OURS.search(h["command"]))


def ours_alone(h):
    """Our statusLine on its own, not chained by the user in front of theirs (that one is theirs to keep)."""
    return ours(h) and "|" not in h["command"]


def edit_settings(mode, with_status):
    """mode add|remove. Returns "statusline-taken" if the user already has another statusLine."""
    if mode == "remove" and not os.path.exists(SETTINGS):
        return ""
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    s = {}
    if os.path.exists(SETTINGS):
        with open(SETTINGS, encoding="utf-8") as f:
            s = json.load(f)
        shutil.copy2(SETTINGS, SETTINGS + ".bak-tokenometro-" + time.strftime("%Y%m%d%H%M%S"))
    if not isinstance(s, dict):
        raise ValueError("settings.json is not a JSON object")
    hook, status = commands()
    if not isinstance(s.get("hooks"), dict):
        s["hooks"] = {}
    ss = s["hooks"].get("SessionStart")
    ss = s["hooks"]["SessionStart"] = ss if isinstance(ss, list) else []
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
    cur = s.get("statusLine") if isinstance(s.get("statusLine"), dict) else None
    mine = cur is None or ours_alone(cur)
    if with_status and mode == "add":
        if mine:
            s["statusLine"] = {"type": "command", "command": status}
        elif not ours(cur):  # already chained in front of theirs: nothing to say
            msg = "statusline-taken"
    if mode == "remove" and cur is not None and ours_alone(cur):
        del s["statusLine"]
    text = json.dumps(s, indent=2, ensure_ascii=False) + "\n"
    tmp = SETTINGS + ".tmp-tokenometro"
    with open(tmp, "w", encoding="utf-8") as f:  # write aside and swap: never a half-written settings.json
        f.write(text)
    os.replace(tmp, SETTINGS)
    return msg


def stop_tracker():
    """Only our own server: the token in .control proves it. Returns False if the port is taken by something else."""
    r = start.stop(DEST, PORT)
    if r != "unknown":
        return True
    if not WIN and stop_legacy():
        return True
    print(f"Port {PORT} is used by another program; it was left running. "
          "Set TOKENOMETRO_PORT (or --port) to use another port.")
    return False


def stop_legacy():
    """Versions before 0.2 have no /api/quit: find the pid on the port and kill it only if it runs tracker.py."""
    pid = ""
    try:
        if shutil.which("ss"):
            out = subprocess.run(["ss", "-ltnpH", f"sport = :{PORT}"], capture_output=True, text=True).stdout
            m = re.search(r"pid=(\d+)", out)
            pid = m.group(1) if m else ""
        elif shutil.which("lsof"):
            out = subprocess.run(["lsof", "-ti", f"tcp:{PORT}", "-sTCP:LISTEN"], capture_output=True, text=True).stdout
            pid = out.split()[0] if out.split() else ""
        if not pid:
            return False
        args = subprocess.run(["ps", "-o", "args=", "-p", pid], capture_output=True, text=True).stdout
        if "tracker.py" not in args:
            return False
        os.kill(int(pid), 15)
    except (OSError, ValueError):
        return False
    for _ in range(30):
        if not start.listening(PORT):
            return True
        time.sleep(0.1)
    return False


def copy_files():
    notes = []
    os.makedirs(DEST, exist_ok=True)
    if os.path.isdir(OLD) and not os.path.exists(os.path.join(DEST, "usage.db")) and os.path.realpath(OLD) != os.path.realpath(DEST):
        for f in ("usage.db", "usage.db-wal", "usage.db-shm", "clients.json", "clientes.json", "prices.json", "plan.json"):
            if os.path.isfile(os.path.join(OLD, f)):
                shutil.copy2(os.path.join(OLD, f), DEST)
        notes.append(f"- Data and settings copied from {OLD}; once everything works you can delete that folder.")
    if os.path.realpath(SRC) != os.path.realpath(DEST):  # running the installed copy: nothing to copy onto itself
        for f in FILES + DOCS:
            if os.path.isfile(os.path.join(SRC, f)):
                shutil.copy2(os.path.join(SRC, f), DEST)
        if not os.path.exists(os.path.join(DEST, "prices.json")):
            shutil.copy2(os.path.join(SRC, "prices.json"), DEST)
    if not any(os.path.exists(os.path.join(DEST, f)) for f in ("clients.json", "clientes.json")):
        shutil.copy2(os.path.join(SRC, "clients.example.json"), os.path.join(DEST, "clients.json"))
    if not WIN:
        for f in ("start.py", "start.sh", "tracker.py", "install.py", "install.sh", "statusline.py"):
            p = os.path.join(DEST, f)
            if os.path.exists(p):
                os.chmod(p, 0o755)
    return notes


def main(args):
    for stream in (sys.stdout, sys.stderr):  # paths with accents on a cp1252 Windows console must not crash
        try:
            stream.reconfigure(errors="replace")
        except AttributeError:
            pass
    if sys.version_info < (3, 8):
        sys.exit("Tokenometro needs Python 3.8 or later")
    try:
        if "--uninstall" in args:
            edit_settings("remove", True)
            stop_tracker()
            print(f'Hook removed and Tokenometro stopped. To delete everything, delete the folder "{DEST}"')
            return
        port_free = stop_tracker()
        notes = copy_files()
        msg = edit_settings("add", "--statusline" in args)
    except (OSError, ValueError) as e:
        sys.exit(f"Could not install: {e}\n(Is {SETTINGS} valid JSON?) The files are in {DEST}.")
    if msg == "statusline-taken":
        _, status = commands()
        notes.append("- You already have a statusLine and it was left untouched. To let the dashboard see plan usage, chain it:")
        notes.append(f"    {status} --tee | <your current command>")
    print("Reading your Claude Code history (the first run can take a while)...")
    subprocess.run([sys.executable, os.path.join(DEST, "tracker.py"), "ingest"], stdout=subprocess.DEVNULL)
    if os.environ.get("TOKENOMETRO_NO_START") != "1" and port_free:
        start.launch(DEST, PORT)
    uninstall = f"{'py' if WIN else 'python3'} {q(os.path.join(DEST, 'install.py'))} --uninstall"
    print(f"""
Done. Dashboard: http://127.0.0.1:{PORT}  (starts by itself when you open Claude Code).
- Clients and projects: {os.path.join(DEST, 'clients.json')}  - prices: {os.path.join(DEST, 'prices.json')}
- Backup of your previous settings.json: {SETTINGS}.bak-tokenometro-*
- Uninstall: {uninstall}""")
    for n in notes:
        print(n)


if __name__ == "__main__":
    main(sys.argv[1:])

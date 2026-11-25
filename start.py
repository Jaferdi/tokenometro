#!/usr/bin/env python3
"""SessionStart hook: starts Tokenometro in the background unless it is already listening.

  python3 start.py              -> run tracker.py from this folder
  python3 start.py --port 7880  -> on another port
  python3 start.py --restart    -> stop the running one first (only if it is ours) and start again
  python3 start.py --plugin     -> Claude Code plugin: copy the code to the data folder first, then run it from there

It never blocks or prints anything: whatever a SessionStart hook prints goes into the context and costs tokens.
Works on Linux, macOS, WSL and Windows.
"""
import filecmp
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CODE = ("tracker.py", "statusline.py", "start.py", "start.sh", "install.py", "install.sh",
        "clients.example.json", "README.md", "README.es.md", "LICENSE")


def data_dir():
    """Where an installed Tokenometro lives: the plugin cache changes on every update, this doesn't."""
    if os.name == "nt":
        return os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local"), "tokenometro")
    return os.path.join(os.path.expanduser("~"), ".local", "share", "tokenometro")


def port_from(args):
    if "--port" in args and args.index("--port") + 1 < len(args):
        return args[args.index("--port") + 1]
    return os.environ.get("TOKENOMETRO_PORT") or os.environ.get("CLAUDE_TOKENS_PORT") or "7878"


def listening(port):
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.5):
            return True
    except (OSError, ValueError):
        return False


def stop(d, port):
    """Stop the Tokenometro listening on port, proving it is ours with the token it left in d/.control.
    Returns "none" (nothing listening), "stopped", or "unknown" (something else, or a version without /api/quit)."""
    if not listening(port):
        return "none"
    try:
        with open(os.path.join(d, ".control"), encoding="utf-8") as f:
            c = json.load(f)
        if str(c.get("port")) != str(port):
            return "unknown"
        req = urllib.request.Request(f"http://127.0.0.1:{int(port)}/api/quit", headers={"X-Tokenometro-Token": c["token"]})
        urllib.request.urlopen(req, timeout=3).read()
    except (OSError, ValueError, KeyError, TypeError):
        return "unknown"
    for _ in range(50):
        if not listening(port):
            return "stopped"
        time.sleep(0.1)
    return "unknown"


def sync(src, dest):
    """Plugin mode: keep the code in dest up to date; prices and clients are the user's, only seeded once.
    Returns True if tracker.py changed."""
    os.makedirs(dest, exist_ok=True)
    changed = False
    for f in CODE:
        s, d = os.path.join(src, f), os.path.join(dest, f)
        if os.path.isfile(s) and not (os.path.isfile(d) and filecmp.cmp(s, d, shallow=False)):
            copy(s, d)
            changed = changed or f == "tracker.py"
    if not os.path.exists(os.path.join(dest, "prices.json")):
        copy(os.path.join(src, "prices.json"), os.path.join(dest, "prices.json"))
    if not any(os.path.exists(os.path.join(dest, f)) for f in ("clients.json", "clientes.json")):
        copy(os.path.join(src, "clients.example.json"), os.path.join(dest, "clients.json"))
    return changed


def copy(s, d):
    """Copy aside and swap, so a server starting at that moment never reads half a tracker.py."""
    tmp = f"{d}.tmp{os.getpid()}"
    shutil.copy2(s, tmp)
    os.replace(tmp, d)


def mark(d, via):
    """Remember how this copy was installed, so the update notice shows the right command."""
    p = os.path.join(d, "install.json")
    try:
        with open(p, encoding="utf-8") as f:
            if json.load(f).get("via") == via:
                return
    except (OSError, ValueError, AttributeError):
        pass
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"via": via}, f)
    except OSError:
        pass


def take_lock(d):
    """Two sessions opening at once: only the one that creates the lock starts it. A lock older than a minute
    is stale (whoever held it died); it is removed and taken again, so only one racer wins it."""
    lock = os.path.join(d, ".starting")
    for _ in range(2):
        try:
            os.mkdir(lock)
            return True
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock) <= 60:
                    return False
                os.rmdir(lock)
            except OSError:
                return False
        except OSError:
            return False
    return False


def release_lock(d):
    try:
        os.rmdir(os.path.join(d, ".starting"))
    except OSError:
        pass


def launch(d, port):
    log = os.path.join(d, "tracker.log")
    try:
        if os.path.getsize(log) > 1_048_576:  # keep the log small
            os.replace(log, log + ".1")
    except OSError:
        pass
    env = dict(os.environ, TOKENOMETRO_PORT=str(port))
    kw = {}
    exe = sys.executable
    if os.name == "nt":
        # no console window, and not killed when the Claude Code terminal closes
        kw["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED_PROCESS | NEW_PROCESS_GROUP | NO_WINDOW
        w = os.path.join(os.path.dirname(exe), "pythonw.exe")
        exe = w if os.path.exists(w) else exe
    else:
        kw["start_new_session"] = True
    with open(log, "ab") as out, open(os.devnull, "rb") as devnull:
        subprocess.Popen([exe, os.path.join(d, "tracker.py"), "serve"], cwd=d, env=env,
                         stdin=devnull, stdout=out, stderr=out, close_fds=True, **kw)


def main(args):
    port = port_from(args)
    d = data_dir() if "--plugin" in args else HERE
    os.makedirs(d, exist_ok=True)
    stopped = False
    # the lock covers the plugin copy too: two sessions opening at once must not both rewrite tracker.py
    locked = take_lock(d)
    try:
        if "--plugin" in args and locked:
            mark(d, "plugin")
        if "--plugin" in args and locked and sync(HERE, d):  # plugin updated: restart the old server
            stopped = stop(d, port) == "stopped"
        if "--restart" in args:
            stopped = stop(d, port) == "stopped" or stopped
        if listening(port) or not (locked or stopped):
            return
        launch(d, port)
        for _ in range(30):  # hold the lock until it listens, so a session opening now doesn't start a second one
            if listening(port):
                break
            time.sleep(0.1)
    finally:
        if locked:
            release_lock(d)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except Exception as e:  # a hook must never break the session; leave a trace for whoever looks at the log
        try:
            with open(os.path.join(HERE, "start-error.log"), "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {e!r}\n")
        except OSError:
            pass

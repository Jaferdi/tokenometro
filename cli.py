"""`tokenometro` command (pipx install git+https://github.com/Jaferdi/tokenometro).

The files of this package are only the installer's source: `tokenometro install` copies them to the data
folder and registers the hook there, exactly like `python3 install.py` from a clone.
"""
import os
import subprocess
import sys
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import start  # noqa: E402

USAGE = """usage: tokenometro <command> [options]

  install [--statusline] [--port N]   install or update, register the hook and start the dashboard
  uninstall                           remove the hook and our statusLine (keeps your data)
  open                                open the dashboard in the browser
  restart                             restart the dashboard
  report [YYYY-MM] [--by client] [--client NAME] [--cur EUR]   monthly CSV to stdout
  digest [YYYY-MM-DD]                 weekly Markdown summary to stdout
  ingest                              read new logs and print a short summary
  demo [--lang es] [--currency EUR]   try it with made-up data, without installing anything
"""


def run(script, args):
    return subprocess.call([sys.executable, script] + args)


def tracker():
    """The installed copy if there is one (it has your database), otherwise the one in this package."""
    installed = os.path.join(start.data_dir(), "tracker.py")
    return installed if os.path.exists(installed) else os.path.join(HERE, "tracker.py")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    cmd, args = (argv[0], argv[1:]) if argv else ("help", [])
    if cmd == "install":
        return run(os.path.join(HERE, "install.py"), args)
    if cmd == "uninstall":
        return run(os.path.join(HERE, "install.py"), ["--uninstall"] + args)
    if cmd in ("report", "digest", "ingest"):
        return run(tracker(), [cmd] + args)
    if cmd == "demo":
        return run(os.path.join(HERE, "demo.py"), args)
    if cmd == "restart":
        return run(os.path.join(start.data_dir(), "start.py"), ["--restart"] + args)
    if cmd == "open":
        url = f"http://127.0.0.1:{start.port_from(args)}"
        if not start.listening(start.port_from(args)):
            print(f"Tokenometro is not running on {url}. Start it with: tokenometro restart (or open Claude Code)")
            return 1
        webbrowser.open(url)
        print(url)
        return 0
    print(USAGE)
    return 0 if cmd in ("help", "-h", "--help") else 2


if __name__ == "__main__":
    sys.exit(main())

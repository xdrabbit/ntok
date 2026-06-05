"""ntok command-line entry point.

    ntok toggle     # start, or stop+transcribe+type (bind this to a hotkey)
    ntok start
    ntok stop
    ntok cancel
    ntok status
    ntok daemon     # run the foreground daemon (systemd uses this)
"""

from __future__ import annotations

import sys

CLIENT_COMMANDS = {"toggle", "start", "stop", "cancel", "status", "ping", "shutdown"}

USAGE = __doc__


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] in {"-h", "--help", "help"}:
        print(USAGE)
        return 0

    cmd = args[0]
    if cmd == "daemon":
        from .daemon import run
        return run()
    if cmd in CLIENT_COMMANDS:
        from .client import main as client_main
        return client_main(cmd)

    print(f"ntok: unknown command {cmd!r}\n", file=sys.stderr)
    print(USAGE, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

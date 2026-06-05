"""ntok command-line entry point.

Local (Phase 1) — transcribe + type on this machine:
    ntok toggle     # start, or stop+flush (bind this to a hotkey)
    ntok start | stop | cancel | status
    ntok daemon     # run the local daemon (systemd uses this)

Networked (Phase 2) — central GPU on blackbird, thin seats everywhere:
    ntok server         # run the transcription server (on blackbird)
    ntok client-daemon  # run the thin seat daemon (on each machine)
    ntok client toggle  # control the seat daemon (bind this to a hotkey)
    ntok client start | stop | cancel | status
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
    if cmd == "server":
        from .net.server import run
        return run()
    if cmd == "client-daemon":
        from .net.client_daemon import run
        return run()
    if cmd == "client":
        sub = args[1] if len(args) > 1 else ""
        if sub not in CLIENT_COMMANDS:
            print(f"ntok client: unknown command {sub!r}\n", file=sys.stderr)
            print(USAGE, file=sys.stderr)
            return 1
        from .client import main as client_main
        from .net.client_daemon import client_socket_path
        return client_main(sub, path=client_socket_path())
    if cmd in CLIENT_COMMANDS:
        from .client import main as client_main
        return client_main(cmd)

    print(f"ntok: unknown command {cmd!r}\n", file=sys.stderr)
    print(USAGE, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

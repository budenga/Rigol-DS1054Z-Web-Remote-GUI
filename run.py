#!/usr/bin/env python3.11
"""Entry point for the DS1054Z web remote GUI.

Usage:
    python3.11 run.py                    # auto-discover scope on the network
    python3.11 run.py 192.168.1.100      # connect to a specific IP
    python3.11 run.py discover           # just list scopes, don't start server
    python3.11 run.py --debug            # also write a DEBUG log file (see --log-dir)
"""

import argparse
import asyncio
import os
import sys

import uvicorn


def main():
    parser = argparse.ArgumentParser(
        description="Rigol DS1054Z Web Remote GUI"
    )
    parser.add_argument(
        "scope_ip", nargs="?", default=None,
        help="Oscilloscope IP address (auto-discover if omitted). "
             "Use 'discover' to just list scopes.",
    )
    parser.add_argument(
        "--port", type=int, default=8082,
        help="Web server port (default: 8082)",
    )
    parser.add_argument(
        "--scope-port", type=int, default=5555,
        help="Scope SCPI port (default: 5555)",
    )
    parser.add_argument(
        "--host", default="0.0.0.0",
        help="Web server bind address (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--fake", action="store_true",
        help="Run against a built-in fake scope (no hardware is touched)",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Write a DEBUG log file (full SCPI trace); see --log-dir",
    )
    parser.add_argument(
        "--log-dir", default=None, metavar="DIR",
        help="Directory for the --debug log file "
             "(default: ~/Library/Logs/Rigol_DS1054Z on macOS)",
    )
    parser.add_argument(
        "--no-browser", action="store_true",
        help="Do not open the browser automatically",
    )
    args = parser.parse_args()

    # --- Discover-only mode ---
    if args.scope_ip == "discover":
        _run_discover(args.scope_port)
        return

    # --- Auto-discover if no IP given ---
    scope_ip = args.scope_ip
    if args.fake:
        scope_ip = "fake"
    elif scope_ip is None:
        scope_ip = _auto_discover(args.scope_port)
        if scope_ip is None:
            sys.exit(1)

    # --- Start web server ---
    import server.app as app_module
    app_module.SCOPE_HOST = scope_ip
    app_module.SCOPE_PORT = args.scope_port
    app_module.USE_FAKE = args.fake
    app_module.DEBUG = args.debug
    app_module.LOG_DIR = args.log_dir
    if args.debug:
        from rigol import logsetup
        log_file = logsetup.setup_logging(True, args.log_dir)

    # Check if port is available
    port = args.port
    if _port_in_use(port):
        owner = _port_owner(port)
        if owner and _is_own_process(owner):
            # It's a previous instance of THIS project — safe to kill
            print(f"  Porta {port} ocupada por uma sessão anterior do DS1054Z. A encerrar...")
            _kill_own_port(port)
        else:
            # Something else is using the port — find a free one
            desc = f" ({owner})" if owner else ""
            print(f"  Porta {port} ocupada{desc}.")
            port = _find_free_port(port)
            print(f"  A usar porta {port} em vez disso.\n")

    print(f"\n  Rigol DS1054Z Web Remote")
    if args.fake:
        print("  Scope: FAKE (built-in simulator, no hardware)")
    else:
        print(f"  Scope: {scope_ip}:{args.scope_port}")
    if args.debug:
        print(f"  Debug log: {log_file}")
    print(f"  Open: http://localhost:{port}\n")

    # Open browser after server starts (2 s delay, in background thread)
    import threading
    import webbrowser

    def _open_browser():
        import time
        time.sleep(2)
        webbrowser.open(f"http://localhost:{port}")

    if not args.no_browser:
        threading.Thread(target=_open_browser, daemon=True).start()

    try:
        uvicorn.run(
            "server.app:app",
            host=args.host,
            port=port,
            reload=False,
            log_level="info",
        )
    except KeyboardInterrupt:
        print("\n  Encerrado.")


def _port_in_use(port: int) -> bool:
    """Check if a TCP port is currently in use."""
    import socket as _socket
    with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def _is_own_process(owner: str) -> bool:
    """Return True if the owner command looks like a previous run of this script."""
    parts = owner.split()
    return any(
        p in ("run.py", "Rigol_DS1054Z") or p.endswith("/run.py")
        for p in parts
    )


def _port_owner(port: int) -> str:
    """Get the command name of the process using a port (macOS/Linux)."""
    import subprocess
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True, text=True, timeout=3,
        )
        pids = result.stdout.strip().split()
        if pids:
            ps = subprocess.run(
                ["ps", "-p", pids[0], "-o", "command="],
                capture_output=True, text=True, timeout=3,
            )
            return ps.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return ""


def _kill_own_port(port: int) -> None:
    """Kill a previous run.py instance on the given port."""
    import subprocess
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True, text=True, timeout=3,
        )
        my_pid = str(os.getpid())
        for pid in result.stdout.strip().split():
            if pid and pid != my_pid:
                subprocess.run(["kill", pid], capture_output=True, timeout=3)
        import time
        time.sleep(0.5)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass


def _find_free_port(start: int) -> int:
    """Find the next free port starting from start+1."""
    import socket as _socket
    for port in range(start + 1, start + 100):
        with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start + 1


def _run_discover(port: int) -> None:
    """List all scopes on the network."""
    from rigol.discovery import discover_quick

    print("\n  Rigol DS1054Z — Network Discovery\n")
    results = asyncio.run(discover_quick(port=port))

    if not results:
        print("  No instruments found.\n")
        return

    print(f"\n  Found {len(results)} instrument(s):\n")
    for r in results:
        print(f"    {r['ip']:16s}  {r['model']:14s}  S/N: {r['serial']}")
        print(f"                      FW: {r['firmware']}")
        print()

    if len(results) == 1:
        print(f"  To connect: python3.11 run.py {results[0]['ip']}\n")


def _auto_discover(port: int) -> str | None:
    """Auto-discover a scope and return its IP, or None."""
    from rigol.discovery import find_first_scope

    print("\n  No IP specified — searching for scope on the network...\n")
    result = asyncio.run(find_first_scope(port=port))

    if result is None:
        print("  No scope found on the network.")
        print("  Specify the IP manually: python3.11 run.py <ip>\n")
        return None

    print(f"\n  Found: {result['model']} (S/N: {result['serial']})")
    print(f"  IP:    {result['ip']}\n")
    return result["ip"]


if __name__ == "__main__":
    main()

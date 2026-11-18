#!/usr/bin/env python3.11
"""Command-line interface for the Rigol DS1054Z oscilloscope.

Uses the same rigol/ package as the web server, driven synchronously
via asyncio.run().  Does NOT require FastAPI or uvicorn.

Usage examples:
    python3.11 ds1054z-cli.py 192.168.1.100 info
    python3.11 ds1054z-cli.py 192.168.1.100 screenshot --invert
    python3.11 ds1054z-cli.py 192.168.1.100 save-data --channels 1,3
    python3.11 ds1054z-cli.py 192.168.1.100 cmd "*IDN?"
    python3.11 ds1054z-cli.py 192.168.1.100 shell
    python3.11 ds1054z-cli.py 192.168.1.100 measure 1 freq
    python3.11 ds1054z-cli.py 192.168.1.100 raw-data --channels 1,4 --mdep 600000 --out capture.npz
"""

import argparse
import asyncio
import csv
import math
import os
import sys
from datetime import datetime

from rigol.transport import RawTCPTransport, TransportError
from rigol.scheduler import SCPIScheduler, Priority, CmdType
from rigol.scope import DS1054Z, CHANNELS, scpi_is_query
from rigol.waveform import raw_to_volts, time_values

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SI_PREFIXES = [
    (1e12,  "T"),
    (1e9,   "G"),
    (1e6,   "M"),
    (1e3,   "k"),
    (1,     ""),
    (1e-3,  "m"),
    (1e-6,  "u"),
    (1e-9,  "n"),
    (1e-12, "p"),
]


def format_si(value: float, unit: str = "", decimals: int = 3) -> str:
    """Format a float with an SI prefix (e.g. 1.234 kHz)."""
    if value is None or math.isnan(value) or math.isinf(value):
        return "---"
    if value == 0:
        return f"0 {unit}"
    abs_val = abs(value)
    for threshold, prefix in SI_PREFIXES:
        if abs_val >= threshold:
            scaled = value / threshold
            return f"{scaled:.{decimals}f} {prefix}{unit}"
    # Smaller than pico — just print as-is
    return f"{value:.{decimals}e} {unit}"


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


MEASURE_MAP = {
    "freq":    "FREQuency",
    "period":  "PERiod",
    "vpp":     "VPP",
    "vmax":    "VMAX",
    "vmin":    "VMIN",
    "vrms":    "VRMS",
    "vavg":    "VAVG",
    "rise":    "RTIMe",
    "fall":    "FTIMe",
    "pwidth":  "PWIDth",
    "nwidth":  "NWIDth",
    "duty":    "PDUTy",
}

MEASURE_UNITS = {
    "FREQuency": "Hz",
    "PERiod":    "s",
    "VPP":       "V",
    "VMAX":      "V",
    "VMIN":      "V",
    "VRMS":      "V",
    "VAVG":      "V",
    "RTIMe":     "s",
    "FTIMe":     "s",
    "PWIDth":    "s",
    "NWIDth":    "s",
    "PDUTy":     "%",
}

# ---------------------------------------------------------------------------
# Async scope context manager
# ---------------------------------------------------------------------------

class ScopeSession:
    """Async context manager that connects transport + scheduler + scope."""

    def __init__(self, host: str, port: int = 5555):
        self.transport = RawTCPTransport(host, port)
        self.scheduler = SCPIScheduler(self.transport)
        self.scope = DS1054Z(self.scheduler)

    async def __aenter__(self) -> "ScopeSession":
        await self.transport.connect()
        self.scheduler.start()
        return self

    async def __aexit__(self, *exc):
        await self.scheduler.stop()
        await self.transport.disconnect()


# ---------------------------------------------------------------------------
# Command implementations
# ---------------------------------------------------------------------------

async def cmd_info(session: ScopeSession) -> None:
    idn = await session.scope.get_idn()
    parts = idn.split(",")
    print()
    print("  Rigol DS1054Z — Instrument Info")
    print("  " + "=" * 38)
    if len(parts) >= 4:
        print(f"  Manufacturer : {parts[0].strip()}")
        print(f"  Model        : {parts[1].strip()}")
        print(f"  Serial       : {parts[2].strip()}")
        print(f"  Firmware     : {parts[3].strip()}")
    else:
        print(f"  IDN: {idn}")
    print()


async def cmd_screenshot(session: ScopeSession, args) -> None:
    scope = session.scope

    if args.invert:
        await scope.set_image_invert(True)
    if args.grayscale:
        await scope.set_image_color(False)

    print("  Capturing screenshot...", end="", flush=True)
    data = await scope.get_screenshot()

    # Restore settings
    if args.invert:
        await scope.set_image_invert(False)
    if args.grayscale:
        await scope.set_image_color(True)

    filename = args.filename or f"ds1054z_{timestamp()}.png"
    with open(filename, "wb") as f:
        f.write(data)
    print(f" done.")
    print(f"  Saved: {filename} ({len(data)} bytes)")


async def cmd_save_data(session: ScopeSession, args) -> None:
    scope = session.scope

    # Parse channels
    if args.channels:
        channels = [int(c.strip()) for c in args.channels.split(",")]
    else:
        # Auto-detect: only channels that are on
        channels = []
        for ch in CHANNELS:
            on = await scope.get_channel_display(ch)
            if on:
                channels.append(ch)
        if not channels:
            channels = [1]

    mode = args.mode.upper()

    print(f"  Mode: {mode}")
    print(f"  Channels: {', '.join(f'CH{c}' for c in channels)}")

    # Collect data for each channel
    channel_data: dict[int, list[float]] = {}
    time_axis: list[float] | None = None

    for ch in channels:
        print(f"  Reading CH{ch}...", end="", flush=True)

        # Configure waveform source
        await session.scheduler.submit(
            f":WAV:SOUR CHAN{ch}", Priority.WAVEFORM, CmdType.WRITE
        )
        await session.scheduler.submit(
            f":WAV:MODE {mode}", Priority.WAVEFORM, CmdType.WRITE
        )
        await session.scheduler.submit(
            ":WAV:FORM BYTE", Priority.WAVEFORM, CmdType.WRITE
        )

        # Read preamble
        preamble = await scope.get_waveform_preamble()

        # Read raw bytes
        raw = await session.scheduler.submit(
            ":WAV:DATA?", Priority.WAVEFORM, CmdType.QUERY_BINARY
        )

        volts = raw_to_volts(raw, preamble)
        channel_data[ch] = volts

        if time_axis is None:
            time_axis = time_values(preamble)

        print(f" {len(volts)} points")

    # Write CSV
    filename = args.filename or f"ds1054z_data_{timestamp()}.csv"

    with open(filename, "w", newline="") as f:
        writer = csv.writer(f)
        header = ["time_s"] + [f"ch{ch}_v" for ch in channels]
        writer.writerow(header)

        n_points = len(time_axis) if time_axis else 0
        for i in range(n_points):
            row = [f"{time_axis[i]:.10e}"]
            for ch in channels:
                v = channel_data[ch][i] if i < len(channel_data[ch]) else float("nan")
                row.append(f"{v:.6e}")
            writer.writerow(row)

    print(f"  Saved: {filename} ({n_points} points)")


async def cmd_raw_data(session: ScopeSession, args) -> None:
    """Read the scope's internal memory in RAW mode and save it as a compressed .npz."""
    from rigol import analysis
    from rigol.raw_reader import read_raw
    scope = session.scope

    channels = [int(c.strip()) for c in args.channels.split(",") if c.strip()]
    if len(channels) < 2:
        raise SystemExit("  raw-data needs at least two channels (voltage and current), e.g. --channels 1,4")
    v_ch = args.v_ch or channels[0]
    i_ch = args.i_ch or channels[1]
    if v_ch not in channels or i_ch not in channels or v_ch == i_ch:
        raise SystemExit(f"  --v-ch/--i-ch must be two different channels from --channels {channels}")
    await scope.get_full_state()

    if args.mdep:
        valid = scope.mdep_options()
        if args.mdep not in valid:
            raise SystemExit(f"  {args.mdep} is not a valid memory depth for "
                             f"{len(scope.enabled_channels())} enabled channel(s): {list(valid)}")
        if await scope.get_trigger_status() == "STOP":
            await scope.run()
        await scope.set_memory_depth(args.mdep)
        await asyncio.sleep(0.2)
        print(f"  Memory depth: {await scope.get_memory_depth()}")

    if args.single:
        print("  Armed (:SINGle), waiting for the trigger... (Ctrl-C to abort)")
        await scope.arm_single()
        await asyncio.sleep(0.6)
        while await scope.get_trigger_status() != "STOP":
            await asyncio.sleep(0.2)
    elif await scope.get_trigger_status() != "STOP":
        print("  Scope is running: stopping it to read its memory.")

    def progress(ch, done, total, eta):
        print(f"\r  CH{ch}: {done:>10,}/{total:,} pts  ETA {eta:5.1f} s", end="", flush=True)

    res = await read_raw(scope, channels, args.mdep, progress)
    print()
    cap = analysis.from_raw(res, v_ch, i_ch, meta={"idn": await scope.get_idn()})
    out = args.out or f"ds1054z_raw_{timestamp()}.npz"
    analysis.save_npz(cap, out)
    print(f"  {res.n:,} points x {len(channels)} channels in {res.seconds:.2f} s "
          f"({res.bytes_per_s / 1e6:.2f} MB/s)")
    print(f"  Saved: {out}  (V = CH{v_ch}, I = CH{i_ch}; open it in the Power analysis view)")


async def cmd_scpi(session: ScopeSession, args) -> None:
    command = args.command
    if scpi_is_query(command):
        resp = await session.scheduler.submit(command, Priority.CONTROL, CmdType.QUERY)
        print(resp)
    else:
        await session.scheduler.submit(command, Priority.CONTROL, CmdType.WRITE)
        print("  OK")


async def cmd_shell(session: ScopeSession) -> None:
    history_file = os.path.expanduser("~/.ds1054z_history")

    try:
        import readline
        try:
            readline.read_history_file(history_file)
        except FileNotFoundError:
            pass
        readline.set_history_length(500)
    except ImportError:
        readline = None

    print()
    print("  DS1054Z SCPI Shell")
    print("  Type SCPI commands. Use 'quit' or 'exit' to leave.")
    print()

    while True:
        try:
            line = input("SCPI> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not line:
            continue
        if line.lower() in ("quit", "exit"):
            break

        try:
            if scpi_is_query(line):
                resp = await session.scheduler.submit(
                    line, Priority.CONTROL, CmdType.QUERY
                )
                print(resp)
            else:
                await session.scheduler.submit(
                    line, Priority.CONTROL, CmdType.WRITE
                )
                print("  OK")
        except TransportError as exc:
            print(f"  Error: {exc}")

    if readline is not None:
        try:
            readline.write_history_file(history_file)
        except OSError:
            pass


async def cmd_run(session: ScopeSession) -> None:
    await session.scope.run()
    print("  Scope running.")


async def cmd_stop(session: ScopeSession) -> None:
    await session.scope.stop()
    print("  Scope stopped.")


async def cmd_single(session: ScopeSession) -> None:
    await session.scope.single()
    print("  Single trigger armed.")


async def cmd_measure(session: ScopeSession, args) -> None:
    ch = args.channel
    item_key = args.item.lower()

    if item_key not in MEASURE_MAP:
        valid = ", ".join(sorted(MEASURE_MAP.keys()))
        print(f"  Error: unknown measurement '{args.item}'")
        print(f"  Valid items: {valid}")
        sys.exit(1)

    scpi_item = MEASURE_MAP[item_key]
    unit = MEASURE_UNITS.get(scpi_item, "")

    value = await session.scope.get_measurement(ch, scpi_item)

    if value is None:
        print(f"  CH{ch} {item_key}: --- (no signal / not available)")
    elif unit == "%":
        print(f"  CH{ch} {item_key}: {value:.2f} %")
    else:
        print(f"  CH{ch} {item_key}: {format_si(value, unit)}")


async def cmd_settings(session: ScopeSession) -> None:
    scope = session.scope
    await scope.get_full_state()
    s = scope.state

    print()
    print("  Rigol DS1054Z — Current Settings")
    print("  " + "=" * 50)

    # Channels
    print()
    print("  CHANNELS")
    print("  " + "-" * 50)
    print(f"  {'CH':<5} {'Display':<9} {'V/div':<12} {'Offset':<12} {'Coupling':<10} {'Probe':<8}")
    print("  " + "-" * 50)
    for ch in CHANNELS:
        cs = s.channels[ch]
        disp = "ON" if cs.on else "OFF"
        if cs.on:
            vdiv = format_si(cs.scale, "V/div")
            offs = format_si(cs.offset, "V")
            print(f"  CH{ch}  {disp:<9} {vdiv:<12} {offs:<12} {cs.coupling:<10} {cs.probe}x")
        else:
            print(f"  CH{ch}  {disp}")

    # Timebase
    print()
    print("  TIMEBASE")
    print("  " + "-" * 50)
    print(f"  Scale  : {format_si(s.timebase_scale, 's/div')}")
    print(f"  Offset : {format_si(s.timebase_offset, 's')}")
    print(f"  Mode   : {s.timebase_mode}")

    # Trigger
    print()
    print("  TRIGGER")
    print("  " + "-" * 50)
    t = s.trigger
    print(f"  Mode   : {t.mode}")
    print(f"  Source : {t.source}")
    print(f"  Level  : {format_si(t.level, 'V')}")
    print(f"  Slope  : {t.slope}")
    print(f"  Sweep  : {t.sweep}")
    print(f"  Status : {t.status}")

    # Acquire
    print()
    print("  ACQUIRE")
    print("  " + "-" * 50)
    print(f"  Type        : {s.acquire_type}")
    print(f"  Sample Rate : {format_si(s.sample_rate, 'Sa/s')}")
    print(f"  Memory Depth: {s.memory_depth}")
    print()


def cmd_web(args) -> None:
    """Launch the web server via subprocess (avoids importing FastAPI here)."""
    import subprocess
    script_dir = os.path.dirname(os.path.abspath(__file__))
    run_py = os.path.join(script_dir, "run.py")

    cmd = [
        sys.executable, run_py,
        args.scope_ip,
        "--port", str(args.port),
    ]
    if getattr(args, "debug", False):
        cmd.append("--debug")
        if getattr(args, "log_dir", None):
            cmd += ["--log-dir", args.log_dir]

    print(f"  Launching web server on port {args.port}...")
    print(f"  Open: http://localhost:{args.port}")
    print()

    try:
        subprocess.run(cmd)
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------------------
# Async runner — connects, runs a command, disconnects
# ---------------------------------------------------------------------------

SCOPE_PORT = 5555


async def run_command(host: str, func, args=None):
    """Open a session, run *func*, close cleanly."""
    async with ScopeSession(host, SCOPE_PORT) as session:
        if args is not None:
            await func(session, args)
        else:
            await func(session)


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ds1054z-cli",
        description="Command-line interface for the Rigol DS1054Z oscilloscope",
    )
    parser.add_argument("scope_ip", nargs="?", default=None,
                        help="Oscilloscope IP address (not needed for 'discover')")
    parser.add_argument("--scope-port", type=int, default=5555,
                        help="Scope SCPI port (default 5555; 5556 is the fake scope of run.py --fake)")

    parser.add_argument("--debug", action="store_true",
                        help="Write a DEBUG log file (full SCPI trace); option goes before the IP")
    parser.add_argument("--log-dir", default=None, metavar="DIR",
                        help="Directory for the --debug log file")

    sub = parser.add_subparsers(dest="command", required=True)

    # discover (no IP needed)
    sub.add_parser("discover", help="Discover Rigol scopes on the network")

    # info
    sub.add_parser("info", help="Show instrument identification")

    # screenshot
    p = sub.add_parser("screenshot", help="Capture a PNG screenshot")
    p.add_argument("--invert",    action="store_true", help="Invert colours")
    p.add_argument("--grayscale", action="store_true", help="Grayscale output")
    p.add_argument("--filename",  type=str, default=None, help="Output filename")

    # save-data
    p = sub.add_parser("save-data", help="Export waveform data as CSV")
    p.add_argument("--mode",     type=str, default="NORM", choices=["NORM", "RAW"], help="Waveform mode (default: NORM)")
    p.add_argument("--channels", type=str, default=None, help="Channels to capture, e.g. 1,2,3")
    p.add_argument("--filename", type=str, default=None, help="Output filename")

    # raw-data
    p = sub.add_parser("raw-data", help="Read the internal memory (RAW) and save a .npz")
    p.add_argument("--channels", type=str, default="1,4", help="Channels to read, voltage first (default 1,4)")
    p.add_argument("--mdep", type=int, default=None, help="Memory depth to set first, e.g. 600000")
    p.add_argument("--out", type=str, default=None, help="Output .npz file")
    p.add_argument("--v-ch", type=int, default=None, help="Voltage channel (default: first of --channels)")
    p.add_argument("--i-ch", type=int, default=None, help="Current channel (default: second of --channels)")
    p.add_argument("--single", action="store_true", help="Arm a single trigger and wait for it first")

    # cmd
    p = sub.add_parser("cmd", help="Send a raw SCPI command")
    p.add_argument("scpi_command", metavar="command", help="SCPI command string")

    # shell
    sub.add_parser("shell", help="Interactive SCPI shell")

    # run / stop / single
    sub.add_parser("run",    help="Start acquisition (:RUN)")
    sub.add_parser("stop",   help="Stop acquisition (:STOP)")
    sub.add_parser("single", help="Arm single trigger (:SINGle)")

    # measure
    p = sub.add_parser("measure", help="Read a measurement value")
    p.add_argument("channel", type=int, choices=[1, 2, 3, 4], help="Channel number (1-4)")
    p.add_argument("item", type=str, help=f"Measurement: {', '.join(sorted(MEASURE_MAP.keys()))}")

    # settings
    sub.add_parser("settings", help="Display full scope settings")

    # web
    p = sub.add_parser("web", help="Launch the web GUI server")
    p.add_argument("--port", type=int, default=8080, help="Web server port (default: 8080)")

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _cmd_discover():
    """Discover scopes on the network (no scope IP needed)."""
    from rigol.discovery import discover_quick

    print("\n  Rigol DS1054Z — Network Discovery\n")
    results = asyncio.run(discover_quick())

    if not results:
        print("  No instruments found.\n")
        return

    print(f"\n  Found {len(results)} instrument(s):\n")
    for r in results:
        print(f"    {r['ip']:16s}  {r['model']:14s}  S/N: {r['serial']}")
        print(f"                      FW: {r['firmware']}")
        print()

    if len(results) == 1:
        print(f"  To connect: python3.11 ds1054z-cli.py {results[0]['ip']} info\n")


def main():
    global SCOPE_PORT
    parser = build_parser()
    args = parser.parse_args()
    SCOPE_PORT = args.scope_port

    if args.debug:
        from rigol import logsetup
        log_file = logsetup.setup_logging(True, args.log_dir, app="ds1054z-cli")
        print(f"  Debug log: {log_file}", file=sys.stderr)

    # Commands that don't need a scope IP
    if args.command == "discover":
        _cmd_discover()
        return

    if args.command == "web":
        cmd_web(args)
        return

    if not args.scope_ip:
        parser.error("scope_ip is required for this command")

    # Dispatch table
    dispatch = {
        "info":       (cmd_info,       False),
        "screenshot": (cmd_screenshot, True),
        "save-data":  (cmd_save_data,  True),
        "raw-data":   (cmd_raw_data,   True),
        "cmd":        (cmd_scpi,       True),
        "shell":      (cmd_shell,      False),
        "run":        (cmd_run,        False),
        "stop":       (cmd_stop,       False),
        "single":     (cmd_single,     False),
        "measure":    (cmd_measure,    True),
        "settings":   (cmd_settings,   False),
    }

    # Rename for 'cmd' subcommand — argparse stores it as scpi_command
    if args.command == "cmd":
        args.command_str = args.scpi_command
        # Re-map for the handler
        args_obj = argparse.Namespace(command=args.scpi_command)
        func, needs_args = dispatch["cmd"]
        try:
            asyncio.run(run_command(args.scope_ip, func, args_obj))
        except TransportError as exc:
            print(f"  Connection error: {exc}", file=sys.stderr)
            sys.exit(1)
        except KeyboardInterrupt:
            print("\n  Interrupted.")
        return

    func, needs_args = dispatch[args.command]
    try:
        if needs_args:
            asyncio.run(run_command(args.scope_ip, func, args))
        else:
            asyncio.run(run_command(args.scope_ip, func))
    except TransportError as exc:
        print(f"  Connection error: {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n  Interrupted.")


if __name__ == "__main__":
    main()

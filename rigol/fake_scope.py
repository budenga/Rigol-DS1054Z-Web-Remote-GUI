"""Fake DS1054Z for developing and testing without hardware (``run.py --fake``).

An asyncio TCP server that speaks enough raw-socket SCPI for the app:
state queries, ``:RUN/:STOP/:SING``, the ``:TRIG:STAT?`` sequence after
``:SING`` (WAIT -> TD -> STOP), ``:ACQ:MDEP`` validation per channel count,
``:WAV:*`` in NORM and RAW mode (RAW only in STOP, at most 250 000 points per
read, like the real scope), ``:MEAS:ITEM?`` and ``:DISP:DATA?``.

The signal is a 24 V rail (CH1) with a sag proportional to the load current
(CH4): 0.3 A idle, bursts of a few amps and narrow 6-12 A spikes, similar to
``tests/fixtures``. NORM data (1200 points) are the RAW signal decimated, so
NORM and RAW results agree.

Nothing here talks to real hardware.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import struct
import time
import zlib
from collections import deque

import numpy as np

from .scope import MDEP_VALID

log = logging.getLogger(__name__)

FAKE_PORT = 5556
MAX_RAW_POINTS = 250_000
SCREEN_POINTS = 1200



# ---------------------------------------------------------------------------
# Signal model (pure functions so tests can build the expected data)
# ---------------------------------------------------------------------------

def make_load_signal(seed: int, n: int, span: float) -> tuple[np.ndarray, np.ndarray]:
    """Physical (v, i) arrays, float32, for a capture of ``n`` points over ``span`` s."""
    rng = np.random.default_rng(seed)
    i = np.full(n, 0.3, dtype=np.float32)
    # bursts: a few amps for 4-10 % of the span
    for _ in range(int(rng.integers(2, 5))):
        start = float(rng.uniform(0.05, 0.85))
        length = float(rng.uniform(0.04, 0.10))
        level = float(rng.uniform(2.0, 4.0))
        a = int(start * n)
        b = min(n, a + max(1, int(length * n)))
        i[a:b] = level
    # narrow spikes 6-12 A, 0.25-0.7 % of the span
    for _ in range(int(rng.integers(3, 7))):
        start = float(rng.uniform(0.02, 0.95))
        width = float(rng.uniform(0.0025, 0.007))
        level = float(rng.uniform(6.0, 12.0))
        a = int(start * n)
        b = min(n, a + max(1, int(width * n)))
        i[a:b] = level
    i += rng.standard_normal(n, dtype=np.float32) * np.float32(0.02)
    v = np.float32(23.0) - np.float32(0.1) * i
    v += rng.standard_normal(n, dtype=np.float32) * np.float32(0.04)
    return v, i


def make_aux_signal(ch: int, n: int, span: float) -> np.ndarray:
    """Simple deterministic waveforms for CH2 (sine) and CH3 (square)."""
    t = np.arange(n, dtype=np.float64) / n * span
    if ch == 2:
        return (3.0 * np.sin(2 * np.pi * 20.0 / span * t)).astype(np.float32)
    return (np.where(np.sin(2 * np.pi * 8.0 / span * t) >= 0, 2.0, 0.0)).astype(np.float32)


def quantize(x: np.ndarray, yinc: float, yorig: int, yref: int) -> np.ndarray:
    """Physical value -> raw byte (0 and 255 mean clipped), inverse of the app's conversion."""
    q = np.rint(x / np.float32(yinc) + np.float32(yorig + yref))
    return np.clip(q, 0, 255).astype(np.uint8)


def _png(width: int = 160, height: int = 96) -> bytes:
    """A small valid PNG (dark grid) for ``:DISP:DATA?``."""
    rows = []
    for y in range(height):
        row = bytearray([0])
        for x in range(width):
            on = (x % 16 == 0) or (y % 16 == 0)
            row += bytes((60, 60, 60)) if on else bytes((8, 8, 8))
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


# ---------------------------------------------------------------------------
# SCPI node matching (short/long forms)
# ---------------------------------------------------------------------------

def _match(spec: str, token: str) -> bool:
    """``spec`` like ``WAVeform``: token must be the short form up to the long form."""
    short = "".join(c for c in spec if c.isupper())
    full = spec.upper()
    t = token.upper()
    return len(t) >= len(short) and full.startswith(t)


_NODE_RE = re.compile(r"^([A-Za-z]+)(\d*)$")

# (spec path, default) - plain settable/queryable values
_SETTINGS: list[tuple[str, str]] = [
    ("TIMebase:SCALe", "0.1"), ("TIMebase:OFFSet", "0"), ("TIMebase:MODE", "MAIN"),
    ("TRIGger:MODE", "EDGE"), ("TRIGger:COUPling", "DC"), ("TRIGger:SWEep", "AUTO"),
    ("TRIGger:EDGE:SOURce", "CHAN1"), ("TRIGger:EDGE:LEVel", "5"),
    ("TRIGger:EDGE:SLOPe", "POS"),
    ("ACQuire:TYPE", "NORM"),
    ("DISPlay:GRID", "FULL"), ("DISPlay:GBRightness", "50"), ("DISPlay:WBRightness", "50"),
    ("STORage:IMAGe:INVert", "0"), ("STORage:IMAGe:COLor", "1"), ("STORage:IMAGe:TYPE", "PNG"),
    ("WAVeform:SOURce", "CHAN1"), ("WAVeform:MODE", "NORM"), ("WAVeform:FORMat", "BYTE"),
    ("WAVeform:STARt", "1"), ("WAVeform:STOP", "1200"),
    ("CURSor:MODE", "OFF"),
]
_CHAN_SETTINGS = [
    ("DISPlay", None), ("SCALe", None), ("OFFSet", None), ("COUPling", "DC"),
    ("PROBe", "1"), ("BWLimit", "OFF"), ("INVert", "0"),
]
_CHAN_DEFAULTS = {
    1: {"DISP": "1", "SCAL": "1", "OFFS": "-22"},
    2: {"DISP": "0", "SCAL": "2", "OFFS": "0"},
    3: {"DISP": "0", "SCAL": "2", "OFFS": "0"},
    4: {"DISP": "1", "SCAL": "5", "OFFS": "-10"},
}


def _short(spec: str) -> str:
    return "".join(c for c in spec if c.isupper())


class FakeScope:
    """Asyncio SCPI server. Use ``await start()`` / ``await stop()``."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = FAKE_PORT,
        *,
        wait_range: tuple[float, float] = (0.3, 1.5),
        throughput: float | None = None,
        trailing: str = "nl",
        seed: int = 1,
    ):
        self.host = host
        self.port = port
        self.wait_range = wait_range
        self.throughput = throughput          # bytes/s for binary blocks, None = unlimited
        self.trailing = trailing              # "nl" | "nlnlz" | "random"
        self._rng = random.Random(seed)
        self._seed_counter = seed * 1000
        self._server: asyncio.AbstractServer | None = None
        self.commands: deque[str] = deque(maxlen=50_000)
        self.connections = 0

        self.vals: dict[str, str] = {}
        for spec, default in _SETTINGS:
            self.vals[self._key(spec)] = default
        for ch in (1, 2, 3, 4):
            for name, default in _CHAN_SETTINGS:
                key = f"CHAN{ch}:{_short(name)}"
                self.vals[key] = _CHAN_DEFAULTS[ch].get(_short(name), default)
        self.mdep: str = "600000"             # "AUTO" or a number
        self.mode = "RUN"                     # RUN | STOP | SING
        self._sing_t0 = 0.0
        self._wait_dur = 0.0
        self.cap_seed = self._next_seed()
        self.cap_count = 0
        self._cache: tuple | None = None      # (key, v, i)

    # -- helpers ----------------------------------------------------------

    @staticmethod
    def _key(spec: str) -> str:
        return ":".join(_short(n) for n in spec.split(":"))

    def _next_seed(self) -> int:
        self._seed_counter += 1
        return self._seed_counter

    def _on_channels(self) -> list[int]:
        return [ch for ch in (1, 2, 3, 4) if self.vals[f"CHAN{ch}:DISP"] in ("1", "ON")]

    def _group(self) -> int:
        return min(max(len(self._on_channels()), 1), 4)

    def _tdiv(self) -> float:
        return float(self.vals["TIM:SCAL"])

    def _span(self) -> float:
        return 12.0 * self._tdiv()

    def _mdep_value(self) -> int:
        if self.mdep == "AUTO":
            return MDEP_VALID[self._group()][1]
        return int(self.mdep)

    def n_eff(self) -> int:
        """Points actually acquired (memory depth limited by max sample rate)."""
        max_rate = 1e9 if self._group() == 1 else 5e8
        return max(SCREEN_POINTS, min(self._mdep_value(), int(max_rate * self._span())))

    def srate(self) -> float:
        return self.n_eff() / self._span()

    def status(self) -> str:
        if self.mode == "STOP":
            return "STOP"
        if self.mode == "RUN":
            return "AUTO"
        t = time.monotonic() - self._sing_t0
        if t < self._wait_dur:
            return "WAIT"
        if t < self._wait_dur + 0.15:
            return "TD"
        self.mode = "STOP"
        self.cap_seed = self._next_seed()
        self.cap_count += 1
        self.vals["TRIG:SWE"] = "SING"
        return "STOP"

    # -- signal -----------------------------------------------------------

    def _physical(self, ch: int, n: int, seed: int) -> np.ndarray:
        span = self._span()
        if ch in (1, 4):
            key = (seed, n, span)
            if self._cache is None or self._cache[0] != key:
                v, i = make_load_signal(seed, n, span)
                self._cache = (key, v, i)
            return self._cache[1] if ch == 1 else self._cache[2]
        return make_aux_signal(ch, n, span)

    def preamble(self, ch: int, raw: bool) -> tuple[float, float, int, float, int, int, int]:
        """(xinc, xorig, xref, yinc, yorig, yref, points) for the given mode."""
        scale = float(self.vals[f"CHAN{ch}:SCAL"])
        offset = float(self.vals[f"CHAN{ch}:OFFS"])
        yinc = scale / 25.0
        yorig = int(round(offset / yinc))
        yref = 125 if raw else 127           # deliberately different: never reuse NORM values
        n = self.n_eff() if raw else SCREEN_POINTS
        span = self._span()
        xinc = span / n
        xorig = -6.0 * self._tdiv() - float(self.vals["TIM:OFFS"])
        return xinc, xorig, 0, yinc, yorig, yref, n

    def expected_raw(self, ch: int, raw: bool = True, seed: int | None = None) -> np.ndarray:
        """The bytes the scope serves for ``ch`` in the current (stopped) capture."""
        seed = self.cap_seed if seed is None else seed
        n_full = self.n_eff()
        phys = self._physical(ch, n_full, seed)
        _, _, _, yinc, yorig, yref, _ = self.preamble(ch, raw)
        if raw:
            return quantize(phys, yinc, yorig, yref)
        idx = (np.arange(SCREEN_POINTS, dtype=np.int64) * n_full) // SCREEN_POINTS
        return quantize(phys[idx], yinc, yorig, yref)

    # -- protocol ---------------------------------------------------------

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._client, self.host, self.port)
        if self.port == 0:
            self.port = self._server.sockets[0].getsockname()[1]
        log.info("Fake DS1054Z listening on %s:%d", self.host, self.port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.connections += 1
        # Like the real DS1054Z (fw 00.04.04.SP4, verified on hardware): queries that
        # arrive in the same write form one program message, and their replies are
        # joined with ';' into ONE response with a single terminator, e.g.
        # ``:WAV:PRE?\n:WAV:DATA?\n`` -> ``<preamble>;#9...<block>\n``. We treat
        # "another complete line is already buffered" as "same write".
        pending: list[tuple[str, object]] = []
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                text = line.decode(errors="replace").strip()
                if text:
                    self.commands.append(text)
                    try:
                        reply = self.handle(text)
                    except Exception as exc:  # pragma: no cover - defensive
                        log.exception("fake scope failed on %r", text)
                        reply = f"ERROR: {exc}"
                    if reply is not None:
                        pending.append(("block", reply[1]) if isinstance(reply, tuple)
                                       else ("text", reply))
                if not pending or b"\n" in reader._buffer:   # more of the same write queued
                    continue
                await self._flush_replies(writer, pending)
                pending = []
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    async def _flush_replies(self, writer: asyncio.StreamWriter, pending) -> None:
        if len(pending) == 1:
            kind, val = pending[0]
            if kind == "block":
                await self._send_block(writer, val)
            else:
                writer.write((val + "\n").encode())
                await writer.drain()
            return
        parts = [b"#9%09d" % len(v) + v if k == "block" else v.encode() for k, v in pending]
        end = self._trailing() if pending[-1][0] == "block" else b"\n"
        writer.write(b";".join(parts) + end)
        await writer.drain()

    def _trailing(self) -> bytes:
        if self.trailing == "nlnlz":
            return b"\n\n\x00"
        if self.trailing == "random":
            return self._rng.choice([b"\n", b"\n\n\x00"])
        return b"\n"

    async def _send_block(self, writer: asyncio.StreamWriter, data: bytes) -> None:
        head = b"#9%09d" % len(data)
        if self.throughput:
            writer.write(head)
            step = 65536
            for k in range(0, len(data), step):
                part = data[k:k + step]
                writer.write(part)
                await writer.drain()
                await asyncio.sleep(len(part) / self.throughput)
            writer.write(self._trailing())
        else:
            writer.write(head + data + self._trailing())
        await writer.drain()

    # -- command handling -------------------------------------------------

    def handle(self, text: str):
        header, _, arg = text.partition(" ")
        arg = arg.strip()
        if header.upper() == "*IDN?":
            return "RIGOL TECHNOLOGIES,DS1054Z,DS1ZA000000000,00.04.04.SP3 (FAKE)"
        if header.startswith("*"):
            return "0" if header.endswith("?") else None
        h = header.lstrip(":")
        query = h.endswith("?")
        if query:
            h = h[:-1]
        nodes = h.split(":")
        key = self._canon(nodes)

        # ---- action commands
        if key == "RUN":
            self.mode = "RUN"
            return None
        if key == "STOP":
            if self.mode != "STOP":
                self.mode = "STOP"
                self.cap_seed = self._next_seed()
                self.cap_count += 1
            return None
        if key == "SING":
            self.mode = "SING"
            self._sing_t0 = time.monotonic()
            self._wait_dur = self._rng.uniform(*self.wait_range)
            return None
        if key in ("TFOR", "AUT"):
            return None
        if key == "TRIG:STAT" and query:
            return self.status()
        if key == "ACQ:MDEP":
            if query:
                return self.mdep
            self._set_mdep(arg)
            return None
        if key == "ACQ:SRAT" and query:
            self.status()
            return f"{self.srate():.6e}"
        if key == "WAV:PRE" and query:
            self.status()
            raw = self.vals["WAV:MODE"].upper().startswith("RAW")
            ch = self._wav_channel()
            xinc, xorig, xref, yinc, yorig, yref, n = self.preamble(ch, raw)
            typ = 2 if raw else 0
            return (f"0,{typ},{n},1,{xinc:.9e},{xorig:.9e},{xref},"
                    f"{yinc:.9e},{yorig},{yref}")
        if key == "WAV:DATA" and query:
            return self._wav_data()
        if key == "MEAS:ITEM" and query:
            return self._measure(arg)
        if key == "DISP:DATA" and query:
            return ("block", _png())

        if key in self.vals:
            if query:
                return self.vals[key]
            if key.endswith(":DISP"):
                arg = "1" if arg.upper() in ("1", "ON") else "0"
            if key.endswith(":INV"):
                arg = "1" if arg.upper() in ("1", "ON") else "0"
            self.vals[key] = arg
            return None
        if query:
            return "0"
        return None

    @staticmethod
    def _canon(nodes: list[str]) -> str:
        """Map SCPI nodes to a canonical key such as ``CHAN1:DISP`` or ``TRIG:EDGE:SOUR``."""
        longnames = {
            "TIMebase": "TIM", "TRIGger": "TRIG", "ACQuire": "ACQ", "DISPlay": "DISP",
            "STORage": "STOR", "WAVeform": "WAV", "MEASure": "MEAS", "CURSor": "CURS",
            "CHANnel": "CHAN", "DECoder": "DEC",
        }
        children = {
            "TIM": ["SCALe", "OFFSet", "MODE"],
            "TRIG": ["MODE", "COUPling", "SWEep", "EDGE", "STATus"],
            "TRIG:EDGE": ["SOURce", "LEVel", "SLOPe"],
            "ACQ": ["TYPE", "MDEPth", "SRATe"],
            "DISP": ["GRID", "GBRightness", "WBRightness", "DATA"],
            "STOR": ["IMAGe"],
            "STOR:IMAG": ["INVert", "COLor", "TYPE"],
            "WAV": ["SOURce", "MODE", "FORMat", "STARt", "STOP", "PREamble", "DATA"],
            "MEAS": ["ITEM"],
            "CURS": ["MODE"],
            "CHAN": ["DISPlay", "SCALe", "OFFSet", "COUPling", "PROBe", "BWLimit", "INVert"],
        }
        roots = list(longnames) + ["RUN", "STOP", "SINGle", "TFORce", "AUToscale"]
        m = _NODE_RE.match(nodes[0])
        if not m:
            return "?"
        word, digits = m.groups()
        base = None
        for r in roots:
            if _match(r, word):
                base = longnames.get(r, _short(r))
                break
        if base is None:
            return "?"
        if base == "CHAN" and not digits:
            return "?"
        path = [base + (digits if base in ("CHAN", "DEC") else "")]
        ctx = base
        for tok in nodes[1:]:
            opts = children.get(ctx, [])
            hit = next((o for o in opts if _match(o, tok)), None)
            if hit is None:
                return "?"
            short = _short(hit)
            path.append(short)
            ctx = f"{ctx}:{short}"
        return ":".join(path)

    def _set_mdep(self, arg: str) -> None:
        a = arg.strip().upper()
        if a == "AUTO":
            self.mdep = "AUTO"
            return
        try:
            n = int(float(a))
        except ValueError:
            return
        if n in MDEP_VALID[self._group()]:
            self.mdep = str(n)

    def _wav_channel(self) -> int:
        m = re.search(r"(\d)", self.vals["WAV:SOUR"])
        return int(m.group(1)) if m else 1

    def _wav_data(self):
        st = self.status()
        raw = self.vals["WAV:MODE"].upper().startswith("RAW")
        ch = self._wav_channel()
        if raw:
            if st != "STOP":
                return "ERROR: RAW mode requires the scope to be stopped"
            n = self.n_eff()
            a = int(float(self.vals["WAV:STAR"]))
            b = int(float(self.vals["WAV:STOP"]))
            if a < 1 or b > n or b < a:
                return "ERROR: invalid start/stop"
            if b - a + 1 > MAX_RAW_POINTS:
                return "ERROR: more than 250000 points requested"
            phys = self._physical(ch, n, self.cap_seed)
            _, _, _, yinc, yorig, yref, _ = self.preamble(ch, True)
            return ("block", quantize(phys[a - 1:b], yinc, yorig, yref).tobytes())
        # NORM: 1200 screen points
        if st == "STOP":
            return ("block", self.expected_raw(ch, raw=False).tobytes())
        # running: every read is a different acquisition (channels not coherent)
        seed = self._next_seed()
        phys = self._physical(ch, SCREEN_POINTS, seed)
        _, _, _, yinc, yorig, yref, _ = self.preamble(ch, False)
        return ("block", quantize(phys, yinc, yorig, yref).tobytes())

    def _screen_phys(self, src: str) -> np.ndarray | None:
        n = self.n_eff()
        idx = (np.arange(SCREEN_POINTS, dtype=np.int64) * n) // SCREEN_POINTS
        if src.upper().startswith("MATH"):
            return self._physical(1, n, self.cap_seed)[idx] * self._physical(4, n, self.cap_seed)[idx]
        m = re.search(r"(\d)", src)
        if not m:
            return None
        return self._physical(int(m.group(1)), n, self.cap_seed)[idx]

    def _measure(self, arg: str) -> str:
        parts = [p.strip() for p in arg.split(",")]
        na = "9.9E37"
        if len(parts) < 2:
            return na
        item = parts[0].upper()
        x = self._screen_phys(parts[1])
        if x is None:
            return na
        x = x.astype(np.float64)
        dt = self._span() / SCREEN_POINTS
        if item.startswith("VMAX"):
            val = x.max()
        elif item.startswith("VMIN"):
            val = x.min()
        elif item.startswith("VPP") or item.startswith("VAMP"):
            val = x.max() - x.min()
        elif item.startswith("VAVG"):
            val = x.mean()
        elif item.startswith("VRMS"):
            val = float(np.sqrt(np.mean(x * x)))
        elif item.startswith("VTOP"):
            val = np.percentile(x, 90)
        elif item.startswith("VBAS"):
            val = np.percentile(x, 10)
        elif item.startswith("MARE"):
            val = x.sum() * dt
        elif item.startswith("FREQ") and parts[1].upper().endswith("2"):
            val = 20.0 / self._span()
        elif item.startswith("PER") and parts[1].upper().endswith("2"):
            val = self._span() / 20.0
        else:
            return na
        return f"{val:.6e}"

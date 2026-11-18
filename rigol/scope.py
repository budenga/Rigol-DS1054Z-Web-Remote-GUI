"""High-level async API for the Rigol DS1054Z oscilloscope."""

import asyncio
import logging
from dataclasses import dataclass, field

from .scheduler import SCPIScheduler, Priority, CmdType
from .waveform import WaveformPreamble, parse_preamble

log = logging.getLogger(__name__)

CHANNELS = (1, 2, 3, 4)
MEASUREMENT_ITEMS = (
    "FREQuency", "PERiod", "VPP", "VMAX", "VMIN", "VRMS", "VAVG",
    "RTIMe", "FTIMe", "PWIDth", "NWIDth", "PDUTy",
)
SENTINEL = 9.9e37  # scope's "not available" value


def scpi_is_query(cmd: str) -> bool:
    """True if any ';'-separated segment's *header* ends in '?' (``:MEAS:ITEM? VAVG,CHAN1`` is a query)."""
    return any(seg.split(None, 1)[0].endswith("?") for seg in cmd.split(";") if seg.strip())

# Valid :ACQ:MDEP values (analog channels) by number of enabled channels
# (Rigol MSO1000Z/DS1000Z programming guide). 1.2 Mpts is NOT valid with 2 channels.
MDEP_VALID = {
    1: (12_000, 120_000, 1_200_000, 12_000_000, 24_000_000),
    2: (6_000, 60_000, 600_000, 6_000_000, 12_000_000),
    3: (3_000, 30_000, 300_000, 3_000_000, 6_000_000),
    4: (3_000, 30_000, 300_000, 3_000_000, 6_000_000),
}


@dataclass
class ChannelState:
    on: bool = False
    scale: float = 1.0
    offset: float = 0.0
    coupling: str = "DC"
    probe: float = 1.0
    bwlimit: str = "OFF"
    invert: bool = False


@dataclass
class TriggerState:
    mode: str = "EDGE"
    status: str = "STOP"
    source: str = "CHAN1"
    level: float = 0.0
    slope: str = "POS"
    coupling: str = "DC"
    sweep: str = "AUTO"


@dataclass
class ScopeState:
    idn: str = ""
    channels: dict[int, ChannelState] = field(default_factory=lambda: {i: ChannelState() for i in CHANNELS})
    timebase_scale: float = 1e-3
    timebase_offset: float = 0.0
    timebase_mode: str = "MAIN"
    trigger: TriggerState = field(default_factory=TriggerState)
    sample_rate: float = 0.0
    memory_depth: str = "AUTO"
    acquire_type: str = "NORM"
    running: bool = False


class DS1054Z:
    def __init__(self, scheduler: SCPIScheduler):
        self._sched = scheduler
        self.state = ScopeState()

    # --- Helpers ---

    async def _q(self, cmd: str, prio: Priority = Priority.STATE) -> str:
        return await self._sched.submit(cmd, prio, CmdType.QUERY)

    async def _w(self, cmd: str, prio: Priority = Priority.CONTROL) -> None:
        await self._sched.submit(cmd, prio, CmdType.WRITE)

    async def _qb(self, cmd: str, prio: Priority = Priority.WAVEFORM) -> bytes:
        return await self._sched.submit(cmd, prio, CmdType.QUERY_BINARY)

    async def _qf(self, cmd: str, prio: Priority = Priority.STATE, default: float = 0.0) -> float:
        """Query a command and parse as float. Returns default on parse error.

        The scope sometimes returns strings like 'measure error!', '****', or
        stale text responses instead of a number. This helper handles those.
        """
        resp = await self._q(cmd, prio)
        try:
            v = float(resp)
            if v >= SENTINEL:
                return default
            return v
        except (ValueError, TypeError):
            log.debug("Non-numeric response for '%s': %r", cmd, resp)
            return default

    # --- Identification ---

    async def get_idn(self) -> str:
        self.state.idn = await self._q("*IDN?")
        return self.state.idn

    # --- Acquisition control ---

    async def run(self, sweep: str | None = None) -> None:
        # After :SING the sweep is SINGle and RUN would not free-run: put it back first,
        # to the user's previous sweep when known (else AUTO).
        # (the cached sweep is only refreshed every 5 s, so an explicit restore is always written)
        if sweep or self.state.trigger.sweep == "SING" or self.state.trigger.status == "WAIT":
            target = sweep if sweep and sweep != "SING" else "AUTO"
            await self._w(f":TRIG:SWE {target}")
            self.state.trigger.sweep = target
            await asyncio.sleep(0.05)
        await self._w(":RUN")
        await asyncio.sleep(0.1)
        await self.get_trigger_status()
        log.info("RUN → status: %s", self.state.trigger.status)

    async def stop(self) -> None:
        await self._w(":STOP")
        await asyncio.sleep(0.1)
        await self.get_trigger_status()
        log.info("STOP → status: %s", self.state.trigger.status)

    async def single(self) -> None:
        await self._w(":SINGle")
        await asyncio.sleep(0.1)
        await self.get_trigger_status()

    async def arm_single(self) -> None:
        """Send ``:SINGle`` only (the acquisition manager polls the status itself)."""
        await self._w(":SING")
        self.state.trigger.sweep = "SING"        # the scope switches its sweep to SINGle

    async def tforce(self) -> None:
        await self._w(":TFORce")

    async def autoscale(self) -> None:
        await self._w(":AUToscale")
        await asyncio.sleep(1.0)  # autoscale takes time
        await self.get_full_state()

    # --- Waveform ---

    async def get_waveform(self, channel: int) -> tuple[bytes, WaveformPreamble]:
        """Atomic NORM read of the 1200 screen points plus their preamble."""
        payload, pre = await self._sched.submit(
            str(channel), Priority.WAVEFORM, CmdType.QUERY_WAVEFORM
        )
        return payload, parse_preamble(pre)

    async def get_waveform_bytes(self, channel: int) -> bytes:
        return (await self.get_waveform(channel))[0]

    async def get_waveform_preamble(self) -> WaveformPreamble:
        resp = await self._q(":WAV:PRE?", Priority.WAVEFORM)
        return parse_preamble(resp)

    async def get_preamble_raw(self, channel: int) -> WaveformPreamble:
        """Preamble of ``channel`` read *after* selecting RAW mode (never reuse NORM values)."""
        resp = await self._sched.submit(
            str(channel), Priority.WAVEFORM, CmdType.PREAMBLE_RAW, args=(channel,)
        )
        return parse_preamble(resp)

    async def get_raw_chunk(self, channel: int, start: int, stop: int) -> bytes:
        """RAW memory points start..stop (1-based, inclusive); at most 250 000."""
        return await self._sched.submit(
            f"{channel}:{start}:{stop}", Priority.WAVEFORM,
            CmdType.WAVEFORM_RAW_CHUNK, args=(channel, start, stop),
        )

    # --- Channel settings ---

    async def get_channel_display(self, ch: int) -> bool:
        resp = await self._q(f":CHAN{ch}:DISP?")
        on = resp in ("1", "ON")
        self.state.channels[ch].on = on
        return on

    async def set_channel_display(self, ch: int, on: bool) -> None:
        await self._w(f":CHAN{ch}:DISP {'ON' if on else 'OFF'}")
        self.state.channels[ch].on = on

    async def get_channel_scale(self, ch: int) -> float:
        v = await self._qf(f":CHAN{ch}:SCAL?", default=self.state.channels[ch].scale)
        self.state.channels[ch].scale = v
        return v

    async def set_channel_scale(self, ch: int, val: float) -> None:
        await self._w(f":CHAN{ch}:SCAL {val}")
        self.state.channels[ch].scale = val

    async def get_channel_offset(self, ch: int) -> float:
        v = await self._qf(f":CHAN{ch}:OFFS?", default=self.state.channels[ch].offset)
        self.state.channels[ch].offset = v
        return v

    async def set_channel_offset(self, ch: int, val: float) -> None:
        await self._w(f":CHAN{ch}:OFFS {val}")
        self.state.channels[ch].offset = val

    async def get_channel_coupling(self, ch: int) -> str:
        resp = await self._q(f":CHAN{ch}:COUP?")
        self.state.channels[ch].coupling = resp
        return resp

    async def set_channel_coupling(self, ch: int, mode: str) -> None:
        await self._w(f":CHAN{ch}:COUP {mode}")
        self.state.channels[ch].coupling = mode

    async def get_channel_probe(self, ch: int) -> float:
        v = await self._qf(f":CHAN{ch}:PROB?", default=self.state.channels[ch].probe)
        self.state.channels[ch].probe = v
        return v

    async def set_channel_probe(self, ch: int, ratio: float) -> None:
        await self._w(f":CHAN{ch}:PROB {ratio}")
        self.state.channels[ch].probe = ratio

    async def get_channel_bwlimit(self, ch: int) -> str:
        resp = await self._q(f":CHAN{ch}:BWL?")
        self.state.channels[ch].bwlimit = resp
        return resp

    async def set_channel_bwlimit(self, ch: int, limit: str) -> None:
        await self._w(f":CHAN{ch}:BWL {limit}")
        self.state.channels[ch].bwlimit = limit

    async def get_channel_invert(self, ch: int) -> bool:
        resp = await self._q(f":CHAN{ch}:INV?")
        inv = resp in ("1", "ON")
        self.state.channels[ch].invert = inv
        return inv

    async def set_channel_invert(self, ch: int, on: bool) -> None:
        await self._w(f":CHAN{ch}:INV {'ON' if on else 'OFF'}")
        self.state.channels[ch].invert = on

    # --- Timebase ---

    async def get_timebase_scale(self) -> float:
        v = await self._qf(":TIM:SCAL?", default=self.state.timebase_scale)
        self.state.timebase_scale = v
        return v

    async def set_timebase_scale(self, val: float) -> None:
        await self._w(f":TIM:SCAL {val}")
        self.state.timebase_scale = val

    async def get_timebase_offset(self) -> float:
        v = await self._qf(":TIM:OFFS?", default=self.state.timebase_offset)
        self.state.timebase_offset = v
        return v

    async def set_timebase_offset(self, val: float) -> None:
        await self._w(f":TIM:OFFS {val}")
        self.state.timebase_offset = val

    async def get_timebase_mode(self) -> str:
        resp = await self._q(":TIM:MODE?")
        self.state.timebase_mode = resp
        return resp

    # --- Trigger ---

    async def get_trigger_status(self) -> str:
        resp = await self._q(":TRIG:STAT?")
        self.state.trigger.status = resp
        self.state.running = resp in ("TD", "WAIT", "RUN", "AUTO")
        return resp

    async def get_trigger_mode(self) -> str:
        resp = await self._q(":TRIG:MODE?")
        self.state.trigger.mode = resp
        return resp

    async def get_trigger_source(self) -> str:
        mode = self.state.trigger.mode
        resp = await self._q(f":TRIG:{mode}:SOUR?")
        self.state.trigger.source = resp
        return resp

    async def set_trigger_source(self, src: str) -> None:
        mode = self.state.trigger.mode
        await self._w(f":TRIG:{mode}:SOUR {src}")
        self.state.trigger.source = src

    async def get_trigger_level(self) -> float:
        mode = self.state.trigger.mode
        v = await self._qf(f":TRIG:{mode}:LEV?", default=self.state.trigger.level)
        self.state.trigger.level = v
        return v

    async def set_trigger_level(self, val: float) -> None:
        mode = self.state.trigger.mode
        await self._w(f":TRIG:{mode}:LEV {val}")
        self.state.trigger.level = val

    async def get_trigger_slope(self) -> str:
        resp = await self._q(":TRIG:EDGE:SLOP?")
        self.state.trigger.slope = resp
        return resp

    async def set_trigger_slope(self, slope: str) -> None:
        await self._w(f":TRIG:EDGE:SLOP {slope}")
        self.state.trigger.slope = slope

    async def get_trigger_coupling(self) -> str:
        resp = await self._q(":TRIG:COUP?")
        self.state.trigger.coupling = resp
        return resp

    async def get_trigger_sweep(self) -> str:
        resp = await self._q(":TRIG:SWE?")
        self.state.trigger.sweep = resp
        return resp

    async def set_trigger_sweep(self, sweep: str) -> None:
        await self._w(f":TRIG:SWE {sweep}")
        self.state.trigger.sweep = sweep

    # --- Measurements ---

    async def get_measurement(self, ch: int, item: str) -> float | None:
        try:
            resp = await self._q(
                f":MEAS:ITEM? {item},CHAN{ch}", Priority.MEASURE
            )
            v = float(resp)
            if v >= SENTINEL:
                return None
            return v
        except (ValueError, TypeError):
            return None

    async def get_measurement_spec(self, spec: str) -> float | None:
        """Query ``:MEAS:ITEM? <item>,<src>[,<src2>]`` for a spec such as ``"VAVG,CHAN1"``."""
        try:
            resp = await self._q(f":MEAS:ITEM? {spec}", Priority.MEASURE)
            v = float(resp)
        except (ValueError, TypeError):
            return None
        if v != v or abs(v) >= SENTINEL or v in (float("inf"), float("-inf")):
            return None
        return v

    async def get_measurements(self, ch: int) -> dict[str, float | None]:
        results = {}
        for item in MEASUREMENT_ITEMS:
            results[item] = await self.get_measurement(ch, item)
        return results

    # --- Acquire ---

    async def get_sample_rate(self) -> float:
        v = await self._qf(":ACQ:SRAT?", default=self.state.sample_rate)
        self.state.sample_rate = v
        return v

    async def get_memory_depth(self) -> str:
        resp = await self._q(":ACQ:MDEP?")
        self.state.memory_depth = resp
        return resp

    def enabled_channels(self) -> list[int]:
        return [ch for ch in CHANNELS if self.state.channels[ch].on]

    def mdep_options(self) -> tuple[int, ...]:
        """Memory depths valid for the number of enabled analog channels."""
        return MDEP_VALID[min(max(len(self.enabled_channels()), 1), 4)]

    async def set_memory_depth(self, points: int | str) -> None:
        await self._w(f":ACQ:MDEP {points}")

    async def get_acquire_type(self) -> str:
        resp = await self._q(":ACQ:TYPE?")
        self.state.acquire_type = resp
        return resp

    async def set_acquire_type(self, typ: str) -> None:
        await self._w(f":ACQ:TYPE {typ}")
        self.state.acquire_type = typ

    # --- Screenshot ---

    async def get_screenshot(self) -> bytes:
        return await self._qb(":DISP:DATA? ON,OFF,PNG", Priority.SCREENSHOT)

    async def set_image_invert(self, on: bool) -> None:
        await self._w(f":STOR:IMAG:INV {'ON' if on else 'OFF'}")

    async def set_image_color(self, on: bool) -> None:
        await self._w(f":STOR:IMAG:COL {'ON' if on else 'OFF'}")

    # --- Display ---

    async def get_display_grid(self) -> str:
        return await self._q(":DISP:GRID?")

    async def get_display_brightness(self) -> int:
        return int(await self._q(":DISP:GBR?"))

    async def get_waveform_brightness(self) -> int:
        return int(await self._q(":DISP:WBR?"))

    # --- Decoder ---

    async def get_decoder_mode(self, n: int) -> str:
        return await self._q(f":DEC{n}:MODE?")

    async def set_decoder_mode(self, n: int, mode: str) -> None:
        await self._w(f":DEC{n}:MODE {mode}")

    async def get_decoder_display(self, n: int) -> bool:
        resp = await self._q(f":DEC{n}:DISP?")
        return resp in ("1", "ON")

    async def set_decoder_display(self, n: int, on: bool) -> None:
        await self._w(f":DEC{n}:DISP {'ON' if on else 'OFF'}")

    async def get_decoder_format(self, n: int) -> str:
        return await self._q(f":DEC{n}:FORM?")

    async def set_decoder_format(self, n: int, fmt: str) -> None:
        await self._w(f":DEC{n}:FORM {fmt}")

    # --- Cursors ---

    async def get_cursor_mode(self) -> str:
        return await self._q(":CURS:MODE?")

    async def set_cursor_mode(self, mode: str) -> None:
        await self._w(f":CURS:MODE {mode}")

    async def set_cursor_manual_type(self, typ: str) -> None:
        await self._w(f":CURS:MAN:TYPE {typ}")

    async def set_cursor_ax(self, x: int) -> None:
        await self._w(f":CURS:MAN:AX {x}")

    async def set_cursor_bx(self, x: int) -> None:
        await self._w(f":CURS:MAN:BX {x}")

    async def set_cursor_ay(self, y: int) -> None:
        await self._w(f":CURS:MAN:AY {y}")

    async def set_cursor_by(self, y: int) -> None:
        await self._w(f":CURS:MAN:BY {y}")

    async def get_cursor_xdelta(self) -> float:
        return float(await self._q(":CURS:MAN:XDEL?"))

    async def get_cursor_ydelta(self) -> float:
        return float(await self._q(":CURS:MAN:YDEL?"))

    # --- Full state snapshot ---

    async def get_full_state(self) -> ScopeState:
        """Read the complete scope state. Used for initial sync and periodic polling."""
        await self.get_trigger_status()
        await self.get_trigger_mode()
        await self.get_timebase_scale()
        await self.get_timebase_offset()
        await self.get_sample_rate()
        await self.get_memory_depth()
        await self.get_acquire_type()

        for ch in CHANNELS:
            await self.get_channel_display(ch)
            if self.state.channels[ch].on:
                await self.get_channel_scale(ch)
                await self.get_channel_offset(ch)
                await self.get_channel_coupling(ch)
                await self.get_channel_probe(ch)

        try:
            await self.get_trigger_source()
            await self.get_trigger_level()
            await self.get_trigger_slope()
            await self.get_trigger_sweep()
        except Exception:
            pass  # some trigger modes don't support all queries

        return self.state

    def state_to_dict(self) -> dict:
        """Serialize the current state to a JSON-friendly dict."""
        s = self.state
        d: dict = {
            "running": s.running,
            "timebase_scale": s.timebase_scale,
            "timebase_offset": s.timebase_offset,
            "timebase_mode": s.timebase_mode,
            "sample_rate": s.sample_rate,
            "memory_depth": s.memory_depth,
            "acquire_type": s.acquire_type,
            "trigger": {
                "mode": s.trigger.mode,
                "status": s.trigger.status,
                "source": s.trigger.source,
                "level": s.trigger.level,
                "slope": s.trigger.slope,
                "coupling": s.trigger.coupling,
                "sweep": s.trigger.sweep,
            },
        }
        for ch in CHANNELS:
            cs = s.channels[ch]
            d[f"ch{ch}"] = {
                "on": cs.on,
                "scale": cs.scale,
                "offset": cs.offset,
                "coupling": cs.coupling,
                "probe": cs.probe,
                "bwlimit": cs.bwlimit,
                "invert": cs.invert,
            }
        return d

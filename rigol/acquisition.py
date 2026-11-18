"""Acquisition manager: trigger-driven captures (Single / Repeat / Free).

One instance per scope, shared by every browser client. It is the only owner of
SCPI traffic that is not a direct user control: the trigger watcher, the screen
snapshot, the RAW read and the slow (5 s) state poll all live here.

Capture modes
-------------
single  arm once (``:SING``), wait for STOP, snapshot, stay stopped
repeat  like single, re-arm after every capture (>= 200 ms apart)
free    ``:RUN``; nothing is read automatically, ``read_screen_now()`` reads the
        screen without stopping (channels may come from different triggers)

All channels of a single/repeat capture come from the *same* acquisition because
they are read while the scope is stopped.
"""

from __future__ import annotations

import asyncio
import logging
import re
import struct
import time
from typing import Any, Awaitable, Callable, Protocol

from .capture import ScreenCapture
from .raw_reader import RawCancelled, RawReadError, RawResult, read_raw
from .scope import CHANNELS, DS1054Z, scpi_is_query
from .transport import TransportError
from .waveform import preamble_to_bytes

log = logging.getLogger(__name__)

CAPTURE_MAGIC = 0x43  # 'C'
MIN_REARM_S = 0.2
WATCH_PERIOD_S = 0.2           # ~5 Hz trigger status
STOP_FALLBACK_S = 0.6          # after :SING, accept STOP even if no WAIT/TD was seen
STATE_POLL_S = 5.0
OUTAGE_GRACE_S = 10.0          # the trigger watcher rides out a link outage this long
LOCK_WAIT_S = 2.0              # Cancel waits this long for the command lock, then proceeds

_SPEC_RE = re.compile(r"[A-Za-z]{3,10},(CHAN[1-4]|MATH)(,(CHAN[1-4]|MATH))?", re.I)


def valid_param_spec(spec: str) -> bool:
    """``ITEM,SRC[,SRC2]`` - validated so a client cannot inject SCPI."""
    return isinstance(spec, str) and bool(_SPEC_RE.fullmatch(spec))


def _bad_scpi_text(s: str) -> bool:
    return "\n" in s or "\r" in s


def _same_depth(reported: str, want: int) -> bool:
    """Compare an :ACQ:MDEP? reply with a requested depth numerically ("AUTO" never matches)."""
    try:
        return int(float(reported.strip())) == int(want)
    except ValueError:
        return False


class Sink(Protocol):
    async def broadcast_json(self, msg: dict) -> None: ...
    async def broadcast_bytes(self, data: bytes) -> None: ...


class _Hijacked(Exception):
    """Someone pressed RUN on the scope while we were armed."""


class AcquisitionManager:
    def __init__(
        self,
        scope: DS1054Z,
        sink: Sink,
        params_provider: Callable[[], list[str]] = lambda: [],
        raw_channels_provider: Callable[[], list[int]] = lambda: [1, 4],
    ):
        self.scope = scope
        self.sink = sink
        self.params_provider = params_provider
        self.raw_channels_provider = raw_channels_provider

        self.mode = "single"
        self.armed = False
        self.then_raw = False
        self.capture_count = 0
        self.last_capture: ScreenCapture | None = None
        self.last_ts: float | None = None
        self.prev_run_state: bool | None = None
        self.busy_raw = False
        self.scope_ok = True
        self.raw_mdep = 600_000

        self.capture_listeners: list[Callable[[ScreenCapture], Awaitable[None]]] = []
        self.raw_listeners: list[Callable[[RawResult, ScreenCapture], Awaitable[None]]] = []
        # called with the screen capture when the RAW part of a RAW capture could not be done
        self.raw_failed_listeners: list[Callable[[ScreenCapture], Awaitable[None]]] = []

        self._task: asyncio.Task | None = None
        self._poll_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._raw_cancel = asyncio.Event()
        self._last_published: dict | None = None
        self._last_arm_t = 0.0
        # True once the capture of a non-repeat arm has been taken: a new Capture
        # click arriving while the loop is still wrapping up must re-arm, not be
        # swallowed by the "already armed" idempotency check.
        self._cycle_done = False
        self._gen = 0                       # bumped by cancel(): an arm() already in flight gives up
        self.prev_sweep: str | None = None

    # ------------------------------------------------------------------ life

    async def start(self) -> None:
        """Start the slow state poll. The initial full state is read by the caller."""
        self._poll_task = asyncio.create_task(self._state_poll(), name="state-poll")

    async def stop(self) -> None:
        for t in (self._task, self._poll_task):
            if t:
                t.cancel()
        for t in (self._task, self._poll_task):
            if t:
                await asyncio.gather(t, return_exceptions=True)
        self._task = self._poll_task = None

    # ------------------------------------------------------------- messaging

    def acq_state(self) -> dict:
        return {
            "mode": self.mode,
            "armed": self.armed,
            "trig_status": self.scope.state.trigger.status,
            "capture_count": self.capture_count,
            "last_ts": self.last_ts,
            "busy_raw": self.busy_raw,
            "raw_source": self.then_raw,
            "raw_mdep": self.raw_mdep,
            "mdep_options": list(self.scope.mdep_options()),
            "scope_ok": self.scope_ok,
        }

    async def publish(self, force: bool = False) -> None:
        st = self.acq_state()
        if force or st != self._last_published:
            self._last_published = st
            await self.sink.broadcast_json({"type": "acq_state", "data": st})

    async def publish_state(self) -> None:
        await self.sink.broadcast_json({"type": "state", "data": self.scope.state_to_dict()})

    async def _error(self, msg: str) -> None:
        log.warning("acquisition: %s", msg)
        await self.sink.broadcast_json({"type": "error", "msg": msg})

    async def _toast(self, msg: str, level: str = "info") -> None:
        await self.sink.broadcast_json({"type": "toast", "level": level, "msg": msg})

    # ------------------------------------------------------------ state poll

    async def _state_poll(self) -> None:
        while True:
            await asyncio.sleep(STATE_POLL_S)
            if self.busy_raw:
                continue
            try:
                await self.scope.get_full_state()
                self.scope_ok = True
                await self.publish_state()
            except TransportError as exc:
                self.scope_ok = False
                log.warning("State poll: %s", exc)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("State poll crashed")
            await self.publish()

    # --------------------------------------------------------- mode commands

    async def command(self, cmd: str) -> None:
        """Handle an ``acq`` message. Never raises."""
        try:
            if cmd in ("single", "repeat"):
                await self.arm(cmd)
            elif cmd == "free":
                await self.enter_free()
            elif cmd == "cancel":
                await self.cancel()
            elif cmd == "read_screen":
                await self.read_screen_now()
            else:
                await self._error(f"Unknown acquisition command: {cmd}")
        except asyncio.CancelledError:
            raise
        except TransportError as exc:
            self.scope_ok = False
            await self._error(f"Scope error: {exc}")
        except Exception as exc:
            log.exception("acq command %s crashed", cmd)
            await self._error(f"Internal error in '{cmd}': {exc}")
        await self.publish()

    async def _scope_running(self) -> bool:
        st = await self.scope.get_trigger_status()
        return st != "STOP"

    async def arm(self, mode: str, raw: bool = False) -> None:
        async with self._lock:
            if self.busy_raw:
                await self._error("RAW read in progress - cancel it first")
                return
            if (self.armed and self.mode == mode and self.then_raw == raw
                    and not self._cycle_done):
                await self.publish(force=True)   # idempotent: a second tab pressed Capture
                return
            my_gen = self._gen
            was_armed = self.armed
            await self._kill_task()
            self.mode = mode
            self.then_raw = raw
            if not was_armed or self.prev_run_state is None:
                self.prev_run_state = await self._scope_running()
                self.prev_sweep = await self.scope.get_trigger_sweep()
            if my_gen != self._gen:
                return
            if raw:
                await self._apply_raw_mdep()
            if my_gen != self._gen:
                return                      # cancelled while we were talking to the scope
            self.armed = True
            self._cycle_done = False
            self._task = asyncio.create_task(self._armed_loop(), name="acq")
            await self.publish()

    async def enter_free(self) -> None:
        self._gen += 1
        async with self._lock:
            if self.busy_raw:
                await self._error("RAW read in progress - cancel it first")
                return
            if not self.armed and self.mode == "free" and await self._scope_running():
                await self.publish(force=True)     # already free and running: no second :RUN
                return
            await self._kill_task()
            self.armed = False
            self.then_raw = False
            self.prev_run_state = None
            self.mode = "free"
            await self.scope.run()
            await self.publish_state()
            await self.publish()

    async def stop_scope(self) -> None:
        self._gen += 1
        async with self._lock:
            if self.busy_raw:
                await self._error("RAW read in progress - cancel it first")
                return
            await self._kill_task()
            self.armed = False
            self.prev_run_state = None
            await self.scope.stop()
            await self.publish_state()
            await self.publish()

    async def cancel(self) -> None:
        """Stop watching; restore RUN if the scope was running before ``arm()``."""
        self._gen += 1                       # an arm() stuck on a dead scope drops out when it resumes
        # Wait for any in-flight arm()/enter_free() so Cancel is ordered after it, but not
        # forever: that command may be hung on a scope that does not answer.
        try:
            await asyncio.wait_for(self._lock.acquire(), LOCK_WAIT_S)
            locked = True
        except asyncio.TimeoutError:
            locked = False
        try:
            was_armed = self.armed
            self._raw_cancel.set()
            await self._kill_task()
            self.armed = False
            self.then_raw = False
            await self.publish()
            if was_armed:
                restore = bool(self.prev_run_state)
                self.prev_run_state = None
                if restore:
                    self.mode = "free"
                    await self.scope.run(self.prev_sweep)
                else:
                    await self.scope.stop()
                await self.publish_state()
            await self.publish()
        finally:
            if locked:
                self._lock.release()

    async def _kill_task(self) -> None:
        t = self._task
        self._task = None
        if t and not t.done():
            t.cancel()
            await asyncio.gather(t, return_exceptions=True)
        self.busy_raw = False

    async def read_screen_now(self) -> None:
        """Snapshot the screen without stopping (free mode)."""
        if self.busy_raw:
            await self._error("RAW read in progress")
            return
        st = await self.scope.get_trigger_status()
        await self.snapshot(coherent=(st == "STOP"), mode="free" if st != "STOP" else self.mode)

    # ----------------------------------------------------------- armed loop

    async def _armed_loop(self) -> None:
        try:
            while True:
                self._last_arm_t = time.monotonic()
                await self.scope.arm_single()
                await self._wait_for_stop()
                cap = await self.snapshot(
                    coherent=True, last=(self.mode != "repeat" and not self.then_raw))
                if self.then_raw:
                    await self._do_raw(cap)
                if self.mode != "repeat":
                    break
                wait = MIN_REARM_S - (time.monotonic() - self._last_arm_t)
                if wait > 0:
                    await asyncio.sleep(wait)
        except asyncio.CancelledError:
            raise                      # cancel()/arm() own the bookkeeping
        except _Hijacked:
            log.info("Scope was set to RUN while armed: switching to free mode")
            self.mode = "free"
            self.prev_run_state = None
            await self._toast("Scope was set to RUN: switched to Free mode")
        except RawCancelled:
            await self._toast("RAW read cancelled")
        except (RawReadError, TransportError) as exc:
            self.scope_ok = not isinstance(exc, TransportError)
            await self._error(f"Capture failed: {exc}")
        except Exception as exc:
            log.exception("armed loop crashed")
            await self._error(f"Internal error: {exc}")
        # Natural end (single done, error, hijack): leave the scope as it is.
        self.armed = False
        self.then_raw = False
        self.prev_run_state = None
        self.busy_raw = False
        self._task = None
        await self.publish()

    async def _wait_for_stop(self) -> None:
        t_arm = time.monotonic()
        outage = 0
        outage_t0 = 0.0
        seen_non_stop = seen_wait = False
        last_st = None
        while True:
            await asyncio.sleep(WATCH_PERIOD_S)
            try:
                st = await self.scope.get_trigger_status()
            except TransportError:
                # brief outage (the scheduler reconnects in the background): keep waiting
                self.scope_ok = False
                outage = outage + 1 if outage_t0 else 1
                outage_t0 = outage_t0 or time.monotonic()
                if time.monotonic() - outage_t0 > OUTAGE_GRACE_S:
                    raise
                await self.publish()
                continue
            outage_t0 = 0.0
            self.scope_ok = True
            await self.publish()
            age = time.monotonic() - t_arm
            if st != last_st:
                log.debug("trigger status %s -> %s (%.2fs after arm)", last_st, st, age)
                last_st = st
            if st != "STOP":
                seen_non_stop = True
            if st == "WAIT":
                seen_wait = True
            if st == "STOP":
                if seen_non_stop or age >= STOP_FALLBACK_S:
                    return
            elif st in ("RUN", "AUTO") and (seen_wait or age >= STOP_FALLBACK_S):
                raise _Hijacked()

    # -------------------------------------------------------------- snapshot

    async def snapshot(self, coherent: bool, mode: str | None = None,
                       last: bool = False) -> ScreenCapture:
        scope = self.scope
        channels = scope.enabled_channels()
        if not channels:
            raise RawReadError("No channel is enabled on the scope")
        data = {}
        for ch in channels:
            data[ch] = await scope.get_waveform(ch)
        params: dict[str, float | None] = {}
        for spec in self.params_provider():
            if valid_param_spec(spec):
                params[spec] = await scope.get_measurement_spec(spec)
        srate = await scope.get_sample_rate()
        mdep = await scope.get_memory_depth()
        tdiv = await scope.get_timebase_scale()
        toff = await scope.get_timebase_offset()
        self.capture_count += 1
        cap = ScreenCapture(
            id=self.capture_count, ts=time.time(), mode=mode or self.mode,
            coherent=coherent, channels=channels, data=data, srate=srate,
            mdep=mdep, tdiv=tdiv, toff=toff, scope_params=params,
        )
        self.last_capture = cap
        self.last_ts = cap.ts
        if last:
            self._cycle_done = True
        for ch in channels:
            payload, pre = data[ch]
            frame = (struct.pack("<BBI", CAPTURE_MAGIC, ch, cap.id)
                     + preamble_to_bytes(pre) + payload)
            await self.sink.broadcast_bytes(frame)
        await self.sink.broadcast_json({"type": "capture", "data": cap.to_json()})
        for fn in list(self.capture_listeners):
            try:
                await fn(cap)
            except Exception:
                log.exception("capture listener failed")
        await self.publish()
        return cap

    # ------------------------------------------------------------------- RAW

    async def set_raw_mdep(self, n: int) -> None:
        if n not in self.scope.mdep_options():
            await self._error(
                f"{n} points is not valid with {len(self.scope.enabled_channels())} "
                f"channel(s) enabled; valid: {list(self.scope.mdep_options())}"
            )
        else:
            self.raw_mdep = n
        await self.publish()

    async def raw_capture(self, mode: str = "single") -> None:
        try:
            if mode not in ("single", "repeat"):
                await self._toast("RAW capture needs Single or Repeat mode", "warn")
                return
            await self.arm(mode, raw=True)
        except asyncio.CancelledError:
            raise
        except TransportError as exc:
            await self._error(f"Scope error: {exc}")
        except Exception as exc:
            log.exception("raw_capture crashed")
            await self._error(str(exc))
        await self.publish()

    async def _apply_raw_mdep(self) -> None:
        """Set the memory depth before arming (scope in RUN) and read it back."""
        want = self.raw_mdep
        if want not in self.scope.mdep_options():
            self.raw_mdep = want = self.scope.mdep_options()[2]
            await self._toast(f"Memory depth reset to {want} for the enabled channels", "warn")
        cur = await self.scope.get_memory_depth()
        if _same_depth(cur, want):
            return
        if not await self._scope_running():
            await self.scope.run(self.prev_sweep)
            await asyncio.sleep(0.1)
        await self.scope.set_memory_depth(want)
        await asyncio.sleep(0.1)
        actual = await self.scope.get_memory_depth()
        if not _same_depth(actual, want):
            await self._toast(f"Memory depth is {actual}, not the requested {want}", "warn")

    async def _raw_failed(self, cap: ScreenCapture) -> None:
        for fn in list(self.raw_failed_listeners):
            try:
                await fn(cap)
            except Exception:
                log.exception("raw-failed listener failed")

    async def _do_raw(self, cap: ScreenCapture) -> None:
        channels = [c for c in self.raw_channels_provider() if c in cap.channels]
        if len(channels) < 2:
            await self._toast("V and I channels must both be enabled for a RAW capture", "warn")
            return                               # the screen capture was already shown (see AnalysisService)
        self.busy_raw = True
        self._raw_cancel.clear()
        await self.publish()

        async def progress(ch: int, done: int, total: int, eta: float) -> None:
            await self.sink.broadcast_json({
                "type": "raw_progress",
                "data": {"ch": ch, "done": done, "total": total, "eta_s": round(eta, 1)},
            })

        try:
            res = await read_raw(self.scope, channels, self.raw_mdep, progress, self._raw_cancel)
        except (RawReadError, TransportError):
            self.busy_raw = False
            await self._raw_failed(cap)          # show the screen data instead of a stale capture
            raise
        finally:
            self.busy_raw = False
        for w in res.warnings:
            await self._toast(w, "warn")
        if self.mode != "repeat":
            self._cycle_done = True
        await self.sink.broadcast_json({
            "type": "raw_done",
            "data": {"capture_id": cap.id, "n": res.n, "channels": res.channels,
                     "seconds": round(res.seconds, 3), "bytes_per_s": round(res.bytes_per_s)},
        })
        for fn in list(self.raw_listeners):
            try:
                await fn(res, cap)
            except Exception:
                log.exception("raw listener failed")
        await self.publish()

    # ------------------------------------------------------------- controls

    async def control(self, cmd: str, args: dict) -> None:
        """Handle a ``control`` message (scope settings). Never raises."""
        scope = self.scope
        try:
            if cmd == "run":
                await self.enter_free()
                return
            if cmd == "stop":
                await self.stop_scope()
                return
            if cmd == "single":
                await self.arm("single")
                await self.publish()
                return
            if self.busy_raw:
                await self._error("Scope busy: RAW read in progress")
                return
            if any(isinstance(v, str) and _bad_scpi_text(v) for v in args.values()):
                await self._error(f"Bad control '{cmd}': line breaks are not allowed")
                return
            n = args.get("n", args.get("dec"))
            handlers: dict[str, Callable[[], Awaitable[Any]]] = {
                "tforce": lambda: scope.tforce(),
                "autoscale": lambda: scope.autoscale(),
                "set_channel_display": lambda: scope.set_channel_display(args["ch"], args["on"]),
                "set_channel_scale": lambda: scope.set_channel_scale(args["ch"], args["val"]),
                "set_channel_offset": lambda: scope.set_channel_offset(args["ch"], args["val"]),
                "set_channel_coupling": lambda: scope.set_channel_coupling(args["ch"], args["mode"]),
                "set_channel_probe": lambda: scope.set_channel_probe(args["ch"], args["val"]),
                "set_channel_bwlimit": lambda: scope.set_channel_bwlimit(args["ch"], args["val"]),
                "set_channel_invert": lambda: scope.set_channel_invert(args["ch"], args["on"]),
                "set_timebase_scale": lambda: scope.set_timebase_scale(args["val"]),
                "set_timebase_offset": lambda: scope.set_timebase_offset(args["val"]),
                "set_trigger_level": lambda: scope.set_trigger_level(args["val"]),
                "set_trigger_source": lambda: scope.set_trigger_source(args["src"]),
                "set_trigger_slope": lambda: scope.set_trigger_slope(args["slope"]),
                "set_trigger_sweep": lambda: scope.set_trigger_sweep(args["sweep"]),
                "set_acquire_type": lambda: scope.set_acquire_type(args["type"]),
                "set_decoder_mode": lambda: scope.set_decoder_mode(n, args["mode"]),
                "set_decoder_display": lambda: scope.set_decoder_display(n, args["on"]),
                "set_decoder_format": lambda: scope.set_decoder_format(n, args["fmt"]),
                "set_cursor_mode": lambda: scope.set_cursor_mode(args["mode"]),
                "set_cursor_ax": lambda: scope.set_cursor_ax(args["x"]),
                "set_cursor_bx": lambda: scope.set_cursor_bx(args["x"]),
                "set_cursor_ay": lambda: scope.set_cursor_ay(args["y"]),
                "set_cursor_by": lambda: scope.set_cursor_by(args["y"]),
            }
            # Setters whose value the scope may coerce: read the parameter back.
            readback: dict[str, Callable[[], Awaitable[Any]]] = {
                "set_channel_scale": lambda: scope.get_channel_scale(args["ch"]),
                "set_channel_offset": lambda: scope.get_channel_offset(args["ch"]),
                "set_channel_probe": lambda: scope.get_channel_probe(args["ch"]),
                "set_channel_coupling": lambda: scope.get_channel_coupling(args["ch"]),
                "set_channel_display": lambda: scope.get_channel_display(args["ch"]),
                "set_timebase_scale": lambda: scope.get_timebase_scale(),
                "set_timebase_offset": lambda: scope.get_timebase_offset(),
                "set_trigger_level": lambda: scope.get_trigger_level(),
                "set_trigger_slope": lambda: scope.get_trigger_slope(),
                "set_trigger_sweep": lambda: scope.get_trigger_sweep(),
                "set_acquire_type": lambda: scope.get_acquire_type(),
                "tforce": lambda: scope.get_trigger_status(),
            }
            handler = handlers.get(cmd)
            if handler is None:
                log.warning("Unknown control command: %s", cmd)
                return
            log.info("Control: %s %s", cmd, args)
            await handler()
            rb = readback.get(cmd)
            if rb:
                await rb()
            await self.publish_state()
            await self.publish()
        except asyncio.CancelledError:
            raise
        except TransportError as exc:
            self.scope_ok = False
            await self._error(f"Control '{cmd}' failed: {exc}")
        except (KeyError, TypeError, ValueError) as exc:
            await self._error(f"Bad control '{cmd}': {exc!r}")
        except Exception as exc:
            log.exception("control %s crashed", cmd)
            await self._error(f"Control '{cmd}' failed: {exc}")

    async def scpi(self, cmd: str) -> str:
        if self.busy_raw:
            return "ERROR: scope busy (RAW read in progress)"
        if _bad_scpi_text(cmd):
            return "ERROR: line breaks are not allowed in a command"
        try:
            if scpi_is_query(cmd):
                return await self.scope._q(cmd)
            await self.scope._w(cmd)
            return "OK"
        except TransportError as exc:
            return f"ERROR: {exc}"

    async def screenshot(self, invert: bool = False, grayscale: bool = False) -> bytes:
        scope = self.scope
        if self.busy_raw:
            raise TransportError("scope busy (RAW read in progress)")
        try:
            if invert:
                await scope.set_image_invert(True)
                await asyncio.sleep(0.2)
            if grayscale:
                await scope.set_image_color(False)
                await asyncio.sleep(0.2)
            return await scope.get_screenshot()
        finally:
            try:
                if invert:
                    await scope.set_image_invert(False)
                if grayscale:
                    await scope.set_image_color(True)
            except TransportError:
                pass

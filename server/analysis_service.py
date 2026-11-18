"""The shared "current analysis capture" and the operations on it."""

from __future__ import annotations

import asyncio
import io
import logging
import math
import time

from rigol import analysis as an
from rigol.capture import ScreenCapture
from rigol.raw_reader import RawResult

log = logging.getLogger(__name__)


class AnalysisService:
    def __init__(self, hub, raw_pending=lambda: False):
        self.hub = hub
        self.raw_pending = raw_pending       # True while a RAW capture is in progress
        self.current: an.AnalysisCapture | None = None
        self.v_ch = 1
        self.i_ch = 4
        self.cfg = an.AnalysisConfig()
        self.hold = False
        self.rev = 0
        self.last_screen: ScreenCapture | None = None
        self.last_raw: tuple[RawResult, ScreenCapture] | None = None
        self.csv: an.ParsedCSV | None = None
        self.src: tuple | None = None        # what the displayed capture was built from (for re-mapping)
        self.held_count = 0
        self.held_id: int | None = None

    # ---------------------------------------------------------------- state

    def state(self) -> dict:
        return {
            "hold": self.hold, "held_count": self.held_count, "held_id": self.held_id,
            "v_ch": self.v_ch, "i_ch": self.i_ch, "i_threshold": self.cfg.i_threshold,
            "has_capture": self.current is not None,
        }

    def meta(self) -> dict | None:
        c = self.current
        if c is None:
            return None
        d = c.summary()
        d["rev"] = self.rev
        return d

    async def publish_state(self) -> None:
        await self.hub.broadcast_json({"type": "analysis_state", "data": self.state()})

    async def _set_current(self, cap: an.AnalysisCapture, src: tuple | None = None) -> None:
        self.current = cap
        self.src = src
        self.rev += 1
        self.held_count = 0
        self.held_id = None
        await self.hub.broadcast_json({"type": "analysis_meta", "data": self.meta()})
        await self.publish_state()

    async def _held(self, cap_id: int) -> None:
        self.held_count += 1
        self.held_id = cap_id
        await self.publish_state()

    # ------------------------------------------------------------ listeners

    async def on_screen_capture(self, cap: ScreenCapture) -> None:
        self.last_screen = cap
        if self.raw_pending() and self.v_ch in cap.channels and self.i_ch in cap.channels:
            return                            # the RAW read will replace it a moment later
        if self.hold:
            await self._held(cap.id)
            return
        try:
            ac = await asyncio.to_thread(an.from_screen_capture, cap, self.v_ch, self.i_ch)
        except an.AnalysisError as exc:
            await self.hub.broadcast_json({"type": "toast", "level": "warn", "msg": str(exc)})
            return
        await self._set_current(ac, ("screen", cap))

    async def on_raw_failed(self, cap: ScreenCapture) -> None:
        """The RAW part failed: show the screen capture rather than a stale one (unless held)."""
        if self.hold:
            await self._held(cap.id)
            return
        try:
            ac = await asyncio.to_thread(an.from_screen_capture, cap, self.v_ch, self.i_ch)
        except an.AnalysisError:
            return
        await self._set_current(ac, ("screen", cap))
        await self.hub.broadcast_json({"type": "toast", "level": "warn",
                                       "msg": "RAW read failed: showing the 1200 screen points"})

    async def on_raw(self, res: RawResult, cap: ScreenCapture) -> None:
        self.last_raw = (res, cap)
        if self.hold:
            await self._held(cap.id)
            return
        await self._from_raw(res, cap)

    async def _from_raw(self, res: RawResult, cap: ScreenCapture) -> None:
        meta = {"coherent": True, "tdiv": cap.tdiv, "toff": cap.toff}
        try:
            ac = await asyncio.to_thread(
                an.from_raw, res, self.v_ch, self.i_ch, cap.scope_params, meta, cap.id, cap.ts)
        except an.AnalysisError as exc:
            await self.hub.broadcast_json({"type": "toast", "level": "warn", "msg": str(exc)})
            return
        await self._set_current(ac, ("raw", (res, cap)))

    # ------------------------------------------------------------- commands

    async def select(self, source: str) -> str | None:
        """Show the latest screen or RAW capture (releases held captures). Returns an error."""
        if source == "last_raw" and self.last_raw:
            await self._from_raw(*self.last_raw)
            return None
        if self.last_screen is not None and source in ("last_screen", "last_raw"):
            try:
                ac = await asyncio.to_thread(
                    an.from_screen_capture, self.last_screen, self.v_ch, self.i_ch)
            except an.AnalysisError as exc:
                return str(exc)
            await self._set_current(ac, ("screen", self.last_screen))
            return None
        return "No capture available yet"

    async def configure(self, v_ch: int | None, i_ch: int | None, i_thr: float | None) -> str | None:
        """Change channels / threshold; rebuild the current capture if the channels changed."""
        err = None
        old = (self.v_ch, self.i_ch)
        if type(v_ch) is int and 1 <= v_ch <= 4:
            self.v_ch = v_ch
        if type(i_ch) is int and 1 <= i_ch <= 4:
            self.i_ch = i_ch
        if i_thr is not None and math.isfinite(i_thr):
            self.cfg.i_threshold = float(i_thr)
        if (self.v_ch, self.i_ch) != old and self.current is not None:
            err = await self._rebuild()
            if err:
                self.v_ch, self.i_ch = old
        await self.publish_state()
        return err

    async def _rebuild(self) -> str | None:
        """Re-map the *displayed* capture to the new channels (never swaps in a newer one)."""
        c = self.current
        kind, obj = self.src if self.src else (c.source, None)
        try:
            if kind == "screen" and obj is not None:
                ac = await asyncio.to_thread(an.from_screen_capture, obj, self.v_ch, self.i_ch)
            elif kind == "raw" and obj is not None:
                res, cap = obj
                ac = await asyncio.to_thread(
                    an.from_raw, res, self.v_ch, self.i_ch, cap.scope_params,
                    {"coherent": True, "tdiv": cap.tdiv}, cap.id, cap.ts)
            elif c.source == "csv" and self.csv is not None:
                ac = await asyncio.to_thread(
                    an.capture_from_csv, self.csv, self.v_ch, self.i_ch, 0, time.time(),
                    c.meta.get("file", ""))
            else:
                return "This capture cannot be re-mapped to other channels"
        except an.AnalysisError as exc:
            return str(exc)
        held = (self.held_count, self.held_id)
        await self._set_current(ac, self.src)
        self.held_count, self.held_id = held       # a re-map does not release held captures
        return None

    async def set_hold(self, on: bool) -> None:
        self.hold = bool(on)
        if not self.hold:
            self.held_count = 0
            self.held_id = None
        await self.publish_state()

    # ------------------------------------------------------------- compute

    async def stats(self, t0: float, t1: float) -> tuple[dict | None, int]:
        cap, rev, cfg = self.current, self.rev, an.AnalysisConfig(self.cfg.i_threshold)
        if cap is None:
            return None, rev
        return await asyncio.to_thread(an.window_stats, cap, t0, t1, cfg), rev

    async def envelope(self, t0: float, t1: float, width: int) -> tuple[dict | None, int]:
        cap, rev = self.current, self.rev
        if cap is None:
            return None, rev
        return await asyncio.to_thread(an.envelope, cap, t0, t1, width), rev

    # ------------------------------------------------------------ import/export

    async def import_file(self, name: str, data: bytes, v_ch: int | None, i_ch: int | None) -> dict:
        """Load a .csv/.npz; raises AnalysisError / ChannelsMissing. Sets hold so live captures don't replace it."""
        lower = name.lower()
        v_ch = v_ch or self.v_ch
        i_ch = i_ch or self.i_ch
        if lower.endswith(".npz"):
            cap = await asyncio.to_thread(an.load_npz, io.BytesIO(data))
            self.csv = None
            v_ch, i_ch = cap.v_ch, cap.i_ch
        elif lower.endswith(".csv"):
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise an.AnalysisError(f"CSV is not valid UTF-8 text: {exc}") from exc
            parsed = await asyncio.to_thread(an.parse_app_csv, text)
            cap = await asyncio.to_thread(
                an.capture_from_csv, parsed, v_ch, i_ch, 0, time.time(), name)
            self.csv = parsed
        else:
            raise an.AnalysisError("Unsupported file type; use .csv or .npz")
        self.v_ch, self.i_ch = cap.v_ch, cap.i_ch
        self.hold = True
        await self._set_current(cap)
        return {"ok": True, "source": cap.source, "channels": cap.meta.get("channels", []),
                "meta": self.meta()}

    async def export_npz(self) -> bytes | None:
        cap = self.current
        if cap is None:
            return None

        def build() -> bytes:
            buf = io.BytesIO()
            an.save_npz(cap, buf)
            return buf.getvalue()

        return await asyncio.to_thread(build)

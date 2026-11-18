"""Chunked RAW-memory reader for the DS1054Z.

``:WAV:MODE RAW`` reads the scope's internal memory, only in STOP state, at most
250 000 points per ``:WAV:DATA?`` (BYTE format). Larger memories are read in
chunks with ``:WAV:STAR`` / ``:WAV:STOP`` (1-based, inclusive) and concatenated.
The RAW preamble (YINC/YORIG/YREF) differs from NORM, so it is read per channel
after selecting RAW mode.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import numpy as np

from .scope import DS1054Z
from .transport import TransportError
from .waveform import WaveformPreamble

log = logging.getLogger(__name__)

CHUNK_POINTS = 250_000


class RawReadError(Exception):
    pass


class RawCancelled(Exception):
    pass


@dataclass
class RawResult:
    channels: list[int]
    raw: dict[int, np.ndarray]                 # uint8, length n
    preambles: dict[int, WaveformPreamble]     # RAW preambles
    n: int
    srate: float
    mdep: str                                  # as reported by :ACQ:MDEP?
    seconds: float = 0.0
    bytes_per_s: float = 0.0
    chunks: int = 0
    timing: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)


ProgressCb = Callable[[int, int, int, float], "Awaitable[None] | None"]


async def read_raw(
    scope: DS1054Z,
    channels: list[int],
    mdep: int | None = None,
    progress_cb: ProgressCb | None = None,
    cancel_event: asyncio.Event | None = None,
) -> RawResult:
    """Read the full internal memory of ``channels``. See module docstring."""
    channels = list(dict.fromkeys(channels))
    if not channels:
        raise RawReadError("No channels to read")

    # 1. STOP state required
    status = await scope.get_trigger_status()
    if status != "STOP":
        await scope.stop()
        for _ in range(20):
            status = await scope.get_trigger_status()
            if status == "STOP":
                break
            await asyncio.sleep(0.1)
        else:
            raise RawReadError("Scope did not reach STOP")

    # 2. Memory depth and sample rate
    mdep_str = await scope.get_memory_depth()
    srate = await scope.get_sample_rate()

    # 3. Per channel: RAW preamble, then chunks
    pres: dict[int, WaveformPreamble] = {}
    for ch in channels:
        pres[ch] = await scope.get_preamble_raw(ch)
    try:
        n_mdep = int(float(mdep_str))
    except ValueError:               # "AUTO": the RAW preamble tells the real size
        n_mdep = 0
    n_pre = pres[channels[0]].points
    # The preamble describes what the scope will serve; trust it when it disagrees.
    n = n_pre if n_pre > 0 else n_mdep
    if n <= 0:
        raise RawReadError("Scope reports no memory points")
    warnings: list[str] = []
    if n_mdep and n_pre and n_mdep != n_pre:
        msg = (f"Memory depth is {n_mdep:,} but the RAW preamble reports {n_pre:,} points; "
               f"reading {n:,} (check on the scope)")
        log.warning(msg)
        warnings.append(msg)
    if mdep and n != mdep:
        log.info("Requested depth %d, scope serves %d", mdep, n)
    if any(p.points not in (0, n) for p in pres.values()):
        raise RawReadError("Channels report different RAW lengths")

    out: dict[int, np.ndarray] = {}
    total_bytes = n * len(channels)
    done_bytes = 0
    chunk_count = 0
    t_start = time.monotonic()
    t_read = 0.0

    for ch in channels:
        arr = np.empty(n, dtype=np.uint8)
        start = 1
        while start <= n:
            if cancel_event is not None and cancel_event.is_set():
                raise RawCancelled()
            stop = min(start + CHUNK_POINTS - 1, n)
            want = stop - start + 1
            payload = None
            for attempt in range(2):
                t0 = time.monotonic()
                try:
                    payload = await scope.get_raw_chunk(ch, start, stop)
                except TransportError as exc:
                    if exc.fatal or attempt == 1:
                        raise RawReadError(f"CH{ch} chunk {start}-{stop}: {exc}") from exc
                    log.warning("CH%d chunk %d-%d failed (%s), retrying", ch, start, stop, exc)
                    payload = None
                    continue
                t_read += time.monotonic() - t0
                if len(payload) == want:
                    break
                log.warning("CH%d chunk %d-%d: got %d bytes, expected %d",
                            ch, start, stop, len(payload), want)
                payload = None
            if payload is None:
                raise RawReadError(
                    f"CH{ch} chunk {start}-{stop}: wrong length after retry"
                )
            arr[start - 1:stop] = np.frombuffer(payload, dtype=np.uint8)
            chunk_count += 1
            done_bytes += want
            start = stop + 1
            if progress_cb is not None:
                elapsed = max(time.monotonic() - t_start, 1e-9)
                rate = done_bytes / elapsed
                eta = (total_bytes - done_bytes) / rate if rate > 0 else 0.0
                res = progress_cb(ch, start - 1, n, eta)
                if inspect.isawaitable(res):
                    await res
            await asyncio.sleep(0)       # let the event loop breathe
        out[ch] = arr

    seconds = time.monotonic() - t_start
    bps = total_bytes / seconds if seconds > 0 else 0.0
    log.info("RAW read: %d ch x %d pts in %.2fs (%.2f MB/s, %d chunks; wire time %.2fs)",
             len(channels), n, seconds, bps / 1e6, chunk_count, t_read)
    return RawResult(
        channels=channels, raw=out, preambles=pres, n=n, srate=srate,
        mdep=mdep_str, seconds=seconds, bytes_per_s=bps, chunks=chunk_count,
        timing={"wire_s": t_read}, warnings=warnings,
    )

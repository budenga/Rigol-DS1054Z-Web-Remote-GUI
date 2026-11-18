"""Power analysis engine: pure numpy, no asyncio.

A capture holds a voltage and a current trace on one uniform time base. From a
time window [t0, t1] it computes averages, power p = v*i, energy, charge and
pulse statistics, and builds a min/max envelope for drawing.

Conventions
-----------
* ``t[k] = t0 + k*dt``; the window contains the samples with ``t0 <= t <= t1``
  (index arithmetic, no masks over the full array).
* Clipped ADC samples (raw 0 / 255) are NaN in ``v`` / ``i`` and are excluded
  from everything that uses them.
* Window duration is ``n_window * dt`` (rectangle rule, uniform sampling).
* Arrays stay float32; sums and means use float64 accumulators (block-wise, so
  a 12 Mpts window does not allocate 100 MB temporaries).
"""

from __future__ import annotations

import csv
import io
import json
import math
from dataclasses import dataclass, field
from typing import Iterator

import numpy as np

from .capture import ScreenCapture
from .waveform import WaveformPreamble

BLOCK = 1 << 20
MAX_CSV_EXPORT = 2_000_000
NPZ_VERSION = 1


class AnalysisError(Exception):
    """User-facing problem with analysis data (bad file, missing channels, ...)."""


class ChannelsMissing(AnalysisError):
    def __init__(self, msg: str, present: list[int]):
        super().__init__(msg)
        self.present = present


class WindowTooLarge(AnalysisError):
    pass


@dataclass
class AnalysisConfig:
    i_threshold: float = 1.0


@dataclass
class AnalysisCapture:
    id: int
    ts: float
    source: str                       # "screen" | "raw" | "csv" | "npz"
    v_ch: int
    i_ch: int
    t0: float
    dt: float
    n: int
    v: np.ndarray                     # float32, NaN where clipped
    i: np.ndarray
    v_clip: int = 0
    i_clip: int = 0
    scope_params: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)
    # Optional raw bytes + preambles (screen / raw sources) so .npz round-trips exactly
    raw_v: np.ndarray | None = field(default=None, repr=False)
    raw_i: np.ndarray | None = field(default=None, repr=False)
    pre_v: WaveformPreamble | None = field(default=None, repr=False)
    pre_i: WaveformPreamble | None = field(default=None, repr=False)
    _p: np.ndarray | None = field(default=None, repr=False)

    @property
    def p(self) -> np.ndarray:
        """v*i, float32, computed lazily and cached."""
        if self._p is None:
            self._p = self.v * self.i
        return self._p

    @property
    def t_start(self) -> float:
        return self.t0

    @property
    def t_end(self) -> float:
        return self.t0 + (self.n - 1) * self.dt

    def summary(self) -> dict:
        return {
            "id": self.id, "ts": self.ts, "source": self.source, "n": self.n,
            "dt": self.dt, "t_start": self.t_start, "t_end": self.t_end,
            "v_ch": self.v_ch, "i_ch": self.i_ch,
            "clipped": {"v": self.v_clip, "i": self.i_clip},
            "scope_params": dict(self.scope_params), "meta": dict(self.meta),
        }


# ---------------------------------------------------------------------------
# Raw bytes -> volts
# ---------------------------------------------------------------------------

def raw_to_float(raw: np.ndarray, pre: WaveformPreamble) -> tuple[np.ndarray, int]:
    """(raw - yorig - yref) * yinc as float32, NaN where raw is 0 or 255. Returns (array, n_clipped)."""
    out = np.subtract(raw, np.float32(pre.yorig + pre.yref), dtype=np.float32)
    out *= np.float32(pre.yinc)
    clipped = (raw == 0) | (raw == 255)
    n_clip = int(np.count_nonzero(clipped))
    if n_clip:
        out[clipped] = np.nan
    return out, n_clip


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

def _build(id_, ts, source, v_ch, i_ch, t0, dt, raw_v, raw_i, pre_v, pre_i, scope_params, meta):
    v, vc = raw_to_float(raw_v, pre_v)
    i, ic = raw_to_float(raw_i, pre_i)
    n = min(len(v), len(i))
    if len(v) != len(i):
        v, i, raw_v, raw_i = v[:n], i[:n], raw_v[:n], raw_i[:n]
        vc = int(np.count_nonzero(np.isnan(v)))
        ic = int(np.count_nonzero(np.isnan(i)))
    return AnalysisCapture(
        id=id_, ts=ts, source=source, v_ch=v_ch, i_ch=i_ch, t0=t0, dt=dt, n=n,
        v=v, i=i, v_clip=vc, i_clip=ic, scope_params=dict(scope_params or {}),
        meta=dict(meta or {}), raw_v=raw_v, raw_i=raw_i, pre_v=pre_v, pre_i=pre_i,
    )


def from_screen_capture(cap: ScreenCapture, v_ch: int, i_ch: int) -> AnalysisCapture:
    """The 1200 screen points of an acquisition."""
    missing = [c for c in (v_ch, i_ch) if c not in cap.data]
    if missing:
        raise ChannelsMissing(
            f"Capture has channels {sorted(cap.data)}; CH{missing[0]} is missing", sorted(cap.data)
        )
    pv, pi_ = cap.data[v_ch][1], cap.data[i_ch][1]
    raw_v = np.frombuffer(cap.data[v_ch][0], dtype=np.uint8).copy()
    raw_i = np.frombuffer(cap.data[i_ch][0], dtype=np.uint8).copy()
    meta = {"srate": cap.srate, "mdep": cap.mdep, "tdiv": cap.tdiv, "coherent": cap.coherent,
            "channels": sorted(cap.data)}
    return _build(cap.id, cap.ts, "screen", v_ch, i_ch, pv.xorig, pv.xinc,
                  raw_v, raw_i, pv, pi_, cap.scope_params, meta)


def from_raw(raw_result, v_ch: int, i_ch: int, scope_params: dict | None = None,
             meta: dict | None = None, id_: int = 0, ts: float = 0.0) -> AnalysisCapture:
    """A RAW memory read (``rigol.raw_reader.RawResult``)."""
    for c in (v_ch, i_ch):
        if c not in raw_result.raw:
            raise ChannelsMissing(
                f"RAW capture has channels {sorted(raw_result.raw)}; CH{c} is missing",
                sorted(raw_result.raw),
            )
    pv, pi_ = raw_result.preambles[v_ch], raw_result.preambles[i_ch]
    m = {"srate": raw_result.srate, "mdep": raw_result.mdep, "n": raw_result.n,
         "raw_seconds": raw_result.seconds, "bytes_per_s": raw_result.bytes_per_s,
         "channels": sorted(raw_result.raw)}
    m.update(meta or {})
    return _build(id_, ts, "raw", v_ch, i_ch, pv.xorig, pv.xinc,
                  raw_result.raw[v_ch], raw_result.raw[i_ch], pv, pi_, scope_params, m)


def from_arrays(id_, ts, source, v_ch, i_ch, t0, dt, v, i, scope_params=None, meta=None):
    v = np.asarray(v, dtype=np.float32)
    i = np.asarray(i, dtype=np.float32)
    n = min(len(v), len(i))
    v, i = v[:n], i[:n]
    return AnalysisCapture(
        id=id_, ts=ts, source=source, v_ch=v_ch, i_ch=i_ch, t0=float(t0), dt=float(dt),
        n=n, v=v, i=i, v_clip=int(np.count_nonzero(np.isnan(v))),
        i_clip=int(np.count_nonzero(np.isnan(i))),
        scope_params=dict(scope_params or {}), meta=dict(meta or {}),
    )


# ---------------------------------------------------------------------------
# Window helpers
# ---------------------------------------------------------------------------

def window_indices(cap: AnalysisCapture, t0: float, t1: float) -> tuple[int, int]:
    """Inclusive sample index range for t0 <= t <= t1 ((0, -1) when empty)."""
    if t0 > t1:
        t0, t1 = t1, t0
    if cap.n <= 0 or cap.dt <= 0 or not (math.isfinite(t0) and math.isfinite(t1)):
        return 0, -1
    lo, hi = cap.t0 - cap.dt, cap.t_end + cap.dt      # clamp first: 1e308 would overflow the index math
    t0 = min(max(t0, lo), hi)
    t1 = min(max(t1, lo), hi)
    a = math.ceil((t0 - cap.t0) / cap.dt - 1e-9)
    b = math.floor((t1 - cap.t0) / cap.dt + 1e-9)
    a = max(a, 0)
    b = min(b, cap.n - 1)
    if a > b:
        return 0, -1
    return int(a), int(b)


def _blocks(a: int, b: int) -> Iterator[tuple[int, int]]:
    """Half-open block ranges covering [a, b] inclusive."""
    k = a
    while k <= b:
        e = min(k + BLOCK, b + 1)
        yield k, e
        k = e


def _num(x) -> float | None:
    if x is None:
        return None
    x = float(x)
    return x if math.isfinite(x) else None


_EMPTY_KEYS = (
    "v_avg", "i_avg", "v_rms", "i_rms", "v_min", "v_max", "i_min", "i_max", "i_pk",
    "p_avg", "p_rms", "p_max", "p_pk", "p_min", "energy_j", "energy_wh", "energy_mwh",
    "charge_c", "charge_mah", "crest_i", "v_rest", "v_drop", "r_app", "t_above",
    "t_pulse_max",
)


def _empty_stats(cap: AnalysisCapture) -> dict:
    d = {k: None for k in _EMPTY_KEYS}
    d.update(n_pulses=0, dt_window=0.0, n_window=0, n_valid=0, n_clipped_v=0,
             n_clipped_i=0, sample_dt=cap.dt, t0=cap.t0, t1=cap.t0)
    return d


def window_stats(cap: AnalysisCapture, t0: float, t1: float,
                 cfg: AnalysisConfig | None = None) -> dict:
    """All computed parameters for the window. Never raises on odd windows."""
    cfg = cfg or AnalysisConfig()
    a, b = window_indices(cap, t0, t1)
    if b < a:
        return _empty_stats(cap)
    n_win = b - a + 1
    dt = cap.dt
    dt_win = n_win * dt
    v = cap.v[a:b + 1]
    i = cap.i[a:b + 1]

    sv = si = sv2 = si2 = spp = spp2 = 0.0
    nv = ni = npair = 0
    vmin = imin = pmin = math.inf
    vmax = imax = pmax = -math.inf
    # least squares V = a + b*I over pairs
    s_i_pair = s_v_pair = s_ii = s_iv = 0.0
    i_lo = math.inf
    i_hi = -math.inf
    for k0, k1 in _blocks(0, n_win - 1):
        vb = v[k0:k1]
        ib = i[k0:k1]
        vok = ~np.isnan(vb)
        iok = ~np.isnan(ib)
        v64 = vb.astype(np.float64)
        i64 = ib.astype(np.float64)
        if vok.all():
            vv = v64
        else:
            vv = v64[vok]
        if iok.all():
            ii = i64
        else:
            ii = i64[iok]
        if vv.size:
            sv += float(vv.sum())
            sv2 += float(np.dot(vv, vv))
            nv += vv.size
            vmin = min(vmin, float(vv.min()))
            vmax = max(vmax, float(vv.max()))
        if ii.size:
            si += float(ii.sum())
            si2 += float(np.dot(ii, ii))
            ni += ii.size
            imin = min(imin, float(ii.min()))
            imax = max(imax, float(ii.max()))
        both = vok & iok
        if both.all():
            pv, pi_ = v64, i64
        else:
            pv, pi_ = v64[both], i64[both]
        if pv.size:
            p = pv * pi_
            spp += float(p.sum())
            spp2 += float(np.dot(p, p))
            npair += pv.size
            pmin = min(pmin, float(p.min()))
            pmax = max(pmax, float(p.max()))
            s_i_pair += float(pi_.sum())
            s_v_pair += float(pv.sum())
            s_ii += float(np.dot(pi_, pi_))
            s_iv += float(np.dot(pi_, pv))
            i_lo = min(i_lo, float(pi_.min()))
            i_hi = max(i_hi, float(pi_.max()))

    d: dict = _empty_stats(cap)
    d.update(n_window=n_win, dt_window=dt_win, n_valid=npair,
             n_clipped_v=n_win - nv, n_clipped_i=n_win - ni,
             sample_dt=dt, t0=cap.t0 + a * dt, t1=cap.t0 + b * dt)
    if nv:
        d["v_avg"] = sv / nv
        d["v_rms"] = math.sqrt(sv2 / nv)
        d["v_min"], d["v_max"] = vmin, vmax
    if ni:
        d["i_avg"] = si / ni
        d["i_rms"] = math.sqrt(si2 / ni)
        d["i_min"], d["i_max"] = imin, imax
        d["i_pk"] = imax
        if d["i_rms"] and d["i_rms"] > 0:
            d["crest_i"] = imax / d["i_rms"]
        d["charge_c"] = d["i_avg"] * dt_win
        d["charge_mah"] = d["charge_c"] / 3.6
    if npair:
        d["p_avg"] = spp / npair
        d["p_rms"] = math.sqrt(spp2 / npair)       # rms of p(t) = v(t)*i(t)
        d["p_max"] = d["p_pk"] = pmax
        d["p_min"] = pmin
        d["energy_j"] = d["p_avg"] * dt_win
        d["energy_wh"] = d["energy_j"] / 3600.0
        d["energy_mwh"] = d["energy_wh"] * 1000.0
        # slope of V vs I (apparent source resistance), needs a real current swing
        if i_hi - i_lo >= 0.2:
            den = npair * s_ii - s_i_pair * s_i_pair
            if den > 0:
                slope = (npair * s_iv - s_i_pair * s_v_pair) / den
                d["r_app"] = -slope
        # resting voltage: median of v where i < threshold
        if cfg.i_threshold is not None:
            rest = _rest_values(v, i, cfg.i_threshold)
            if rest.size:
                d["v_rest"] = float(np.median(rest))
                if d["v_min"] is not None:
                    d["v_drop"] = d["v_rest"] - d["v_min"]
    if ni:
        thr = cfg.i_threshold
        above = int(np.count_nonzero(i > thr))      # NaN compares False
        d["t_above"] = above * dt
        n_pulses, longest = _pulses(i, thr, d["i_min"], d["i_max"])
        d["n_pulses"] = n_pulses
        d["t_pulse_max"] = longest * dt
    for k, val in list(d.items()):
        if isinstance(val, float):
            d[k] = _num(val)
    return d


def _rest_values(v: np.ndarray, i: np.ndarray, thr: float) -> np.ndarray:
    """v samples where i < thr (both valid)."""
    mask = i < thr                                  # NaN -> False
    mask &= ~np.isnan(v)
    return v[mask]


def _pulses(i: np.ndarray, thr: float, i_min: float, i_max: float) -> tuple[int, int]:
    """Count rising crossings of ``thr`` with 5 % hysteresis; longest pulse in samples.

    Schmitt trigger: goes high as soon as i > thr (so every sample counted in ``t_above``
    belongs to a pulse), low when i < thr - h with h = 5 % of (i_max - i_min). Samples in the
    dead band and NaN keep the state.
    """
    h = 0.05 * (i_max - i_min)
    hi = i > thr
    lo = i < thr - h
    ev = np.flatnonzero(hi | lo)
    if ev.size == 0:
        return 0, 0
    state = hi[ev]                                   # True = high, at each decisive sample
    prev = np.concatenate(([False], state[:-1]))     # the state before the first event is low
    rises = ev[state & ~prev]
    falls = ev[~state & prev]
    if rises.size == 0:
        return 0, 0
    if falls.size == 0:
        ends = np.full(rises.size, len(i))
    else:
        k = np.searchsorted(falls, rises, side="right")
        ends = np.where(k < falls.size, falls[np.minimum(k, falls.size - 1)], len(i))
    return int(rises.size), int((ends - rises).max())


# ---------------------------------------------------------------------------
# Envelope for drawing
# ---------------------------------------------------------------------------

def _sig5(arr: np.ndarray) -> list:
    out = []
    for x in arr.tolist():
        out.append(None if (x != x or x in (math.inf, -math.inf)) else float(f"{x:.5g}"))
    return out


def envelope(cap: AnalysisCapture, t0: float, t1: float, width: int) -> dict:
    """min/max per bucket for v, i, p over [t0, t1]; the samples themselves if few."""
    width = int(max(1, min(width, 4000)))
    a, b = window_indices(cap, t0, t1)
    if b < a:
        return {"mode": "empty", "n": 0, "t_a": t0, "t_b": t1, "width": width}
    n_w = b - a + 1
    t_a = cap.t0 + a * cap.dt
    t_b = cap.t0 + b * cap.dt
    if n_w <= width:
        sl = slice(a, b + 1)
        v, i, p = cap.v[sl], cap.i[sl], cap.p[sl]
        t = (t_a + np.arange(n_w) * cap.dt)
        return {
            "mode": "samples", "n": n_w, "t_a": t_a, "t_b": t_b, "width": width,
            "t": [float(f"{x:.12g}") for x in t.tolist()],
            "v": _sig5(v), "i": _sig5(i), "p": _sig5(p),
        }
    starts = (np.arange(width, dtype=np.int64) * n_w) // width
    sl = slice(a, b + 1)
    out = {"mode": "envelope", "n": n_w, "t_a": t_a, "t_b": t_b, "width": width}
    for name, arr in (("v", cap.v), ("i", cap.i), ("p", cap.p)):
        seg = arr[sl]
        out[f"{name}_min"] = _sig5(np.fmin.reduceat(seg, starts))
        out[f"{name}_max"] = _sig5(np.fmax.reduceat(seg, starts))
    return out


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def _pre_to_dict(p: WaveformPreamble) -> dict:
    return {k: getattr(p, k) for k in
            ("fmt", "typ", "points", "count", "xinc", "xorig", "xref", "yinc", "yorig", "yref")}


def save_npz(cap: AnalysisCapture, path_or_file) -> None:
    """Compressed .npz. Stores the raw bytes + preambles when known (bit-exact reload)."""
    meta = {
        "version": NPZ_VERSION, "id": cap.id, "ts": cap.ts, "source": cap.source,
        "v_ch": cap.v_ch, "i_ch": cap.i_ch, "t0": cap.t0, "dt": cap.dt, "n": cap.n,
        "scope_params": cap.scope_params, "meta": cap.meta,
    }
    arrays: dict = {}
    if cap.raw_v is not None and cap.raw_i is not None and cap.pre_v and cap.pre_i:
        arrays["raw_v"] = cap.raw_v
        arrays["raw_i"] = cap.raw_i
        meta["pre_v"] = _pre_to_dict(cap.pre_v)
        meta["pre_i"] = _pre_to_dict(cap.pre_i)
    else:
        arrays["v"] = cap.v
        arrays["i"] = cap.i
    np.savez_compressed(path_or_file, meta_json=np.array(json.dumps(meta, allow_nan=True)), **arrays)


def load_npz(path_or_file) -> AnalysisCapture:
    try:
        z = np.load(path_or_file, allow_pickle=False)
    except Exception as exc:
        raise AnalysisError(f"Not a valid .npz file: {exc}") from exc
    with z:
        names = set(z.files)
        if "meta_json" not in names:
            raise AnalysisError("This .npz was not saved by this app (no meta_json)")
        try:
            meta = json.loads(str(z["meta_json"]))
        except (ValueError, TypeError) as exc:
            raise AnalysisError(f"Corrupt metadata in .npz: {exc}") from exc
        if not isinstance(meta, dict) or meta.get("version") != NPZ_VERSION:
            raise AnalysisError(
                f"Unsupported .npz version {meta.get('version') if isinstance(meta, dict) else '?'}"
            )
        try:
            if {"raw_v", "raw_i"} <= names:
                pv = WaveformPreamble(**meta["pre_v"])
                pi_ = WaveformPreamble(**meta["pre_i"])
                raw_v = np.asarray(z["raw_v"], dtype=np.uint8)
                raw_i = np.asarray(z["raw_i"], dtype=np.uint8)
                cap = _build(meta["id"], meta["ts"], "npz", meta["v_ch"], meta["i_ch"],
                             meta["t0"], meta["dt"], raw_v, raw_i, pv, pi_,
                             meta.get("scope_params"), meta.get("meta"))
            elif {"v", "i"} <= names:
                cap = from_arrays(meta["id"], meta["ts"], "npz", meta["v_ch"], meta["i_ch"],
                                  meta["t0"], meta["dt"], z["v"], z["i"],
                                  meta.get("scope_params"), meta.get("meta"))
            else:
                raise AnalysisError("The .npz has neither raw_v/raw_i nor v/i arrays")
        except KeyError as exc:
            raise AnalysisError(f"The .npz is missing field {exc}") from exc
        except (TypeError, ValueError) as exc:
            raise AnalysisError(f"Malformed data in .npz: {exc}") from exc
    if cap.n < 2 or not (cap.dt > 0):
        raise AnalysisError("The .npz has no usable samples (n < 2 or dt <= 0)")
    if cap.v.ndim != 1 or cap.i.ndim != 1:
        raise AnalysisError("The .npz arrays must be one-dimensional")
    if type(cap.v_ch) is not int or type(cap.i_ch) is not int or not (
            1 <= cap.v_ch <= 4 and 1 <= cap.i_ch <= 4):
        raise AnalysisError("The .npz has invalid voltage/current channel numbers")
    cap.meta = dict(cap.meta)
    cap.meta["orig_source"] = meta.get("source")
    return cap


@dataclass
class ParsedCSV:
    """Channels of the app's 1200-point CSV export (or a window export)."""
    channels: dict[int, tuple[np.ndarray, np.ndarray]]    # ch -> (t, volts)
    kind: str = "app"                                      # "app" | "window"


def parse_app_csv(text: str) -> ParsedCSV:
    """Parse ``Channel,Sample,Raw,Volts,Time_s`` (also the ``t_s,v,i,p`` window export)."""
    rdr = csv.reader(io.StringIO(text))
    try:
        header = next(rdr)
    except StopIteration:
        raise AnalysisError("The CSV file is empty") from None
    head = [h.strip().lower() for h in header]
    if head[:5] == ["channel", "sample", "raw", "volts", "time_s"]:
        per: dict[int, tuple[list, list]] = {}
        for ln, row in enumerate(rdr, start=2):
            if not row:
                continue
            try:
                ch = int(row[0].strip().upper().removeprefix("CH"))
                t = float(row[4])
                volts = float(row[3]) if row[3].strip() else math.nan
            except (ValueError, IndexError):
                raise AnalysisError(f"CSV line {ln} is malformed: {row!r}") from None
            ts, vs = per.setdefault(ch, ([], []))
            ts.append(t)
            vs.append(volts)
        if not per:
            raise AnalysisError("The CSV has a header but no data rows")
        return ParsedCSV(
            {ch: (np.asarray(t, dtype=np.float64), np.asarray(v, dtype=np.float64))
             for ch, (t, v) in per.items()}, "app")
    if head[:4] == ["t_s", "v", "i", "p"]:
        ts, vs, is_ = [], [], []
        for ln, row in enumerate(rdr, start=2):
            if not row:
                continue
            try:
                ts.append(float(row[0]))
                vs.append(float(row[1]) if row[1].strip() else math.nan)
                is_.append(float(row[2]) if row[2].strip() else math.nan)
            except (ValueError, IndexError):
                raise AnalysisError(f"CSV line {ln} is malformed: {row!r}") from None
        if not ts:
            raise AnalysisError("The CSV has a header but no data rows")
        t = np.asarray(ts, dtype=np.float64)
        return ParsedCSV({0: (t, np.asarray(vs)), 1: (t, np.asarray(is_))}, "window")
    raise AnalysisError(
        "Unrecognised CSV header; expected 'Channel,Sample,Raw,Volts,Time_s' as saved by this app"
    )


def capture_from_csv(parsed: ParsedCSV, v_ch: int, i_ch: int, id_: int = 0,
                     ts: float = 0.0, name: str = "") -> AnalysisCapture:
    if parsed.kind == "window":
        (t, v), (_, i) = parsed.channels[0], parsed.channels[1]
        v_ch_used, i_ch_used = v_ch, i_ch
    else:
        present = sorted(parsed.channels)
        missing = [c for c in (v_ch, i_ch) if c not in parsed.channels]
        if missing:
            raise ChannelsMissing(
                f"CSV has channels {present}; CH{missing[0]} is missing", present)
        (t, v), (t2, i) = parsed.channels[v_ch], parsed.channels[i_ch]
        if len(t) != len(t2):
            raise AnalysisError(
                f"CH{v_ch} has {len(t)} samples but CH{i_ch} has {len(t2)}")
        v_ch_used, i_ch_used = v_ch, i_ch
    n = len(t)
    if n < 2:
        raise AnalysisError("The CSV has fewer than 2 samples")
    # dt from the span (robust to few-digit rounding of each time value)
    dt = float(t[-1] - t[0]) / (n - 1)
    if not (dt > 0):
        raise AnalysisError("Time column is not increasing")
    expected = t[0] + dt * np.arange(n)
    if np.max(np.abs(t - expected)) > 1e-3 * dt + 1e-6 * float(np.max(np.abs(t))):
        raise AnalysisError("Time column is not uniformly sampled")
    meta = {"file": name, "channels": sorted(parsed.channels) if parsed.kind == "app" else []}
    return from_arrays(id_, ts, "csv", v_ch_used, i_ch_used, float(t[0]), dt, v, i, {}, meta)


def load_app_csv(text: str, v_ch: int, i_ch: int, **kw) -> AnalysisCapture:
    return capture_from_csv(parse_app_csv(text), v_ch, i_ch, **kw)


def export_window_csv_iter(cap: AnalysisCapture, t0: float, t1: float) -> Iterator[str]:
    """``t_s,v,i,p`` rows of the window. Refuses windows above 2 M samples (no silent decimation)."""
    a, b = window_indices(cap, t0, t1)
    n = b - a + 1
    if n > MAX_CSV_EXPORT:
        raise WindowTooLarge(
            f"Window has {n:,} samples; CSV export is limited to {MAX_CSV_EXPORT:,}. "
            "Narrow the window.")
    yield "t_s,v,i,p\n"
    if n <= 0:
        return
    for k0, k1 in _blocks(a, b):
        idx = np.arange(k0, k1, dtype=np.float64)
        t = cap.t0 + idx * cap.dt
        v = cap.v[k0:k1].astype(np.float64)
        i = cap.i[k0:k1].astype(np.float64)
        arr = np.column_stack((t, v, i, v * i))
        buf = io.StringIO()
        np.savetxt(buf, arr, fmt=["%.9e", "%.6e", "%.6e", "%.6e"], delimiter=",")
        yield buf.getvalue().replace("nan", "")


def export_window_csv(cap: AnalysisCapture, t0: float, t1: float) -> str:
    return "".join(export_window_csv_iter(cap, t0, t1))

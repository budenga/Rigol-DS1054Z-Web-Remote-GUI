"""Waveform data decoding and voltage conversion for DS1054Z."""

import struct
from dataclasses import dataclass


@dataclass
class WaveformPreamble:
    fmt: int        # 0=BYTE, 1=WORD, 2=ASCii
    typ: int        # 0=NORMal, 1=MAXimum, 2=RAW
    points: int     # number of data points
    count: int      # averaging count
    xinc: float     # time increment per sample (s)
    xorig: float    # time of first sample (s)
    xref: int       # reference time index
    yinc: float     # voltage increment per ADC count (V)
    yorig: float    # voltage origin (V)
    yref: int       # ADC count at 0V reference


def parse_preamble(response: str) -> WaveformPreamble:
    """Parse the response from :WAVeform:PREamble? into a WaveformPreamble."""
    parts = response.split(",")
    return WaveformPreamble(
        fmt=int(parts[0]),
        typ=int(parts[1]),
        points=int(parts[2]),
        count=int(parts[3]),
        xinc=float(parts[4]),
        xorig=float(parts[5]),
        xref=int(float(parts[6])),
        yinc=float(parts[7]),
        yorig=float(parts[8]),
        yref=int(float(parts[9])),
    )


def raw_to_volts(raw_bytes: bytes, pre: WaveformPreamble) -> list[float]:
    """Convert raw BYTE-format samples to voltage values.

    Values of 0 or 255 (clipped) are replaced with NaN.
    """
    nan = float("nan")
    samples = struct.unpack(f"{len(raw_bytes)}B", raw_bytes)
    result = []
    for val in samples:
        if val == 0 or val == 255:
            result.append(nan)
        else:
            result.append((val - pre.yorig - pre.yref) * pre.yinc)
    return result


def time_values(pre: WaveformPreamble) -> list[float]:
    """Generate time axis values from the preamble."""
    return [pre.xorig + i * pre.xinc for i in range(pre.points)]


def preamble_to_bytes(pre: WaveformPreamble) -> bytes:
    """Serialize preamble to 40 bytes for binary WebSocket frame.

    Format: 2 doubles (xinc, xorig) + 1 int (xref) +
            2 doubles (yinc, yorig) + 1 int (yref) + 1 int (points)
    = 8+8+4 + 8+8+4 + 4 = 44 bytes
    """
    return struct.pack(
        "<ddiddii",
        pre.xinc, pre.xorig, pre.xref,
        pre.yinc, pre.yorig, pre.yref,
        pre.points,
    )


PREAMBLE_STRUCT_SIZE = struct.calcsize("<ddiddii")  # 44 bytes


def bytes_to_preamble_partial(data: bytes) -> WaveformPreamble:
    """Deserialize preamble from binary WebSocket frame (44 bytes)."""
    xinc, xorig, xref, yinc, yorig, yref, points = struct.unpack(
        "<ddiddii", data[:PREAMBLE_STRUCT_SIZE]
    )
    return WaveformPreamble(
        fmt=0, typ=0, points=points, count=1,
        xinc=xinc, xorig=xorig, xref=xref,
        yinc=yinc, yorig=yorig, yref=yref,
    )

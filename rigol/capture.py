"""Plain data container for one screen (NORM, 1200-point) capture.

No asyncio and no numpy here so that both the acquisition manager and the
analysis engine can import it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .waveform import WaveformPreamble


@dataclass
class ScreenCapture:
    id: int
    ts: float                     # unix time
    mode: str                     # single | repeat | free
    coherent: bool                # all channels from the same acquisition
    channels: list[int]
    data: dict[int, tuple[bytes, WaveformPreamble]]
    srate: float = 0.0
    mdep: str = ""
    tdiv: float = 0.0
    toff: float = 0.0
    scope_params: dict[str, float | None] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "ts": self.ts,
            "mode": self.mode,
            "coherent": self.coherent,
            "channels": list(self.channels),
            "srate": self.srate,
            "mdep": self.mdep,
            "tdiv": self.tdiv,
            "toff": self.toff,
            "scope_params": dict(self.scope_params),
        }

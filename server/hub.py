"""Connected-client registry and broadcast helpers."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from fastapi import WebSocket

from rigol.acquisition import valid_param_spec

log = logging.getLogger(__name__)

SEND_TIMEOUT_S = 5.0


@dataclass(eq=False)
class ClientSession:
    ws: WebSocket
    params: list[str] = field(default_factory=list)       # this client's scope parameter selection
    cmd_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    pending: dict = field(default_factory=dict)            # latest analysis_window / analysis_view
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    alive: bool = True
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)   # one frame on the wire at a time

    async def send_json(self, msg: dict) -> None:
        async with self.send_lock:
            try:
                await asyncio.wait_for(self.ws.send_json(msg), SEND_TIMEOUT_S)
            except Exception:
                self.alive = False

    async def send_bytes(self, data: bytes) -> None:
        async with self.send_lock:
            try:
                await asyncio.wait_for(self.ws.send_bytes(data), SEND_TIMEOUT_S)
            except Exception:
                self.alive = False

    async def drop(self) -> None:
        """Close a client we stopped serving; the browser reconnects and resyncs everything."""
        try:
            await self.ws.close(code=1011)
        except Exception:
            pass


class ClientHub:
    """All connected sockets. Satisfies the acquisition manager's ``Sink``."""

    def __init__(self) -> None:
        self.sessions: set[ClientSession] = set()
        self._lock = asyncio.Lock()      # keep binary frames + their JSON in order

    def add(self, s: ClientSession) -> None:
        self.sessions.add(s)

    def remove(self, s: ClientSession) -> None:
        self.sessions.discard(s)

    def params_union(self) -> list[str]:
        out: dict[str, None] = {}
        for s in self.sessions:
            for p in s.params:
                if valid_param_spec(p):
                    out[p] = None
        return list(out)

    async def broadcast_json(self, msg: dict) -> None:
        async with self._lock:
            await asyncio.gather(*(s.send_json(msg) for s in list(self.sessions)))
            self._reap()

    async def broadcast_bytes(self, data: bytes) -> None:
        async with self._lock:
            await asyncio.gather(*(s.send_bytes(data) for s in list(self.sessions)))
            self._reap()

    def _reap(self) -> None:
        for s in [s for s in self.sessions if not s.alive]:
            self.sessions.discard(s)
            s.wake.set()
            asyncio.create_task(s.drop())

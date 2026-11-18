"""SCPI command scheduler — serializes all scope communication through a single queue.

The DS1054Z only handles one SCPI session at a time. This scheduler ensures
all commands (waveform reads, state polls, user controls) are serialized
through a single asyncio coroutine.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from .transport import RawTCPTransport, TransportError

log = logging.getLogger(__name__)


class Priority(IntEnum):
    CONTROL = 0       # user button presses — jump the queue
    STATE = 5         # scope state polling
    WAVEFORM = 10     # streaming waveform reads
    MEASURE = 15      # measurement polling
    SCREENSHOT = 20   # screenshot capture (slowest)


class CmdType(IntEnum):
    WRITE = 0         # send, no response
    QUERY = 1         # send, read text
    QUERY_BINARY = 2  # send, read binary block
    QUERY_WAVEFORM = 3  # atomic NORM waveform: setup + preamble + data -> (bytes, str)
    PREAMBLE_RAW = 4    # RAW-mode preamble; args = (channel,)
    WAVEFORM_RAW_CHUNK = 5  # RAW chunk; args = (channel, start, stop)


@dataclass(order=True)
class CommandTask:
    priority: int
    seq: int = field(compare=True)
    cmd: str = field(compare=False)
    cmd_type: CmdType = field(compare=False, default=CmdType.QUERY)
    args: tuple = field(compare=False, default=())
    future: asyncio.Future = field(compare=False, default_factory=lambda: asyncio.get_event_loop().create_future())


class SCPIScheduler:
    def __init__(self, transport: RawTCPTransport):
        self._transport = transport
        self._queue: asyncio.PriorityQueue[CommandTask] = asyncio.PriorityQueue()
        self._seq = 0
        self._drain_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._running = False

    @property
    def transport(self) -> RawTCPTransport:
        return self._transport

    def start(self) -> None:
        self._running = True
        self._drain_task = asyncio.create_task(self._drain_loop())

    async def stop(self) -> None:
        self._running = False
        if self._reconnect_task:
            self._reconnect_task.cancel()
            await asyncio.gather(self._reconnect_task, return_exceptions=True)
            self._reconnect_task = None
        if self._drain_task:
            self._drain_task.cancel()
            try:
                await self._drain_task
            except asyncio.CancelledError:
                pass
            self._drain_task = None

    async def submit(
        self, cmd: str, priority: Priority = Priority.STATE,
        cmd_type: CmdType = CmdType.QUERY, args: tuple = (),
    ) -> Any:
        """Submit a command and await its result."""
        loop = asyncio.get_running_loop()
        self._seq += 1
        task = CommandTask(
            priority=priority,
            seq=self._seq,
            cmd=cmd,
            cmd_type=cmd_type,
            args=args,
            future=loop.create_future(),
        )
        await self._queue.put(task)
        return await task.future

    def submit_fire_and_forget(
        self, cmd: str, priority: Priority = Priority.CONTROL,
    ) -> None:
        """Submit a write command without waiting for result."""
        loop = asyncio.get_running_loop()
        self._seq += 1
        task = CommandTask(
            priority=priority,
            seq=self._seq,
            cmd=cmd,
            cmd_type=CmdType.WRITE,
            future=loop.create_future(),
        )
        self._queue.put_nowait(task)

    def _ensure_reconnect(self) -> None:
        if self._reconnect_task is None or self._reconnect_task.done():
            self._reconnect_task = asyncio.create_task(self._reconnect())

    async def _reconnect(self) -> None:
        try:
            await self._transport.reconnect()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.error("Reconnect failed")

    async def _drain_loop(self) -> None:
        """Single consumer loop — processes one command at a time."""
        while self._running:
            try:
                task = await self._queue.get()
            except asyncio.CancelledError:
                return

            if not self._transport.connected:
                # link is down: do not park commands behind the reconnect (they would replay later)
                if not task.future.done():
                    task.future.set_exception(TransportError("Scope not connected", fatal=True))
                self._ensure_reconnect()
                continue
            try:
                if task.cmd_type == CmdType.WRITE:
                    await self._transport.write(task.cmd)
                    if not task.future.done():
                        task.future.set_result(None)
                elif task.cmd_type == CmdType.QUERY:
                    result = await self._transport.query(task.cmd)
                    if not task.future.done():
                        task.future.set_result(result)
                elif task.cmd_type == CmdType.QUERY_BINARY:
                    result = await self._transport.query_binary(task.cmd)
                    if not task.future.done():
                        task.future.set_result(result)
                elif task.cmd_type == CmdType.QUERY_WAVEFORM:
                    # cmd contains the channel number as a string
                    result = await self._transport.query_waveform(int(task.cmd))
                    if not task.future.done():
                        task.future.set_result(result)
                elif task.cmd_type == CmdType.PREAMBLE_RAW:
                    result = await self._transport.query_preamble_raw(*task.args)
                    if not task.future.done():
                        task.future.set_result(result)
                elif task.cmd_type == CmdType.WAVEFORM_RAW_CHUNK:
                    result = await self._transport.query_waveform_raw_chunk(*task.args)
                    if not task.future.done():
                        task.future.set_result(result)
            except TransportError as exc:
                log.error("Command '%s' failed: %s", task.cmd, exc)
                if not task.future.done():
                    task.future.set_exception(exc)
                # Re-open the link only if it is actually gone; a protocol-level
                # error (e.g. the scope answered "ERROR" to a RAW read) leaves the
                # stream in sync.
                if not self._transport.connected:
                    self._ensure_reconnect()
            except asyncio.CancelledError:
                if not task.future.done():
                    task.future.cancel()
                return
            except Exception as exc:  # never let one bad command kill the queue
                log.exception("Command '%s' crashed", task.cmd)
                if not task.future.done():
                    task.future.set_exception(TransportError(str(exc)))

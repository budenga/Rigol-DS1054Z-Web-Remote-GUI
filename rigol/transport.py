"""Raw TCP SCPI transport for Rigol DS1054Z (port 5555).

Uses a SINGLE persistent connection for all commands (text and binary).
After binary block reads, trailing garbage bytes (\\n) are flushed.

The DS1054Z does NOT reliably handle two simultaneous TCP connections,
so the earlier approach of disposable sockets for binary reads caused
timeouts and response desynchronization on the persistent socket.
"""

import asyncio
import logging
import time

log = logging.getLogger(__name__)

SCPI_PORT = 5555
READ_TIMEOUT = 5.0
RAW_CHUNK_TIMEOUT = 30.0   # a 250 kB RAW chunk can take a while on a busy scope
DRAIN_TIMEOUT = 0.005      # trailing-byte drain after a binary block (see _flush_trailing)
CONNECT_TIMEOUT = 3.0
MAX_RECONNECT_DELAY = 30.0
_JUNK = " \t\r\n\x00"


class TransportError(Exception):
    """Communication failure. ``fatal`` means the TCP link must be re-opened."""

    def __init__(self, msg: str = "", fatal: bool = False):
        super().__init__(msg)
        self.fatal = fatal


class RawTCPTransport:
    def __init__(self, host: str, port: int = SCPI_PORT):
        self.host = host
        self.port = port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port),
                timeout=CONNECT_TIMEOUT,
            )
            self._connected = True
            log.info("Connected to %s:%d", self.host, self.port)
        except (OSError, asyncio.TimeoutError) as exc:
            self._connected = False
            raise TransportError(f"Cannot connect to {self.host}:{self.port}: {exc}") from exc

    async def disconnect(self) -> None:
        self._connected = False
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except OSError:
                pass
            self._writer = None
            self._reader = None
        log.info("Disconnected from %s:%d", self.host, self.port)

    async def reconnect(self) -> None:
        delay = 1.0
        while True:
            await self.disconnect()
            try:
                await self.connect()
                return
            except TransportError:
                log.warning("Reconnect failed, retrying in %.0fs...", delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, MAX_RECONNECT_DELAY)

    # --- Text commands ---

    async def write(self, cmd: str) -> None:
        """Send a command with no response expected."""
        async with self._lock:
            await self._send(cmd)
            log.debug("WRITE: %s", cmd)
            # Let the scope process the command
            await asyncio.sleep(0.05)

    async def query(self, cmd: str) -> str:
        """Send a command and read a text response (terminated by \\n)."""
        async with self._lock:
            await self._send(cmd)
            resp = await self._read_line()
            log.debug("QUERY: %s → %s", cmd, resp[:200] if resp else "")
            return resp

    async def _send(self, cmd: str) -> None:
        if not self._connected or not self._writer:
            raise TransportError("Not connected", fatal=True)
        try:
            self._writer.write((cmd + "\n").encode())
            await self._writer.drain()
        except OSError as exc:
            self._connected = False
            raise TransportError(f"Send failed: {exc}", fatal=True) from exc

    async def _read_line(self) -> str:
        if not self._reader:
            raise TransportError("Not connected", fatal=True)
        try:
            # The DS1054Z sometimes leaves a stray "\n" behind a binary block that
            # the short drain missed; skip blank lines so it cannot be mistaken
            # for the reply to the next query.
            for _ in range(4):
                data = await asyncio.wait_for(
                    self._reader.readline(), timeout=READ_TIMEOUT
                )
                if not data:
                    self._connected = False
                    raise TransportError("Connection closed by scope", fatal=True)
                text = data.decode(errors="replace").strip(_JUNK)
                if text:
                    return text
            return ""
        except asyncio.TimeoutError as exc:
            self._connected = False
            raise TransportError("Read timeout", fatal=True) from exc
        except OSError as exc:
            self._connected = False
            raise TransportError(f"Read failed: {exc}", fatal=True) from exc

    # --- Binary block commands (on the SAME persistent connection) ---

    async def _read_block(self, timeout: float) -> bytes:
        """Read one IEEE 488.2 definite-length block (``#<n><len><payload>``).

        Must be called with ``self._lock`` held and the command already sent.
        Drains the trailing bytes afterwards.
        """
        header_hash = await asyncio.wait_for(
            self._reader.readexactly(1), timeout=timeout
        )
        # Stray bytes from the previous block that the drain missed.
        for _ in range(8):
            if header_hash not in (b"\n", b"\r", b"\x00"):
                break
            header_hash = await asyncio.wait_for(
                self._reader.readexactly(1), timeout=timeout
            )
        if header_hash != b"#":
            # Not a binary block (the scope answered with an error string):
            # consume the rest of the line so the stream stays in sync.
            rest = await asyncio.wait_for(self._reader.readline(), timeout=timeout)
            raise TransportError(
                f"Expected '#', got {(header_hash + rest).decode(errors='replace').strip()!r}"
            )
        n_digits_byte = await asyncio.wait_for(
            self._reader.readexactly(1), timeout=timeout
        )
        n_digits = int(n_digits_byte)
        if n_digits == 0:
            raise TransportError("Indefinite-length block not supported", fatal=True)
        length_str = await asyncio.wait_for(
            self._reader.readexactly(n_digits), timeout=timeout
        )
        data_length = int(length_str)
        log.debug("BLOCK header: %d bytes expected", data_length)
        payload = await asyncio.wait_for(
            self._reader.readexactly(data_length), timeout=timeout
        )
        await self._flush_trailing()
        return payload

    async def _locked_exchange(self, send: str, timeout: float, what: str, reader):
        """Run ``send`` + ``reader`` under the lock with uniform error mapping."""
        async with self._lock:
            if not self._connected or not self._reader or not self._writer:
                raise TransportError("Not connected", fatal=True)
            t0 = time.monotonic()
            log.debug("SEND %s: %s", what, send.strip().replace("\n", " | "))
            try:
                self._writer.write(send.encode())
                await self._writer.drain()
                result = await reader(timeout)
                log.debug("DONE %s in %.3fs", what, time.monotonic() - t0)
                return result
            except asyncio.TimeoutError as exc:
                self._connected = False
                raise TransportError(f"{what} timeout", fatal=True) from exc
            except (OSError, asyncio.IncompleteReadError, ValueError) as exc:
                self._connected = False
                raise TransportError(f"{what} failed: {exc}", fatal=True) from exc

    async def query_binary(self, cmd: str, timeout: float = READ_TIMEOUT) -> bytes:
        """Send a command and read an IEEE 488.2 binary block on the persistent connection."""
        return await self._locked_exchange(
            cmd + "\n", timeout, "Binary read", self._read_block
        )

    async def query_waveform(
        self, channel: int, timeout: float = READ_TIMEOUT
    ) -> tuple[bytes, str]:
        """Atomic NORM waveform read: setup + preamble + data in one lock acquisition.

        Returns ``(payload, preamble_string)``. The preamble is requested *before*
        the data so that the binary block is the last thing on the wire and the
        trailing-byte drain cannot swallow the start of a text reply.

        Only ONE query per write: when ``:WAV:PRE?`` and ``:WAV:DATA?`` arrive in the
        same write, the DS1054Z (fw 00.04.04.SP4, verified on hardware) answers with a
        single compound reply ``<preamble>;#9...<block>`` and no newline after the
        preamble, so a line read never ends. Setters may share a write.
        """
        send = (
            f":WAV:SOUR CHAN{channel}\n:WAV:MODE NORM\n:WAV:FORM BYTE\n"
            ":WAV:STAR 1\n:WAV:STOP 1200\n"      # a previous RAW chunk must not leave its range behind
            ":WAV:PRE?\n"
        )

        async def reader(t: float) -> tuple[bytes, str]:
            pre = await self._read_line_timeout(t)
            self._writer.write(b":WAV:DATA?\n")
            await self._writer.drain()
            payload = await self._read_block(t)
            return payload, pre

        return await self._locked_exchange(send, timeout, "Waveform read", reader)

    async def query_preamble_raw(
        self, channel: int, timeout: float = READ_TIMEOUT
    ) -> str:
        """Select RAW mode on ``channel`` and read ``:WAV:PRE?`` (RAW YINC/YORIG/YREF)."""
        send = f":WAV:SOUR CHAN{channel}\n:WAV:MODE RAW\n:WAV:FORM BYTE\n:WAV:PRE?\n"
        return await self._locked_exchange(
            send, timeout, "RAW preamble read", self._read_line_timeout
        )

    async def query_waveform_raw_chunk(
        self, channel: int, start: int, stop: int, timeout: float = RAW_CHUNK_TIMEOUT
    ) -> bytes:
        """Read memory points ``start..stop`` (1-based, inclusive) of ``channel`` in RAW mode."""
        send = (
            f":WAV:SOUR CHAN{channel}\n:WAV:MODE RAW\n:WAV:FORM BYTE\n"
            f":WAV:STAR {start}\n:WAV:STOP {stop}\n:WAV:DATA?\n"
        )
        return await self._locked_exchange(
            send, timeout, "RAW chunk read", self._read_block
        )

    async def _read_line_timeout(self, timeout: float) -> str:
        data = await asyncio.wait_for(self._reader.readline(), timeout=timeout)
        if not data:
            raise OSError("connection closed by scope")
        text = data.decode(errors="replace").strip(_JUNK)
        if not text:  # stray blank line: read the real one
            return await self._read_line_timeout(timeout)
        return text

    async def _flush_trailing(self) -> None:
        """Discard the trailing bytes the DS1054Z sends after a binary block.

        The firmware appends ``\\n`` (sometimes ``\\n\\n\\x00``) after the payload, and
        left in the buffer they would corrupt the next reply. We must not wait a
        fixed 100 ms every time, so drain with a short per-read timeout and stop at
        the first quiet gap (or EOF).
        """
        total = 0
        while True:
            try:
                trailing = await asyncio.wait_for(
                    self._reader.read(16), timeout=DRAIN_TIMEOUT
                )
            except (asyncio.TimeoutError, OSError):
                break
            if not trailing:
                break
            total += len(trailing)
        if total:
            log.debug("Drained %d trailing bytes", total)

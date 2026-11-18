"""Shared helpers: a scope stack wired to the fake scope on an ephemeral port."""

import asyncio
from contextlib import asynccontextmanager

from rigol.fake_scope import FakeScope
from rigol.scheduler import SCPIScheduler
from rigol.scope import DS1054Z
from rigol.transport import RawTCPTransport


@asynccontextmanager
async def scope_stack(**fake_kw):
    fake_kw.setdefault("wait_range", (0.02, 0.05))
    fake = FakeScope(port=0, **fake_kw)
    await fake.start()
    transport = RawTCPTransport("127.0.0.1", fake.port)
    await transport.connect()
    sched = SCPIScheduler(transport)
    sched.start()
    scope = DS1054Z(sched)
    await scope.get_full_state()
    try:
        yield fake, scope
    finally:
        await sched.stop()
        await transport.disconnect()
        await fake.stop()


async def stop_capture(scope: DS1054Z, timeout: float = 5.0) -> None:
    """:SING and wait until the fake reports STOP."""
    await scope.arm_single()
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if await scope.get_trigger_status() == "STOP":
            return
        await asyncio.sleep(0.02)
    raise AssertionError("scope never reached STOP")


class Sink:
    """Collects what the acquisition manager broadcasts."""

    def __init__(self):
        self.json: list[dict] = []
        self.binary: list[bytes] = []

    async def broadcast_json(self, msg):
        self.json.append(msg)

    async def broadcast_bytes(self, data):
        self.binary.append(data)

    def of_type(self, t):
        return [m for m in self.json if m.get("type") == t]

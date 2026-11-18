"""Transport behaviour that depends on how the real DS1054Z parses program messages."""

import asyncio

from rigol.fake_scope import FakeScope
from rigol.transport import RawTCPTransport
from tests.helpers import scope_stack


def run(coro):
    return asyncio.run(coro)


def test_fake_merges_queries_sent_in_one_write():
    """Hardware fact (fw 00.04.04.SP4): two queries in one write -> one ';'-joined reply."""
    async def go():
        fake = FakeScope(port=0)
        await fake.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", fake.port)
            writer.write(b":WAV:SOUR CHAN1\n:WAV:MODE NORM\n:WAV:FORM BYTE\n:WAV:PRE?\n:WAV:DATA?\n")
            await writer.drain()
            head = await asyncio.wait_for(reader.readuntil(b"#"), timeout=2)
            assert head.endswith(b";#")                  # preamble has no newline, just ';'
            assert b"\n" not in head
            writer.close()
        finally:
            await fake.stop()
    run(go())


def test_fake_answers_separate_writes_separately():
    async def go():
        fake = FakeScope(port=0)
        await fake.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", fake.port)
            writer.write(b"*IDN?\n")
            await writer.drain()
            idn = await asyncio.wait_for(reader.readline(), timeout=2)
            assert idn.startswith(b"RIGOL") and idn.endswith(b"\n") and b";" not in idn
            writer.close()
        finally:
            await fake.stop()
    run(go())


def test_query_waveform_against_merging_fake():
    """Regression: the old pipelined PRE?+DATA? write timed out on the real scope."""
    async def go():
        async with scope_stack() as (fake, scope):
            for ch in (1, 4):
                payload, pre = await asyncio.wait_for(scope.get_waveform(ch), timeout=3)
                assert len(payload) == 1200
                assert pre.points == 1200
    run(go())


def test_transport_never_pipelines_two_queries():
    """Every write the transport sends contains at most one query."""
    async def go():
        fake = FakeScope(port=0)
        await fake.start()
        sent: list[bytes] = []
        t = RawTCPTransport("127.0.0.1", fake.port)
        await t.connect()
        orig = t._writer.write
        t._writer.write = lambda b: (sent.append(b), orig(b))[1]
        try:
            await t.query_waveform(1)
            await t.query("*IDN?")
            await t.write(":STOP")
            await t.query_preamble_raw(1)
            await t.query_waveform_raw_chunk(1, 1, 1000)
        finally:
            await t.disconnect()
            await fake.stop()
        for b in sent:
            n = sum(1 for c in b.decode().split("\n") if c.strip().endswith("?"))
            assert n <= 1, f"write with {n} queries: {b!r}"
    run(go())

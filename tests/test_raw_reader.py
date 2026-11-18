"""RAW memory reader against the fake scope (chunking, limits, cancel, errors)."""

import asyncio

import numpy as np
import pytest

from rigol.raw_reader import RawCancelled, RawReadError, read_raw
from rigol.transport import TransportError
from tests.helpers import scope_stack, stop_capture


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("mdep, chunks_per_ch", [(600_000, 3), (6_000_000, 24)])
def test_chunking_and_exact_data(mdep, chunks_per_ch):
    async def go():
        async with scope_stack(trailing="random") as (fake, scope):
            await scope.set_memory_depth(mdep)
            assert int(await scope.get_memory_depth()) == mdep
            await stop_capture(scope)
            progress = []
            res = await read_raw(scope, [1, 4], mdep, lambda *a: progress.append(a))
            assert res.n == mdep and res.chunks == 2 * chunks_per_ch
            assert len(progress) == res.chunks
            assert progress[-1][1] == mdep and progress[-1][3] == pytest.approx(0, abs=1e-6)
            for ch in (1, 4):
                assert res.raw[ch].dtype == np.uint8 and len(res.raw[ch]) == mdep
                assert np.array_equal(res.raw[ch], fake.expected_raw(ch, raw=True))
            # RAW preamble was read in RAW mode: differs from NORM (yref 125 vs 127)
            assert res.preambles[1].yref == 125 and res.preambles[1].typ == 2
            assert res.srate == pytest.approx(mdep / 1.2)
            assert res.bytes_per_s > 0
    run(go())


def test_raw_requires_stop_and_reader_stops_scope():
    async def go():
        async with scope_stack() as (fake, scope):
            # scope is running: the fake refuses RAW data...
            with pytest.raises(TransportError, match="Expected '#'"):
                await scope.get_raw_chunk(1, 1, 100)
            assert scope._sched.transport.connected          # stream still in sync
            # ...but read_raw stops the scope itself first
            res = await read_raw(scope, [1, 4])
            assert fake.mode == "STOP" and res.n == 600_000
    run(go())


def test_chunk_limit_enforced_by_fake():
    async def go():
        async with scope_stack() as (fake, scope):
            await stop_capture(scope)
            with pytest.raises(TransportError):
                await scope.get_raw_chunk(1, 1, 250_001)
            assert len(await scope.get_raw_chunk(1, 1, 250_000)) == 250_000
            assert len(await scope.get_raw_chunk(1, 600_000, 600_000)) == 1
    run(go())


def test_cancel_between_chunks_leaves_scope_stopped():
    async def go():
        async with scope_stack() as (fake, scope):
            await stop_capture(scope)
            ev = asyncio.Event()

            def progress(ch, done, total, eta):
                if done >= 250_000:
                    ev.set()

            with pytest.raises(RawCancelled):
                await read_raw(scope, [1, 4], None, progress, ev)
            assert fake.mode == "STOP"
            # the connection is healthy afterwards
            assert await scope.get_trigger_status() == "STOP"
    run(go())


def test_wrong_chunk_length_retries_once_then_fails():
    async def go():
        async with scope_stack() as (fake, scope):
            await stop_capture(scope)
            calls = {"n": 0}
            real = scope.get_raw_chunk

            async def short_chunk(ch, a, b):
                calls["n"] += 1
                return (await real(ch, a, b))[:-1]      # always one byte short

            scope.get_raw_chunk = short_chunk
            with pytest.raises(RawReadError, match="wrong length"):
                await read_raw(scope, [1, 4])
            assert calls["n"] == 2                       # one retry, then fail

            calls["n"] = 0

            async def flaky(ch, a, b):
                calls["n"] += 1
                data = await real(ch, a, b)
                return data[:-1] if calls["n"] == 1 else data

            scope.get_raw_chunk = flaky
            res = await read_raw(scope, [1, 4])
            assert np.array_equal(res.raw[1], fake.expected_raw(1))
    run(go())


def test_auto_memory_depth_uses_preamble_points():
    async def go():
        async with scope_stack() as (fake, scope):
            fake.mdep = "AUTO"
            await stop_capture(scope)
            res = await read_raw(scope, [1, 4])
            assert res.mdep == "AUTO" and res.n == 60_000
            assert np.array_equal(res.raw[4], fake.expected_raw(4))
    run(go())


def test_scope_timeout_mid_read_raises_clean_error_and_link_recovers():
    async def go():
        async with scope_stack() as (fake, scope):
            await stop_capture(scope)
            transport = scope._sched.transport
            real = transport.query_waveform_raw_chunk

            async def tiny_timeout(ch, a, b, timeout=0.05):
                fake.throughput = 1000                      # 1 kB/s: a chunk cannot finish
                return await real(ch, a, b, timeout=0.05)

            transport.query_waveform_raw_chunk = tiny_timeout
            with pytest.raises(RawReadError, match="timeout"):
                await read_raw(scope, [1, 4])
            transport.query_waveform_raw_chunk = real
            fake.throughput = None
            for _ in range(60):                              # scheduler re-opens the link
                if transport.connected:
                    break
                await asyncio.sleep(0.05)
            assert transport.connected
            assert await scope.get_trigger_status() == "STOP"
    run(go())

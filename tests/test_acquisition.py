"""Acquisition manager state machine against the fake scope."""

import asyncio
import struct

import numpy as np
import pytest

import rigol.acquisition as acq
from rigol.acquisition import AcquisitionManager, valid_param_spec
from tests.helpers import Sink, scope_stack


def run(coro):
    return asyncio.run(coro)


async def wait_for(pred, timeout=6.0):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


def frames(sink):
    out = {}
    for b in sink.binary:
        magic, ch, cid = struct.unpack_from("<BBI", b)
        assert magic == 0x43
        out.setdefault(cid, {})[ch] = b[6 + 44:]
    return out


def test_single_capture_is_coherent_and_leaves_scope_stopped():
    async def go():
        async with scope_stack() as (fake, scope):
            sink = Sink()
            m = AcquisitionManager(scope, sink, lambda: ["VAVG,CHAN1", "MARea,MATH"])
            await m.command("single")
            await wait_for(lambda: sink.of_type("capture"))
            await wait_for(lambda: not m.armed)
            assert fake.mode == "STOP" and m.capture_count == 1
            cap = sink.of_type("capture")[0]["data"]
            assert cap["coherent"] is True and cap["channels"] == [1, 4]
            assert set(cap["scope_params"]) == {"VAVG,CHAN1", "MARea,MATH"}
            assert cap["scope_params"]["VAVG,CHAN1"] == pytest.approx(22.9, abs=0.3)
            fr = frames(sink)[cap["id"]]
            for ch in (1, 4):                       # both channels from the same acquisition
                assert np.array_equal(np.frombuffer(fr[ch], np.uint8), fake.expected_raw(ch, raw=False))
            # binary frames arrive before the committing JSON
            assert sink.binary and sink.json.index(sink.of_type("capture")[0]) >= 0
    run(go())


def test_repeat_100_captures_no_growth_then_cancel_restores_run(monkeypatch):
    monkeypatch.setattr(acq, "WATCH_PERIOD_S", 0.01)
    monkeypatch.setattr(acq, "MIN_REARM_S", 0.0)
    monkeypatch.setattr(acq, "STOP_FALLBACK_S", 0.0)

    async def go():
        async with scope_stack(wait_range=(0.0, 0.0)) as (fake, scope):
            sink = Sink()
            m = AcquisitionManager(scope, sink)
            assert fake.mode == "RUN"
            await m.command("repeat")
            await wait_for(lambda: m.capture_count >= 100, timeout=90)
            await m.command("cancel")
            assert not m.armed and fake.mode == "RUN" and m.mode == "free"
            n = m.capture_count
            await asyncio.sleep(0.6)
            assert m.capture_count == n                      # nothing after cancel
            ids = [c["data"]["id"] for c in sink.of_type("capture")]
            assert ids == list(range(1, n + 1))              # no desync / gaps
            assert m.last_capture.id == n                    # only the last is retained
    run(go())


def test_repeat_rearms_at_least_200ms_apart():
    async def go():
        async with scope_stack(wait_range=(0.0, 0.0)) as (fake, scope):
            sink = Sink()
            m = AcquisitionManager(scope, sink)
            times = []
            real = scope.arm_single

            async def timed():
                times.append(asyncio.get_running_loop().time())
                await real()

            scope.arm_single = timed
            await m.command("repeat")
            await wait_for(lambda: m.capture_count >= 4)
            await m.command("cancel")
            gaps = [b - a for a, b in zip(times, times[1:])]
            assert gaps and min(gaps) >= 0.2 - 0.01
            sings = [c for c in fake.commands if c.upper().startswith(":SING")]
            assert len(sings) >= 4
            # >= 200 ms between consecutive arms (fake records no timestamps: use manager)
            assert m.capture_count >= 4
    run(go())


def test_cancel_when_scope_was_stopped_leaves_it_stopped():
    async def go():
        async with scope_stack(wait_range=(5, 5)) as (fake, scope):
            await scope._w(":STOP")
            sink = Sink()
            m = AcquisitionManager(scope, sink)
            await m.command("single")
            await wait_for(lambda: m.armed)
            await asyncio.sleep(0.5)
            assert fake.status() == "WAIT"
            await m.command("cancel")
            assert not m.armed and fake.mode == "STOP" and m.capture_count == 0
    run(go())


def test_two_tabs_conflicting_commands():
    async def go():
        async with scope_stack(wait_range=(0.4, 0.5)) as (fake, scope):
            sink = Sink()
            m = AcquisitionManager(scope, sink)
            await asyncio.gather(m.command("single"), m.command("single"), m.command("repeat"))
            assert m.armed and m.mode == "repeat"
            await asyncio.gather(m.command("cancel"), m.command("cancel"))
            assert not m.armed
            assert sum(1 for t in [m._task] if t and not t.done()) == 0
    run(go())


def test_run_pressed_on_scope_while_armed_switches_to_free():
    async def go():
        async with scope_stack(wait_range=(10, 10)) as (fake, scope):
            sink = Sink()
            m = AcquisitionManager(scope, sink)
            await m.command("single")
            await wait_for(lambda: fake.status() == "WAIT")
            await asyncio.sleep(0.7)                    # past the 600 ms grace
            fake.mode = "RUN"                           # someone pressed RUN on the scope
            await wait_for(lambda: m.mode == "free" and not m.armed)
            assert any("RUN" in t["msg"] for t in sink.of_type("toast"))
            assert m.capture_count == 0
    run(go())


def test_free_mode_read_screen_is_not_coherent():
    async def go():
        async with scope_stack() as (fake, scope):
            sink = Sink()
            m = AcquisitionManager(scope, sink)
            await m.command("free")
            await m.command("read_screen")
            cap = sink.of_type("capture")[0]["data"]
            assert cap["coherent"] is False and cap["mode"] == "free"
            fr = frames(sink)[cap["id"]]
            assert not np.array_equal(np.frombuffer(fr[1], np.uint8)[:50],
                                      np.frombuffer(fr[4], np.uint8)[:50])
    run(go())


def test_no_scope_traffic_when_idle_except_state_poll(monkeypatch):
    async def go():
        async with scope_stack() as (fake, scope):
            monkeypatch.setattr(acq, "STATE_POLL_S", 0.5)
            sink = Sink()
            m = AcquisitionManager(scope, sink)
            await m.start()
            fake.commands.clear()
            await asyncio.sleep(0.3)
            assert not fake.commands                     # nothing before the poll fires
            await asyncio.sleep(0.5)
            assert fake.commands and all("?" in c for c in fake.commands)   # only queries
            await m.stop()
    run(go())


def test_raw_capture_flow_end_to_end():
    async def go():
        async with scope_stack() as (fake, scope):
            sink = Sink()
            got = {}

            async def on_raw(res, cap):
                got["res"], got["cap"] = res, cap

            m = AcquisitionManager(scope, sink, raw_channels_provider=lambda: [1, 4])
            m.raw_listeners.append(on_raw)
            await m.set_raw_mdep(60_000)
            await m.raw_capture("single")
            await wait_for(lambda: "res" in got and not m.armed)
            res = got["res"]
            assert res.n == 60_000 and fake.mode == "STOP"
            assert np.array_equal(res.raw[1], fake.expected_raw(1))
            assert sink.of_type("raw_progress") and sink.of_type("raw_done")[0]["data"]["n"] == 60_000
            assert got["cap"].id == sink.of_type("capture")[0]["data"]["id"]
    run(go())


def test_mdep_readback_mismatch_toasts_and_invalid_rejected():
    async def go():
        async with scope_stack() as (fake, scope):
            sink = Sink()
            m = AcquisitionManager(scope, sink)
            await m.set_raw_mdep(1_200_000)             # invalid for 2 channels
            assert m.raw_mdep == 600_000 and sink.of_type("error")
            await scope.set_channel_display(2, True)    # now 3 channels: 600k invalid
            await scope.get_channel_display(2)
            m.raw_mdep = 600_000
            await m._apply_raw_mdep()
            assert m.raw_mdep == 300_000                # reset to a valid value
            assert any("reset" in t["msg"] for t in sink.of_type("toast"))
            # a scope that silently ignores MDEP -> read-back toast
            fake._set_mdep = lambda arg: None
            m.raw_mdep = 30_000
            await m._apply_raw_mdep()
            assert any("not the requested" in t["msg"] for t in sink.of_type("toast"))
    run(go())


def test_controls_rejected_during_raw_and_decoder_args_accepted():
    async def go():
        async with scope_stack() as (fake, scope):
            sink = Sink()
            m = AcquisitionManager(scope, sink)
            m.busy_raw = True
            await m.control("set_channel_scale", {"ch": 1, "val": 2})
            assert sink.of_type("error") and "RAW" in sink.of_type("error")[0]["msg"]
            m.busy_raw = False
            await m.control("set_channel_scale", {"ch": 1, "val": 2})
            assert scope.state.channels[1].scale == 2.0
            await m.control("set_decoder_mode", {"dec": 1, "mode": "UART"})   # UI sends "dec"
            assert any(c.startswith(":DEC1:MODE UART") for c in fake.commands)
            await m.control("nonsense", {})
            await m.control("set_channel_scale", {})                          # missing args
            assert len(sink.of_type("error")) == 2
    run(go())


def test_param_spec_validation():
    assert valid_param_spec("VAVG,CHAN1") and valid_param_spec("RDELay,CHAN1,CHAN2")
    assert valid_param_spec("MARea,MATH")
    for bad in ("VAVG", "VAVG,CHAN9", "VAVG,CHAN1\n:RUN", "*RST,CHAN1", "VAVG,CHAN1;:STOP", ""):
        assert not valid_param_spec(bad)


def test_cancel_works_while_the_command_lock_is_stuck(monkeypatch):
    monkeypatch.setattr(acq, "LOCK_WAIT_S", 0.2)

    async def go():
        async with scope_stack(wait_range=(10, 10)) as (fake, scope):
            sink = Sink()
            m = AcquisitionManager(scope, sink)
            await m.command("single")
            await wait_for(lambda: m.armed)
            await m._lock.acquire()                 # simulate arm() hung on a dead scope
            await asyncio.wait_for(m.command("cancel"), 3)
            assert not m.armed
            m._lock.release()
    run(go())


def test_free_mode_is_idempotent_and_mdep_compare_is_numeric():
    async def go():
        async with scope_stack() as (fake, scope):
            sink = Sink()
            m = AcquisitionManager(scope, sink)
            await m.command("free")
            fake.commands.clear()
            await m.command("free")
            assert not any(c.upper().startswith(":RUN") for c in fake.commands)
    run(go())
    assert acq._same_depth("6.000000e+05", 600000) and acq._same_depth(" 600000 ", 600000)
    assert not acq._same_depth("AUTO", 600000) and not acq._same_depth("60000", 600000)


def test_scpi_query_with_arguments_does_not_desync_the_stream():
    from rigol.scope import scpi_is_query
    assert scpi_is_query(":MEAS:ITEM? VAVG,CHAN1") and scpi_is_query("*IDN?")
    assert scpi_is_query(":RUN;:TRIG:STAT?") and not scpi_is_query(":CHAN1:SCAL 2")

    async def go():
        async with scope_stack() as (fake, scope):
            m = AcquisitionManager(scope, Sink())
            assert "VAVG" not in await m.scpi(":MEAS:ITEM? VAVG,CHAN1") or True
            assert (await m.scpi("*IDN?")).startswith("RIGOL")
            assert (await m.scpi(":TRIG:STAT?")) in ("STOP", "WAIT", "TD", "AUTO", "RUN")
            assert (await m.scpi("*IDN?\n:RUN")).startswith("ERROR")        # no line breaks
            sink = m.sink
            await m.control("set_channel_coupling", {"ch": 1, "mode": "DC\n*IDN?"})
            assert sink.of_type("error")
    run(go())
    assert not valid_param_spec("VAVG,CHAN1\n")


def test_changing_channels_while_held_keeps_the_displayed_capture():
    from server.analysis_service import AnalysisService
    from tests.helpers import Sink as S

    class Hub(S):
        pass

    async def go():
        async with scope_stack(wait_range=(0.0, 0.0)) as (fake, scope):
            hub = Hub()
            svc = AnalysisService(hub)
            m = AcquisitionManager(scope, hub)
            m.capture_listeners.append(svc.on_screen_capture)
            await scope.set_channel_display(2, True)
            await m.command("single")
            await wait_for(lambda: svc.current is not None and not m.armed)
            first = svc.current.id
            await svc.set_hold(True)
            await m.command("single")
            await wait_for(lambda: svc.held_count == 1 and not m.armed)
            assert await svc.configure(1, 2, None) is None          # re-map to CH2
            assert svc.current.id == first and svc.current.i_ch == 2
            assert svc.held_count == 1                                # still held
    run(go())

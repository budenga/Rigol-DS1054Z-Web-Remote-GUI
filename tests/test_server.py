"""End-to-end through FastAPI + WebSocket against the fake scope."""

import json
import struct
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

import server.app as appmod


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(appmod, "USE_FAKE", True)
    monkeypatch.setattr(appmod, "FAKE_PORT", 0)
    monkeypatch.setattr(appmod, "FAKE_WAIT", (0.05, 0.1))
    monkeypatch.setattr(appmod, "FAKE_THROUGHPUT", None)
    with TestClient(appmod.app) as c:
        yield c


def recv_until(ws, pred, timeout=15.0, collect=None):
    end = time.time() + timeout
    while time.time() < end:
        m = ws.receive()
        if m.get("bytes") is not None:
            if collect is not None:
                collect.append(m["bytes"])
            continue
        msg = json.loads(m["text"])
        if collect is not None:
            collect.append(msg)
        if pred(msg):
            return msg
    raise AssertionError("timeout waiting for message")


def settle(ws):
    """Round-trip through the (sequential) command worker so earlier messages are fully handled."""
    ws.send_text(json.dumps({"type": "scpi", "cmd": "*IDN?"}))
    recv_until(ws, lambda m: m.get("type") == "scpi_response")


def drain_initial(ws):
    types = []
    for _ in range(3):
        types.append(json.loads(ws.receive_text())["type"])
    return types


def test_single_capture_over_websocket_two_tabs_see_the_same(client):
    with client.websocket_connect("/ws") as a, client.websocket_connect("/ws") as b:
        assert drain_initial(a) == ["state", "acq_state", "analysis_state"]
        drain_initial(b)
        a.send_text(json.dumps({"type": "params", "items": ["VAVG,CHAN1", "bad;cmd"]}))
        a.send_text(json.dumps({"type": "acq", "cmd": "single"}))
        got_a, got_b = [], []
        ma = recv_until(a, lambda m: m.get("type") == "capture", collect=got_a)
        mb = recv_until(b, lambda m: m.get("type") == "capture", collect=got_b)
        assert ma["data"]["id"] == mb["data"]["id"] == 1
        assert ma["data"]["coherent"] and ma["data"]["channels"] == [1, 4]
        assert list(ma["data"]["scope_params"]) == ["VAVG,CHAN1"]       # invalid spec dropped
        frames = [x for x in got_a if isinstance(x, bytes)]
        assert len(frames) == 2
        magic, ch, cid = struct.unpack_from("<BBI", frames[0])
        assert (magic, ch, cid) == (0x43, 1, 1) and len(frames[0]) == 6 + 44 + 1200
        # analysis follows the capture
        meta = recv_until(a, lambda m: m.get("type") == "analysis_meta")
        assert meta["data"]["source"] == "screen" and meta["data"]["n"] == 1200


def test_analysis_window_and_view_and_export(client):
    with client.websocket_connect("/ws") as ws:
        drain_initial(ws)
        ws.send_text(json.dumps({"type": "acq", "cmd": "single"}))
        meta = recv_until(ws, lambda m: m.get("type") == "analysis_meta")["data"]
        ws.send_text(json.dumps({"type": "analysis_window", "req": 7,
                                 "t0": meta["t_end"], "t1": meta["t_start"]}))   # swapped
        st = recv_until(ws, lambda m: m.get("type") == "analysis_stats")["data"]
        assert st["req"] == 7 and st["stats"]["n_window"] == 1200
        assert st["stats"]["v_avg"] == pytest.approx(22.9, abs=0.3)
        ws.send_text(json.dumps({"type": "analysis_view", "req": 8, "t0": meta["t_start"],
                                 "t1": meta["t_end"], "width": 300}))
        env = recv_until(ws, lambda m: m.get("type") == "analysis_envelope")["data"]
        assert env["req"] == 8 and env["env"]["mode"] == "envelope" and env["env"]["width"] == 300
        # nonsense bounds are rejected, not crashed
        ws.send_text(json.dumps({"type": "analysis_window", "req": 9, "t0": "x", "t1": None}))
        recv_until(ws, lambda m: m.get("type") == "error")

    r = client.get("/analysis/export.csv")
    assert r.status_code == 200 and r.text.startswith("t_s,v,i,p") and r.text.count("\n") == 1201
    r = client.get("/analysis/export.csv", params={"t0": 0, "t1": 0.01})
    assert 1 < r.text.count("\n") < 30
    r = client.get("/analysis/export.npz")
    assert r.status_code == 200 and r.content[:2] == b"PK"
    npz = r.content
    # re-import the npz and the exported CSV
    r = client.post("/analysis/import", params={"name": "x.npz"}, content=npz)
    assert r.status_code == 200 and r.json()["source"] == "npz"
    r = client.post("/analysis/import", params={"name": "w.csv"},
                    content=client.get("/analysis/export.csv").content)
    assert r.status_code == 200 and r.json()["source"] == "csv"


def test_import_fixture_csv_numbers_and_missing_channels(client):
    from pathlib import Path
    data = (Path(__file__).parent / "fixtures" / "DS1054Z_data_20261118_103155.csv").read_bytes()
    r = client.post("/analysis/import", params={"name": "f.csv", "v_ch": 1, "i_ch": 4}, content=data)
    assert r.status_code == 200, r.text
    assert r.json()["channels"] == [1, 4]
    r = client.post("/analysis/import", params={"name": "f.csv", "v_ch": 2, "i_ch": 4}, content=data)
    assert r.status_code == 422 and r.json()["channels"] == [1, 4]
    assert client.post("/analysis/import", params={"name": "f.txt"}, content=b"x").status_code == 400
    assert client.post("/analysis/import", params={"name": "f.csv"}, content=b"").status_code == 400
    bad = client.post("/analysis/import", params={"name": "f.csv"}, content=b"\xff\xfe\x00bad")
    assert bad.status_code == 400
    with client.websocket_connect("/ws") as ws:
        drain_initial(ws)
        recv_until(ws, lambda m: m.get("type") == "analysis_meta")   # sent on connect (imported cap)
        ws.send_text(json.dumps({"type": "analysis_window", "req": 1, "t0": -1, "t1": 1}))
        s = recv_until(ws, lambda m: m.get("type") == "analysis_stats")["data"]["stats"]
        assert s["v_avg"] == pytest.approx(22.752, rel=1e-3)
        assert s["i_avg"] == pytest.approx(1.1978, rel=1e-3)
        assert s["p_avg"] == pytest.approx(26.966, rel=1e-3)
        assert s["i_rms"] == pytest.approx(1.983, rel=1e-3)
        assert s["energy_j"] == pytest.approx(32.36, rel=1e-3)


def test_raw_capture_over_websocket_then_cancel(client):
    with client.websocket_connect("/ws") as ws:
        drain_initial(ws)
        ws.send_text(json.dumps({"type": "raw_config", "mdep": 60000}))
        ws.send_text(json.dumps({"type": "raw_capture", "mode": "single"}))
        done = recv_until(ws, lambda m: m.get("type") == "raw_done")["data"]
        assert done["n"] == 60000
        meta = recv_until(ws, lambda m: m.get("type") == "analysis_meta")["data"]
        assert meta["source"] == "raw" and meta["n"] == 60000
        assert meta["meta"]["bytes_per_s"] > 0
        # window over a quarter of the capture
        ws.send_text(json.dumps({"type": "analysis_window", "req": 1,
                                 "t0": meta["t_start"], "t1": meta["t_start"] + 0.25 * (meta["t_end"] - meta["t_start"])}))
        st = recv_until(ws, lambda m: m.get("type") == "analysis_stats")["data"]["stats"]
        assert 14900 <= st["n_window"] <= 15100
        # armed + cancel
        ws.send_text(json.dumps({"type": "acq", "cmd": "repeat"}))
        ws.send_text(json.dumps({"type": "acq", "cmd": "cancel"}))
        s = recv_until(ws, lambda m: m.get("type") == "acq_state" and not m["data"]["armed"]
                       and m["data"]["mode"] != "single")
        assert s["data"]["mode"] in ("repeat", "free")


def test_hold_keeps_current_capture(client):
    with client.websocket_connect("/ws") as ws:
        drain_initial(ws)
        ws.send_text(json.dumps({"type": "acq", "cmd": "single"}))
        m1 = recv_until(ws, lambda m: m.get("type") == "analysis_meta")["data"]
        ws.send_text(json.dumps({"type": "analysis_hold", "on": True}))
        ws.send_text(json.dumps({"type": "acq", "cmd": "single"}))
        held = recv_until(ws, lambda m: m.get("type") == "analysis_state" and m["data"]["held_count"] == 1)
        assert held["data"]["hold"] is True
        ws.send_text(json.dumps({"type": "analysis_select", "source": "last_screen"}))
        m2 = recv_until(ws, lambda m: m.get("type") == "analysis_meta")["data"]
        assert m2["id"] == 2 and m2["rev"] > m1["rev"]
        settle(ws)

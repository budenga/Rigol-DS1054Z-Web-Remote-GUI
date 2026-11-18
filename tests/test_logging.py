"""--debug file logging: file content, no duplicate handlers, no file without --debug."""

import json
import logging.handlers
import logging

import pytest
from fastapi.testclient import TestClient

import server.app as appmod
from rigol import logsetup
from tests.test_server import recv_until


@pytest.fixture(autouse=True)
def _clean_logging():
    logsetup.shutdown_logging()
    yield
    logsetup.shutdown_logging()


def _tagged(logger):
    return [h for h in logger.handlers if getattr(h, "_rigol_handler", False)]


def _single_capture(client):
    with client.websocket_connect("/ws") as ws:
        ws.send_text(json.dumps({"type": "acq", "cmd": "single"}))
        recv_until(ws, lambda m: m.get("type") == "capture")


def _start(monkeypatch, debug, log_dir):
    monkeypatch.setattr(appmod, "USE_FAKE", True)
    monkeypatch.setattr(appmod, "FAKE_PORT", 0)
    monkeypatch.setattr(appmod, "FAKE_WAIT", (0.05, 0.1))
    monkeypatch.setattr(appmod, "FAKE_THROUGHPUT", None)
    monkeypatch.setattr(appmod, "DEBUG", debug)
    monkeypatch.setattr(appmod, "LOG_DIR", str(log_dir))
    return TestClient(appmod.app)


def test_debug_writes_scpi_trace_to_file(monkeypatch, tmp_path):
    with _start(monkeypatch, True, tmp_path) as client:
        _single_capture(client)
        files = list(tmp_path.glob("ds1054z_*.log"))
        assert len(files) == 1
        # exactly one of our handlers per logger while running (no duplicated lines)
        assert len(_tagged(logging.getLogger())) == 2          # console + file
        assert sum(isinstance(h, logging.handlers.RotatingFileHandler)
                   for h in logging.getLogger().handlers) == 1
    text = files[0].read_text(encoding="utf-8")
    assert "Debug log file:" in text
    assert "scope target=" in text and "fake=True" in text
    assert "QUERY: :TRIG:STAT?" in text
    assert "SEND Waveform read" in text and ":WAV:PRE?" in text
    assert "BLOCK header: 1200 bytes expected" in text
    assert "trigger status" in text
    assert "Connected: RIGOL" in text                          # *IDN? once connected
    assert text.count("Connected: RIGOL") == 1
    assert "\x00" not in text                                  # no binary payloads
    assert not _tagged(logging.getLogger())                    # handlers removed at shutdown


def test_no_file_without_debug(monkeypatch, tmp_path):
    with _start(monkeypatch, False, tmp_path) as client:
        _single_capture(client)
        assert not any(isinstance(h, logging.handlers.RotatingFileHandler)
                       for h in logging.getLogger().handlers)
    assert list(tmp_path.iterdir()) == []


def test_setup_is_idempotent_and_reuses_file(tmp_path):
    p1 = logsetup.setup_logging(True, tmp_path)
    p2 = logsetup.setup_logging(True, tmp_path)
    assert p1 == p2 and len(list(tmp_path.glob("*.log"))) == 1
    assert len(_tagged(logging.getLogger())) == 2
    assert logsetup.setup_logging(False, tmp_path) is None
    assert len(_tagged(logging.getLogger())) == 1              # console only


def test_old_logs_are_pruned(tmp_path):
    import os
    for i in range(30):
        f = tmp_path / f"ds1054z_20200101_0000{i:02d}.log"
        f.write_text("x")
        os.utime(f, (1_600_000_000 + i, 1_600_000_000 + i))
    logsetup.setup_logging(True, tmp_path)
    assert len(list(tmp_path.glob("ds1054z_*.log"))) == logsetup.KEEP_FILES


def test_open_log_folder_endpoint(monkeypatch, tmp_path):
    opened = []
    monkeypatch.setattr(logsetup, "open_folder", lambda p: opened.append(str(p)) or True)
    with _start(monkeypatch, True, tmp_path) as client:
        r = client.post("/logs/open")
        assert r.status_code == 200
        assert r.json() == {"ok": True, "path": str(tmp_path), "active": True}
    assert opened == [str(tmp_path)]


def test_open_log_folder_without_debug_uses_existing_dir(monkeypatch, tmp_path):
    opened = []
    monkeypatch.setattr(logsetup, "open_folder", lambda p: opened.append(str(p)) or True)
    with _start(monkeypatch, False, tmp_path) as client:
        r = client.post("/logs/open")
        assert r.json()["active"] is False and r.json()["ok"] is True
    assert opened == [str(tmp_path)]


def test_open_log_folder_missing_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(logsetup, "open_folder", lambda p: pytest.fail("must not open"))
    with _start(monkeypatch, False, tmp_path / "nope") as client:
        r = client.post("/logs/open")
        assert r.status_code == 404 and "--debug" in r.json()["error"]


def test_open_log_folder_refused_for_remote_clients(monkeypatch, tmp_path):
    monkeypatch.setattr(logsetup, "open_folder", lambda p: pytest.fail("must not open"))
    _start(monkeypatch, False, tmp_path)                       # applies the monkeypatches
    with TestClient(appmod.app, client=("192.168.1.50", 5555)) as remote:
        r = remote.post("/logs/open")
        assert r.status_code == 403

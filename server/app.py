"""FastAPI application: static GUI, WebSocket endpoint and analysis import/export."""

import asyncio
import logging
import math
import platform
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from rigol import analysis as an
from rigol import logsetup
from rigol.acquisition import AcquisitionManager
from rigol.scheduler import SCPIScheduler
from rigol.scope import DS1054Z
from rigol.transport import RawTCPTransport
from .analysis_service import AnalysisService
from .hub import ClientHub
from .ws_handler import handle_websocket

log = logging.getLogger(__name__)

# Configured by run.py before uvicorn starts
SCOPE_HOST = "192.168.1.100"
SCOPE_PORT = 5555
USE_FAKE = False          # run.py --fake: start the fake scope in this process
DEBUG = False             # run.py --debug: write a DEBUG log file
LOG_DIR = None            # run.py --log-dir (default: logsetup.default_log_dir())
FAKE_PORT = 5556          # 0 = ephemeral (tests)
FAKE_WAIT = (0.3, 1.5)
FAKE_THROUGHPUT = 3e6     # bytes/s, so RAW progress is visible with the fake

STATIC_DIR = Path(__file__).parent.parent / "static"
MAX_IMPORT_BYTES = 600 * 1024 * 1024

_transport: RawTCPTransport | None = None
_scheduler: SCPIScheduler | None = None
_scope: DS1054Z | None = None
_hub: ClientHub | None = None
_manager: AcquisitionManager | None = None
_analysis: AnalysisService | None = None
_fake = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _transport, _scheduler, _scope, _hub, _manager, _analysis, _fake

    path = logsetup.setup_logging(DEBUG, LOG_DIR)      # idempotent; uvicorn has reset its own loggers by now
    if path:
        log.info("Debug log file: %s", path)
        log.debug("argv=%s", sys.argv)
        log.debug("python=%s on %s", sys.version.split()[0], platform.platform())
        log.debug("scope target=%s:%s fake=%s", SCOPE_HOST, SCOPE_PORT, USE_FAKE)

    host, port = SCOPE_HOST, SCOPE_PORT
    if USE_FAKE:
        from rigol.fake_scope import FakeScope
        _fake = FakeScope(port=FAKE_PORT, throughput=FAKE_THROUGHPUT, wait_range=FAKE_WAIT)
        await _fake.start()
        host, port = "127.0.0.1", _fake.port
        log.warning("Using the FAKE scope on %s:%d - no hardware is touched", host, port)

    _transport = RawTCPTransport(host, port)
    await _transport.connect()
    _scheduler = SCPIScheduler(_transport)
    _scheduler.start()
    _scope = DS1054Z(_scheduler)
    log.info("Connected: %s", await _scope.get_idn())
    await _scope.get_full_state()
    log.info("State synced")

    _hub = ClientHub()
    _manager = AcquisitionManager(_scope, _hub, params_provider=_hub.params_union)
    _analysis = AnalysisService(_hub, raw_pending=lambda: _manager.then_raw)
    _manager.raw_channels_provider = lambda: [_analysis.v_ch, _analysis.i_ch]
    _manager.capture_listeners.append(_analysis.on_screen_capture)
    _manager.raw_listeners.append(_analysis.on_raw)
    _manager.raw_failed_listeners.append(_analysis.on_raw_failed)
    await _manager.start()

    yield

    await _manager.stop()
    await _scheduler.stop()
    await _transport.disconnect()
    if _fake:
        await _fake.stop()
    logsetup.shutdown_logging()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


async def _screenshot(invert: bool):
    if not _manager:
        return Response(status_code=503, content="Not connected")
    try:
        png = await _manager.screenshot(invert=invert)
        return Response(content=png, media_type="image/png")
    except Exception as exc:
        return Response(status_code=500, content=str(exc))


@app.get("/screenshot")
async def screenshot():
    return await _screenshot(False)


@app.get("/screenshot/invert")
async def screenshot_invert():
    return await _screenshot(True)


# --- log folder --------------------------------------------------------------

_LOCAL_CLIENTS = {"127.0.0.1", "::1", "::ffff:127.0.0.1", "testclient"}


@app.post("/logs/open")
async def logs_open(request: Request):
    """Open the log folder in the file manager of the machine running the server.

    Loopback clients only: the server listens on all interfaces, and a page on another
    machine must not be able to pop windows on this one.
    """
    if not request.client or request.client.host not in _LOCAL_CLIENTS:
        return _err(403, "Only available from the machine running the server")
    current = logsetup.log_path()
    folder = current.parent if current else Path(LOG_DIR).expanduser() if LOG_DIR \
        else logsetup.default_log_dir()
    if not folder.is_dir():
        return _err(404, "No log folder yet. Start the server with --debug to create one.")
    if not logsetup.open_folder(folder):
        return _err(501, f"Cannot open a file manager here. Logs are in {folder}")
    return {"ok": True, "path": str(folder), "active": current is not None}


# --- analysis import / export ------------------------------------------------

def _err(status: int, msg: str, **extra) -> JSONResponse:
    return JSONResponse({"ok": False, "error": msg, **extra}, status_code=status)


@app.post("/analysis/import")
async def analysis_import(request: Request, name: str = "", v_ch: int | None = None,
                          i_ch: int | None = None):
    """Raw request body (no multipart). ``.csv`` or ``.npz`` by the ``name`` extension."""
    if not _analysis:
        return _err(503, "Not ready")
    try:
        if int(request.headers.get("content-length", "0")) > MAX_IMPORT_BYTES:
            return _err(413, "File is too large")
    except ValueError:
        pass
    chunks, size = [], 0
    async for chunk in request.stream():          # also enforces the limit for chunked uploads
        size += len(chunk)
        if size > MAX_IMPORT_BYTES:
            return _err(413, "File is too large")
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data:
        return _err(400, "The file is empty")
    try:
        result = await _analysis.import_file(name, data, v_ch, i_ch)
    except an.ChannelsMissing as exc:
        return _err(422, str(exc), channels=exc.present)
    except an.AnalysisError as exc:
        return _err(400, str(exc))
    return JSONResponse(result)


def _stamp(cap) -> str:
    return time.strftime("%Y%m%d_%H%M%S", time.localtime(cap.ts or time.time()))


@app.get("/analysis/export.npz")
async def analysis_export_npz(rev: int | None = None):
    if not _analysis:
        return _err(503, "Not ready")
    if rev is not None and rev != _analysis.rev:
        return _err(409, "The displayed capture changed; export again")
    data = await _analysis.export_npz()
    if data is None:
        return _err(404, "No capture to export")
    name = f"DS1054Z_capture_{_stamp(_analysis.current)}.npz"
    return Response(data, media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{quote(name)}"'})


@app.get("/analysis/export.csv")
async def analysis_export_csv(t0: float | None = None, t1: float | None = None,
                              rev: int | None = None):
    cap = _analysis.current if _analysis else None
    if cap is None:
        return _err(404, "No capture to export")
    if rev is not None and rev != _analysis.rev:
        return _err(409, "The displayed capture changed; export again")
    a = cap.t_start if t0 is None else t0
    b = cap.t_end if t1 is None else t1
    if not (math.isfinite(a) and math.isfinite(b)):
        return _err(400, "t0 and t1 must be finite numbers")
    lo, hi = an.window_indices(cap, a, b)
    if hi - lo + 1 > an.MAX_CSV_EXPORT:
        return _err(413, f"Window has {hi - lo + 1:,} samples; CSV export is limited to "
                         f"{an.MAX_CSV_EXPORT:,}. Narrow the window.")
    name = f"DS1054Z_window_{_stamp(cap)}.csv"
    return StreamingResponse(
        an.export_window_csv_iter(cap, a, b), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{quote(name)}"'})


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    if not _manager or not _hub or not _analysis:
        await ws.close(code=1011, reason="Scope not connected")
        return
    try:
        await handle_websocket(ws, _hub, _manager, _analysis)
    except WebSocketDisconnect:
        log.info("WebSocket client disconnected")
    except Exception as exc:
        log.error("WebSocket error: %s", exc)


class _NoCacheStatic(StaticFiles):
    """Always revalidate: the GUI is edited in place and must never run stale JS/CSS."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


# Static files (CSS, JS)
app.mount("/static", _NoCacheStatic(directory=str(STATIC_DIR)), name="static")

"""WebSocket session handler.

All SCPI traffic is owned by the shared ``AcquisitionManager``; a socket only
runs three small tasks:

* a receive loop (never blocks on the scope; ``cancel`` bypasses the queue),
* a command worker that handles the other messages in order,
* an analysis worker that answers only the *latest* window/view request.

Server -> client binary frame (screen capture, one per channel)::

    [0x43 'C', ch u8, capture_id u32 LE, 44-byte preamble <ddiddii, raw bytes]

and then the JSON ``capture`` message that commits it.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import math

from fastapi import WebSocket, WebSocketDisconnect

from rigol.acquisition import AcquisitionManager, valid_param_spec
from rigol.transport import TransportError
from .analysis_service import AnalysisService
from .hub import ClientHub, ClientSession

log = logging.getLogger(__name__)

MAX_PARAMS = 200


def _num(x, default=None):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if math.isfinite(v) else default


async def handle_websocket(
    ws: WebSocket, hub: ClientHub, manager: AcquisitionManager, analysis: AnalysisService
) -> None:
    session = ClientSession(ws)
    hub.add(session)
    tasks = [
        asyncio.create_task(_command_worker(session, manager, analysis), name="ws-commands"),
        asyncio.create_task(_analysis_worker(session, analysis), name="ws-analysis"),
    ]
    try:
        await session.send_json({"type": "state", "data": manager.scope.state_to_dict()})
        await session.send_json({"type": "acq_state", "data": manager.acq_state()})
        await session.send_json({"type": "analysis_state", "data": analysis.state()})
        meta = analysis.meta()
        if meta:
            await session.send_json({"type": "analysis_meta", "data": meta})
        await _receive_loop(session, manager)
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass        # client left / server shutting down: the finally block does the cleanup
    finally:
        session.alive = False
        hub.remove(session)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _receive_loop(session: ClientSession, manager: AcquisitionManager) -> None:
    while True:
        text = await session.ws.receive_text()
        try:
            msg = json.loads(text)
        except ValueError:
            await session.send_json({"type": "error", "msg": "Bad JSON message"})
            continue
        if not isinstance(msg, dict):
            continue
        t = msg.get("type")
        if t in ("analysis_window", "analysis_view"):
            session.pending[t] = msg            # superseded requests are simply overwritten
            session.wake.set()
        elif (t == "acq" and msg.get("cmd") == "cancel") or t == "raw_cancel":
            asyncio.create_task(manager.command("cancel"))   # never wait behind other commands
        else:
            session.cmd_queue.put_nowait(msg)


async def _command_worker(
    session: ClientSession, manager: AcquisitionManager, analysis: AnalysisService
) -> None:
    while True:
        msg = await session.cmd_queue.get()
        try:
            await _dispatch(session, manager, analysis, msg)
        except asyncio.CancelledError:
            raise
        except TransportError as exc:
            await session.send_json({"type": "error", "msg": f"Scope error: {exc}"})
        except Exception as exc:
            log.exception("message %s failed", msg.get("type"))
            await session.send_json({"type": "error", "msg": f"Internal error: {exc}"})


async def _dispatch(session, manager, analysis, msg: dict) -> None:
    t = msg.get("type")
    if t == "control":
        await manager.control(str(msg.get("cmd", "")), msg.get("args") or {})
    elif t == "acq":
        await manager.command(str(msg.get("cmd", "")))
    elif t == "params":
        items = msg.get("items")
        if isinstance(items, list):
            session.params = [p for p in items[:MAX_PARAMS] if valid_param_spec(p)]
    elif t == "raw_config":
        n = _num(msg.get("mdep"))
        if n is not None:
            await manager.set_raw_mdep(int(n))
    elif t == "raw_capture":
        await manager.raw_capture(str(msg.get("mode", "single")))
    elif t == "screenshot":
        args = msg.get("args") or {}
        try:
            png = await manager.screenshot(bool(args.get("invert")), bool(args.get("grayscale")))
            await session.send_json({"type": "screenshot", "data": base64.b64encode(png).decode()})
        except TransportError as exc:
            await session.send_json({"type": "error", "msg": f"Screenshot failed: {exc}"})
    elif t == "scpi":
        resp = await manager.scpi(str(msg.get("cmd", "")))
        await session.send_json({"type": "scpi_response", "data": resp})
    elif t == "analysis_select":
        err = await analysis.select(str(msg.get("source", "last_screen")))
        if err:
            await session.send_json({"type": "toast", "level": "warn", "msg": err})
    elif t == "analysis_config":
        err = await analysis.configure(
            msg.get("v_ch"), msg.get("i_ch"), _num(msg.get("i_threshold")))
        if err:
            await session.send_json({"type": "toast", "level": "warn", "msg": err})
    elif t == "analysis_hold":
        await analysis.set_hold(bool(msg.get("on")))
    else:
        log.warning("Unknown message type: %r", t)


async def _analysis_worker(session: ClientSession, analysis: AnalysisService) -> None:
    """Answer only the newest pending window / view request (no queue while dragging)."""
    while True:
        await session.wake.wait()
        session.wake.clear()
        if not session.alive:
            return
        for kind in ("analysis_window", "analysis_view"):
            msg = session.pending.pop(kind, None)
            if msg is None:
                continue
            try:
                await _answer(session, analysis, kind, msg)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("%s failed", kind)
                await session.send_json({"type": "error", "msg": f"Analysis failed: {exc}"})
        if session.pending:
            session.wake.set()


async def _answer(session: ClientSession, analysis: AnalysisService, kind: str, msg: dict) -> None:
    req = msg.get("req")
    t0, t1 = _num(msg.get("t0")), _num(msg.get("t1"))
    if t0 is None or t1 is None:
        await session.send_json({"type": "error", "msg": "Window bounds must be finite numbers"})
        return
    if kind == "analysis_window":
        stats, rev = await analysis.stats(t0, t1)
        if "analysis_window" in session.pending:
            return                                  # superseded while computing: drop
        await session.send_json(
            {"type": "analysis_stats", "data": {"req": req, "rev": rev, "stats": stats}})
    else:
        width = int(_num(msg.get("width"), 800))
        env, rev = await analysis.envelope(t0, t1, width)
        if "analysis_view" in session.pending:
            return
        data = {"req": req, "rev": rev, "env": env}
        await session.send_json({"type": "analysis_envelope", "data": data})

/**
 * app.js — Main entry point for the Rigol DS1054Z Web Remote.
 *
 * WebSocket with auto-reconnect. Server → browser:
 *   binary 0x43 frames  one channel of a screen capture (committed by the JSON `capture`)
 *   JSON                {type, data} — state, acq_state, capture, raw_progress, raw_done,
 *                       analysis_*, toast, error, screenshot, scpi_response
 * Every JSON message is also re-published on the bus under its `type`.
 * Exports sendControl(), sendScpi() and sendMessage() for the other modules.
 */

import { initRenderer, feedCaptureFrame, commitCapture, updateRendererState } from './renderer.js';
import { initControls, updateControlsState } from './controls.js';
import { initMeasurements, updateMeasurements, updateMeasurementChannels } from './measurements.js';
import { initScreenshot, handleScreenshotData } from './screenshot.js';
import { initCursors, updateCursorState } from './cursors.js';
import { initTheme, cycleTheme } from './theme.js';
import { initAcquisition, doCapture } from './acquisition.js';
import { initAnalysisView, onViewActive } from './analysis/view.js';
import { sendParamsNow } from './paramsync.js';
import { on, emit, loadPref, savePref } from './bus.js';
import { toast } from './toast.js';

let _ws = null;
const RECONNECT_MS = 2000;
const CAPTURE_MAGIC = 0x43;
let _connDot = null;
let _connText = null;

// --- WebSocket ---

function _connect() {
    _ws = new WebSocket(`ws://${location.host}/ws`);
    _ws.binaryType = 'arraybuffer';

    _ws.onopen = () => {
        _updateConnectionUI(true);
        sendParamsNow();
    };
    _ws.onclose = () => {
        _updateConnectionUI(false);
        setTimeout(_connect, RECONNECT_MS);
    };
    _ws.onerror = () => { _ws?.close(); };
    _ws.onmessage = (event) => {
        if (event.data instanceof ArrayBuffer) _handleBinary(event.data);
        else _handleJSON(event.data);
    };
}

function _handleBinary(buffer) {
    if (buffer.byteLength < 2) return;
    if (new DataView(buffer).getUint8(0) === CAPTURE_MAGIC) feedCaptureFrame(buffer);
}

function _handleJSON(raw) {
    let msg;
    try {
        msg = JSON.parse(raw);
    } catch {
        console.warn('[WS] Bad JSON:', raw);
        return;
    }
    switch (msg.type) {
        case 'state':
            updateControlsState(msg.data);
            updateRendererState(msg.data);
            updateCursorState(msg.data);
            updateMeasurementChannels(msg.data);
            break;
        case 'capture':
            if (commitCapture(msg.data)) {
                updateMeasurements(msg.data.scope_params);
                document.getElementById('scope-incoherent').hidden = msg.data.coherent !== false;
            }
            break;
        case 'screenshot':
            handleScreenshotData(msg.data);
            break;
        case 'scpi_response':
            console.log('[SCPI]', msg.data);
            break;
        case 'toast':
            toast(msg.msg, msg.level);
            break;
        case 'error':
            console.error('[Server]', msg.msg);
            toast(msg.msg, 'error');
            break;
        case 'connection':
            console.log('[Connection]', msg.status, msg.msg);
            break;
        default:
            break;
    }
    emit(msg.type, msg.data !== undefined ? msg.data : msg);
}

function _updateConnectionUI(connected) {
    _connDot?.classList.toggle('connected', connected);
    if (_connText) _connText.textContent = connected ? 'Connected' : 'Disconnected';
}

// --- Public API ---

/** @param {string} cmd e.g. "set_channel_scale" @param {Object} [args] */
export function sendControl(cmd, args = {}) {
    sendMessage({ type: 'control', cmd, args });
}

/** @param {string} cmd raw SCPI */
export function sendScpi(cmd) {
    sendMessage({ type: 'scpi', cmd });
}

/** @returns {boolean} whether the message was sent */
export function sendMessage(msg) {
    if (_ws && _ws.readyState === WebSocket.OPEN) {
        _ws.send(JSON.stringify(msg));
        return true;
    }
    return false;
}

// --- View switch (instant, no animation) ---

function _initViews() {
    const tabs = [...document.querySelectorAll('#view-switch button[data-view]')];
    const show = (view) => {
        for (const t of tabs) t.setAttribute('aria-selected', String(t.dataset.view === view));
        document.getElementById('view-scope').hidden = view !== 'scope';
        document.getElementById('view-analysis').hidden = view !== 'analysis';
        savePref('ds1054z.view', view);
        emit('viewchange', view);
        if (view === 'analysis') onViewActive();
        else window.dispatchEvent(new Event('resize'));
    };
    for (const t of tabs) t.addEventListener('click', () => show(t.dataset.view));
    show(loadPref('ds1054z.view', 'scope') === 'analysis' ? 'analysis' : 'scope');
    return show;
}

// --- Global keyboard shortcuts (C = capture, T = theme; others live in the analysis view) ---

function _initKeys() {
    document.addEventListener('keydown', (e) => {
        if (e.metaKey || e.ctrlKey || e.altKey) return;
        const t = e.target;
        if (t && (t.tagName === 'INPUT' || t.tagName === 'SELECT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
        if (e.key === 'c' || e.key === 'C') { e.preventDefault(); doCapture(); }
        else if (e.key === 't' || e.key === 'T') { e.preventDefault(); cycleTheme(); }
    });
}

document.addEventListener('DOMContentLoaded', () => {
    _connDot = document.getElementById('conn-dot');
    _connText = document.getElementById('conn-text');

    initTheme(document.getElementById('btn-theme'));
    document.getElementById('btn-logs').addEventListener('click', openLogFolder);
    initRenderer();
    initControls();
    initMeasurements();
    initScreenshot();
    initCursors(document.getElementById('scope-canvas'));
    initAcquisition();
    initAnalysisView();
    _initViews();
    _initKeys();
    _connect();
});

/** Ask the server to open its log folder in Finder (only works from the server's own machine). */
async function openLogFolder() {
    try {
        const r = await fetch('/logs/open', { method: 'POST' });
        const j = await r.json();
        if (j.ok) {
            toast(j.active ? 'Log folder opened' : 'Logging is off (start with --debug). Opened the folder with earlier logs.', j.active ? 'ok' : 'warn');
        } else {
            toast(j.error || 'Cannot open the log folder', 'warn');
        }
    } catch (e) {
        toast('Cannot reach the server', 'error');
    }
}

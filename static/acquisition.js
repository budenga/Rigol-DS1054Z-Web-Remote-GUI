/**
 * acquisition.js — top-bar acquisition controls: Single | Repeat | Free, Capture,
 * Cancel and the status pill. The server owns the acquisition state machine
 * (rigol/acquisition.py); this module only sends `acq` / `raw_capture` messages
 * and renders `acq_state`.
 */

import { sendMessage } from './app.js';
import { on, loadPref, savePref } from './bus.js';
import { toast } from './toast.js';

let _mode = 'single';
let _source = 'screen';        // 'screen' | 'raw' (set by the Power analysis view)
let _view = 'scope';
let _acq = null;
let _el = {};

export function initAcquisition() {
    _el = {
        modes: document.getElementById('acq-mode'),
        capture: document.getElementById('btn-capture'),
        cancel: document.getElementById('btn-cancel'),
        pill: document.getElementById('status-pill'),
        text: document.getElementById('status-pill-text'),
        meta: document.getElementById('status-pill-meta'),
    };
    const saved = loadPref('ds1054z.acqMode', 'single');
    _mode = ['single', 'repeat', 'free'].includes(saved) ? saved : 'single';
    _renderMode();

    _el.modes.addEventListener('click', (e) => {
        const b = e.target.closest('button[data-mode]');
        if (!b) return;
        _mode = b.dataset.mode;
        savePref('ds1054z.acqMode', _mode);
        _renderMode();
    });
    _el.capture.addEventListener('click', doCapture);
    _el.cancel.addEventListener('click', () => sendMessage({ type: 'acq', cmd: 'cancel' }));

    on('acq_state', (s) => { _acq = s; _renderState(); });
    on('viewchange', (v) => { _view = v; });
    _renderState();
}

export function getAcqMode() { return _mode; }
export function getAcqState() { return _acq; }

/** The Power analysis view tells us what the Capture button should read. */
export function setCaptureSource(src) { _source = src; }

export function doCapture() {
    if (_acq?.busy_raw) { toast('A RAW read is in progress', 'warn'); return; }
    const raw = _source === 'raw' && _view === 'analysis';
    if (_mode === 'free') {
        if (raw) { toast('RAW capture needs Single or Repeat mode', 'warn'); return; }
        sendMessage({ type: 'acq', cmd: 'free' });
        sendMessage({ type: 'acq', cmd: 'read_screen' });
        return;
    }
    if (raw) sendMessage({ type: 'raw_capture', mode: _mode });
    else sendMessage({ type: 'acq', cmd: _mode });
}

function _renderMode() {
    for (const b of _el.modes.querySelectorAll('button[data-mode]')) {
        b.setAttribute('aria-checked', String(b.dataset.mode === _mode));
    }
}

function _fmtTime(ts) {
    if (!ts) return '';
    const d = new Date(ts * 1000);
    return d.toLocaleTimeString([], { hour12: false });
}

function _renderState() {
    const s = _acq;
    let state = 'stopped';
    let text = 'Stopped';
    if (s) {
        if (!s.scope_ok) { state = 'offline'; text = 'Scope not responding'; }
        else if (s.busy_raw) { state = 'raw'; text = 'Reading RAW memory…'; }
        else if (s.armed || s.trig_status === 'WAIT') { state = 'waiting'; text = 'Waiting for trigger…'; }
        else if (['TD', 'RUN', 'AUTO'].includes(s.trig_status)) { state = 'free'; text = 'Running (free)'; }
    }
    _el.pill.dataset.state = state;
    _el.text.textContent = text;
    _el.meta.textContent = s && s.capture_count ? `#${s.capture_count} · ${_fmtTime(s.last_ts)}` : '';
    const armed = !!(s && (s.armed || s.busy_raw));
    _el.cancel.hidden = !armed;
    _el.capture.disabled = !!(s && s.busy_raw);
}

/**
 * renderer.js — Canvas renderer for the scope view (committed captures only).
 *
 * Binary frame from the server (one per channel of a capture):
 *   [0x43 'C', channel u8, capture_id u32 LE, preamble(44), ...raw_bytes]
 *   preamble: <ddiddii (little-endian)
 *     xinc(f64), xorig(f64), xref(i32), yinc(f64), yorig(f64), yref(i32), points(i32)
 * A capture is drawn only when its JSON `capture` message arrives, so all
 * channels on screen always come from the same acquisition.
 *
 * The canvas follows its container (ResizeObserver, devicePixelRatio) and is
 * redrawn on demand (new capture, resize, theme change, cursor move).
 */

import { drawCursors } from './cursors.js';
import { layout, computeLayout, GRID_COLS, GRID_ROWS, SUBDIV } from './layout.js';
import { themeColors } from './theme.js';

const HEADER = 6;
const PREAMBLE_SIZE = 44;

// --- Module state ---

/** capture id -> Map(channel -> {raw, preamble}) while waiting for the committing JSON */
const _pending = new Map();
/** @type {{id:number, channels: Map<number,{raw:Uint8Array,preamble:object}>, meta:object}|null} */
let _committed = null;

let _canvas = null;
let _ctx = null;
let _colors = null;
let _screenshotImg = null;
let _screenshotOpacity = 0.5;
let _screenshotVisible = false;
let _triggerLevel = 0;
let _triggerSource = 1;
let _timebaseScale = 1e-3;
let _channelStates = {};
let _dirty = false;

function parsePreamble(dv, offset) {
    return {
        xinc:   dv.getFloat64(offset,      true),
        xorig:  dv.getFloat64(offset + 8,  true),
        xref:   dv.getInt32  (offset + 16, true),
        yinc:   dv.getFloat64(offset + 20, true),
        yorig:  dv.getFloat64(offset + 28, true),
        yref:   dv.getInt32  (offset + 36, true),
        points: dv.getInt32  (offset + 40, true),
    };
}

// --- Public API ---

export function initRenderer() {
    _canvas = document.getElementById('scope-canvas');
    _ctx = _canvas.getContext('2d');
    _readColors();
    const box = _canvas.parentElement;
    new ResizeObserver(_resize).observe(box);
    document.addEventListener('themechange', () => { _readColors(); requestRedraw(); });
    _resize();
}

/** Redraw on the next animation frame (coalesces bursts). */
export function requestRedraw() {
    if (_dirty) return;
    _dirty = true;
    requestAnimationFrame(_draw);
}

/** Store one binary capture frame; it is drawn once `commitCapture` is called. */
export function feedCaptureFrame(buffer) {
    if (buffer.byteLength < HEADER + PREAMBLE_SIZE + 1) return;
    const dv = new DataView(buffer);
    const ch = dv.getUint8(1);
    const id = dv.getUint32(2, true);
    const preamble = parsePreamble(dv, HEADER);
    const raw = new Uint8Array(buffer.slice(HEADER + PREAMBLE_SIZE));
    let m = _pending.get(id);
    if (!m) { m = new Map(); _pending.set(id, m); }
    m.set(ch, { raw, preamble });
    // never keep more than a few uncommitted captures
    if (_pending.size > 4) _pending.delete(_pending.keys().next().value);
}

/** The capture's JSON arrived: make the buffered channels the displayed capture. */
export function commitCapture(meta) {
    const frames = _pending.get(meta.id);
    if (!frames) return false;
    _committed = { id: meta.id, channels: frames, meta };
    for (const id of [..._pending.keys()]) if (id <= meta.id) _pending.delete(id);
    requestRedraw();
    return true;
}

export function updateRendererState(state) {
    _timebaseScale = state.timebase_scale || 1e-3;
    _triggerLevel = state.trigger?.level || 0;
    const src = state.trigger?.source || 'CHAN1';
    _triggerSource = parseInt(src.replace('CHAN', ''), 10) || 1;
    _channelStates = {};
    for (let ch = 1; ch <= 4; ch++) {
        const cs = state[`ch${ch}`];
        if (cs) _channelStates[ch] = { on: cs.on, scale: cs.scale, offset: cs.offset,
                                       coupling: cs.coupling, probe: cs.probe || 1 };
    }
    requestRedraw();
}

export function setScreenshotImage(img) { _screenshotImg = img; requestRedraw(); }
export function setScreenshotVisible(vis) { _screenshotVisible = vis; requestRedraw(); }
export function setScreenshotOpacity(opacity) { _screenshotOpacity = opacity; requestRedraw(); }

/** Channel data of the displayed capture, for the CSV save. */
export function getChannelData() { return _committed ? _committed.channels : new Map(); }
export function getCommittedCapture() { return _committed; }

// --- Sizing / colours ---

function _readColors() {
    const c = themeColors();
    _colors = {
        bg: c['plot-bg'], minor: c['grid-minor'], major: c['grid-major'], center: c['grid-center'],
        frame: c['plot-frame'], text: c['text-bright'], dim: c['text-dim'],
        ch: { 1: c.ch1, 2: c.ch2, 3: c.ch3, 4: c.ch4 },
        ca: c['cursor-a'], cb: c['cursor-b'],
    };
}

function _resize() {
    const box = _canvas.parentElement;
    const w = Math.floor(box.clientWidth);
    const h = Math.floor(box.clientHeight);
    if (w < 10 || h < 10) return;                      // view hidden
    const dpr = window.devicePixelRatio || 1;
    _canvas.width = Math.round(w * dpr);
    _canvas.height = Math.round(h * dpr);
    _ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    computeLayout(w, h, dpr);
    requestRedraw();
}

// --- Drawing ---

function _draw() {
    _dirty = false;
    const ctx = _ctx;
    if (!ctx || layout.w < 10) return;
    ctx.fillStyle = _colors.bg;
    ctx.fillRect(0, 0, layout.w, layout.h);
    _drawGrid(ctx);
    _drawTraces(ctx);
    _drawTriggerMarker(ctx);
    _drawLabels(ctx);
    _drawScreenshotOverlay(ctx);
    drawCursors(ctx, _colors);
}

function _drawGrid(ctx) {
    const { ml: x0, mt: y0, gw, gh, cw, ch } = layout;
    ctx.strokeStyle = _colors.minor;
    ctx.lineWidth = 0.5;
    ctx.beginPath();
    for (let i = 0; i <= GRID_COLS * SUBDIV; i++) {
        const x = x0 + (i * cw / SUBDIV);
        ctx.moveTo(x, y0); ctx.lineTo(x, y0 + gh);
    }
    for (let i = 0; i <= GRID_ROWS * SUBDIV; i++) {
        const y = y0 + (i * ch / SUBDIV);
        ctx.moveTo(x0, y); ctx.lineTo(x0 + gw, y);
    }
    ctx.stroke();

    ctx.strokeStyle = _colors.major;
    ctx.lineWidth = 0.8;
    ctx.beginPath();
    for (let i = 0; i <= GRID_COLS; i++) {
        const x = x0 + i * cw;
        ctx.moveTo(x, y0); ctx.lineTo(x, y0 + gh);
    }
    for (let i = 0; i <= GRID_ROWS; i++) {
        const y = y0 + i * ch;
        ctx.moveTo(x0, y); ctx.lineTo(x0 + gw, y);
    }
    ctx.stroke();

    ctx.strokeStyle = _colors.center;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x0, y0 + gh / 2); ctx.lineTo(x0 + gw, y0 + gh / 2);
    ctx.moveTo(x0 + gw / 2, y0); ctx.lineTo(x0 + gw / 2, y0 + gh);
    ctx.stroke();

    ctx.strokeStyle = _colors.frame;
    ctx.strokeRect(x0, y0, gw, gh);
}

function _drawTraces(ctx) {
    if (!_committed) return;
    const { ml, mt, gw, gh, ch: cellH } = layout;
    for (const [chn, data] of _committed.channels) {
        const cs = _channelStates[chn];
        if (!cs || !cs.on) continue;
        const { raw, preamble } = data;
        const n = raw.length;
        if (n === 0) continue;

        ctx.strokeStyle = _colors.ch[chn] || _colors.text;
        ctx.lineWidth = 1.5;
        ctx.lineJoin = 'round';
        ctx.beginPath();
        const centerY = mt + gh / 2;
        let started = false;
        for (let i = 0; i < n; i++) {
            const val = raw[i];
            if (val === 0 || val === 255) { started = false; continue; }   // clipped
            const volts = (val - preamble.yorig - preamble.yref) * preamble.yinc;
            const py = centerY - ((volts + cs.offset) / cs.scale) * cellH;
            const px = ml + (i / n) * gw;
            if (!started) { ctx.moveTo(px, py); started = true; } else ctx.lineTo(px, py);
        }
        ctx.stroke();
    }
}

function _drawTriggerMarker(ctx) {
    const cs = _channelStates[_triggerSource];
    if (!cs || !cs.on) return;
    const { ml, mt, gw, gh, ch: cellH } = layout;
    const py = mt + gh / 2 - ((_triggerLevel + cs.offset) / cs.scale) * cellH;
    if (py < mt || py > mt + gh) return;
    const color = _colors.ch[_triggerSource] || _colors.text;

    ctx.strokeStyle = color;
    ctx.lineWidth = 1;
    ctx.setLineDash([6, 4]);
    ctx.globalAlpha = 0.6;
    ctx.beginPath();
    ctx.moveTo(ml, py); ctx.lineTo(ml + gw, py);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.globalAlpha = 1;

    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.moveTo(ml + gw + 1, py);
    ctx.lineTo(ml + gw + 8, py - 4);
    ctx.lineTo(ml + gw + 8, py + 4);
    ctx.closePath();
    ctx.fill();
    ctx.font = '9px ui-sans-serif, sans-serif';
    ctx.fillText('T', ml - 12, py + 3);
}

function _drawLabels(ctx) {
    const { ml, mt, gw, gh, ch: cellH } = layout;
    ctx.font = '10px ui-monospace, Menlo, Consolas, monospace';
    ctx.fillStyle = _colors.dim;
    ctx.fillText('T: ' + _formatSI(_timebaseScale, 's') + '/div', ml + 4, mt + 12);

    let labelX = ml + 140;
    for (let ch = 1; ch <= 4; ch++) {
        const cs = _channelStates[ch];
        if (!cs || !cs.on) continue;
        ctx.fillStyle = _colors.ch[ch];
        const text = `CH${ch}: ${_formatSI(cs.scale, 'V')}/div`;
        ctx.fillText(text, labelX, mt + 12);
        labelX += ctx.measureText(text).width + 16;
    }

    for (let ch = 1; ch <= 4; ch++) {
        const cs = _channelStates[ch];
        if (!cs || !cs.on) continue;
        const py = mt + gh / 2 - (cs.offset / cs.scale) * cellH;
        const y = Math.max(mt, Math.min(mt + gh, py));
        ctx.fillStyle = _colors.ch[ch];
        ctx.beginPath();
        ctx.moveTo(ml - 1, y);
        ctx.lineTo(ml - 8, y - 4);
        ctx.lineTo(ml - 8, y + 4);
        ctx.closePath();
        ctx.fill();
        ctx.font = '9px ui-sans-serif, sans-serif';
        ctx.fillText(String(ch), ml - 18, y + 3);
    }
}

function _drawScreenshotOverlay(ctx) {
    if (!_screenshotVisible || !_screenshotImg) return;
    ctx.globalAlpha = _screenshotOpacity;
    ctx.drawImage(_screenshotImg, 0, 0, layout.w, layout.h);
    ctx.globalAlpha = 1;
}

// --- SI formatting (axis labels) ---

const SI_PREFIXES = [
    { exp: 9, prefix: 'G' }, { exp: 6, prefix: 'M' }, { exp: 3, prefix: 'k' }, { exp: 0, prefix: '' },
    { exp: -3, prefix: 'm' }, { exp: -6, prefix: 'µ' }, { exp: -9, prefix: 'n' }, { exp: -12, prefix: 'p' },
];

function _formatSI(value, unit) {
    if (value === 0) return `0 ${unit}`;
    const abs = Math.abs(value);
    for (const { exp, prefix } of SI_PREFIXES) {
        const t = Math.pow(10, exp);
        if (abs >= t * 0.999) {
            const digits = abs >= t * 100 ? 0 : abs >= t * 10 ? 1 : abs >= t ? 2 : 3;
            return (value / t).toFixed(digits) + ' ' + prefix + unit;
        }
    }
    return value.toExponential(2) + ' ' + unit;
}

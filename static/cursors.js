/**
 * cursors.js — Draggable measurement cursors for the scope canvas.
 *
 * - X cursors (vertical lines → time / frequency), Y cursors (horizontal → voltage)
 * - Positions are fractions of the 12 x 8 grid, so they survive canvas resizes and
 *   map through the same layout transform as the traces.
 * - Pointer events with pointer capture (mouse, touch, pen).
 * - Readout panel with A, B, dX, 1/dX (or A, B, dY).
 * No animation on cursor drag (it is used constantly).
 */

import { formatSI } from './controls.js';
import { layout } from './layout.js';
import { requestRedraw } from './renderer.js';

const CURSOR_NONE = 'off';
const CURSOR_X = 'x';
const CURSOR_Y = 'y';
const GRAB = 8; // px

let _mode = CURSOR_NONE;
let _ax = 1 / 3;  // grid fractions
let _bx = 2 / 3;
let _ay = 1 / 3;
let _by = 2 / 3;
let _dragging = null;
let _canvas = null;
let _scopeState = null;

export function initCursors(canvas) {
    _canvas = canvas;
    document.getElementById('cursor-mode')?.addEventListener('change', (e) => setCursorMode(e.target.value));
    _bindPointer();
}

export function setCursorMode(mode) {
    _mode = mode;
    _updateReadout();
    requestRedraw();
}

export function updateCursorState(state) {
    _scopeState = state;
    _updateReadout();
}

export function drawCursors(ctx, colors) {
    if (_mode === CURSOR_NONE) return;
    const { ml, mt, gw, gh } = layout;
    ctx.save();
    ctx.setLineDash([6, 4]);
    ctx.lineWidth = 1;
    const items = _mode === CURSOR_X
        ? [['A', _ax, colors.ca], ['B', _bx, colors.cb]]
        : [['A', _ay, colors.ca], ['B', _by, colors.cb]];
    for (const [name, f, color] of items) {
        ctx.strokeStyle = color;
        ctx.fillStyle = color;
        ctx.beginPath();
        if (_mode === CURSOR_X) {
            const x = ml + f * gw;
            ctx.moveTo(x, mt); ctx.lineTo(x, mt + gh);
            ctx.stroke();
            _label(ctx, name, x + 3, mt + 24);
        } else {
            const y = mt + f * gh;
            ctx.moveTo(ml, y); ctx.lineTo(ml + gw, y);
            ctx.stroke();
            _label(ctx, name, ml + 4, y - 4);
        }
    }
    ctx.restore();
}

function _label(ctx, text, x, y) {
    ctx.save();
    ctx.setLineDash([]);
    ctx.font = '11px ui-monospace, Menlo, monospace';
    ctx.fillText(text, x, y);
    ctx.restore();
}

// --- pointer handling (CSS pixels, same space as the layout) ---

function _pos(e) {
    const r = _canvas.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
}

function _hit(x, y) {
    const { ml, mt, gw, gh } = layout;
    if (_mode === CURSOR_X) {
        if (Math.abs(x - (ml + _ax * gw)) < GRAB) return 'ax';
        if (Math.abs(x - (ml + _bx * gw)) < GRAB) return 'bx';
    } else if (_mode === CURSOR_Y) {
        if (Math.abs(y - (mt + _ay * gh)) < GRAB) return 'ay';
        if (Math.abs(y - (mt + _by * gh)) < GRAB) return 'by';
    }
    return null;
}

function _bindPointer() {
    _canvas.addEventListener('pointerdown', (e) => {
        if (_mode === CURSOR_NONE) return;
        const p = _pos(e);
        _dragging = _hit(p.x, p.y);
        if (_dragging) {
            _canvas.setPointerCapture(e.pointerId);
            _canvas.style.cursor = _mode === CURSOR_X ? 'col-resize' : 'row-resize';
            e.preventDefault();
        }
    });
    _canvas.addEventListener('pointermove', (e) => {
        const p = _pos(e);
        if (!_dragging) {
            _canvas.style.cursor = _mode !== CURSOR_NONE && _hit(p.x, p.y)
                ? (_mode === CURSOR_X ? 'col-resize' : 'row-resize') : '';
            return;
        }
        const { ml, mt, gw, gh } = layout;
        const fx = Math.max(0, Math.min(1, (p.x - ml) / gw));
        const fy = Math.max(0, Math.min(1, (p.y - mt) / gh));
        if (_dragging === 'ax') _ax = fx;
        else if (_dragging === 'bx') _bx = fx;
        else if (_dragging === 'ay') _ay = fy;
        else if (_dragging === 'by') _by = fy;
        _updateReadout();
        requestRedraw();
    });
    const end = () => { _dragging = null; _canvas.style.cursor = ''; };
    _canvas.addEventListener('pointerup', end);
    _canvas.addEventListener('pointercancel', end);
}

// --- readout ---

function _updateReadout() {
    const el = document.getElementById('cursor-readout');
    if (!el) return;
    if (_mode === CURSOR_NONE) { el.style.display = 'none'; return; }
    el.style.display = 'block';
    if (!_scopeState) { el.innerHTML = '<div class="cursor-val">No data</div>'; return; }
    if (_mode === CURSOR_X) _readoutX(el); else _readoutY(el);
}

function _readoutX(el) {
    const total = (_scopeState.timebase_scale || 1e-3) * 12;
    const off = _scopeState.timebase_offset || 0;
    const tA = (_ax - 0.5) * total + off;
    const tB = (_bx - 0.5) * total + off;
    const dt = tB - tA;
    const freq = dt !== 0 ? 1 / Math.abs(dt) : Infinity;
    el.innerHTML = `
        <div class="cursor-val"><span style="color:var(--cursor-a)">A:</span> ${formatSI(tA, 's')}</div>
        <div class="cursor-val"><span style="color:var(--cursor-b)">B:</span> ${formatSI(tB, 's')}</div>
        <div class="cursor-val">dX: ${formatSI(dt, 's')}</div>
        <div class="cursor-val">1/dX: ${freq === Infinity ? '--' : formatSI(freq, 'Hz')}</div>`;
}

function _readoutY(el) {
    let scale = 1, off = 0;
    for (let ch = 1; ch <= 4; ch++) {
        const cs = _scopeState[`ch${ch}`];
        if (cs && cs.on) { scale = cs.scale; off = cs.offset; break; }
    }
    const total = scale * 8;
    const vA = (0.5 - _ay) * total - off;
    const vB = (0.5 - _by) * total - off;
    el.innerHTML = `
        <div class="cursor-val"><span style="color:var(--cursor-a)">A:</span> ${formatSI(vA, 'V')}</div>
        <div class="cursor-val"><span style="color:var(--cursor-b)">B:</span> ${formatSI(vB, 'V')}</div>
        <div class="cursor-val">dY: ${formatSI(vB - vA, 'V')}</div>`;
}

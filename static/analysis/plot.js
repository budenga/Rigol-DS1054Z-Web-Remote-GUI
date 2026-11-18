/**
 * plot.js — three-lane (V, I, P) canvas plot on a shared time axis.
 *
 * Draws from the server's min/max envelope (vertical line per pixel column, so
 * peaks survive at any zoom). Interactions: t0/t1 cursors (pointer capture),
 * wheel zoom around the pointer, drag-pan on empty area, hover crosshair.
 * Nothing here animates: cursor drag, zoom and pan are used constantly.
 *
 * `renderTo()` is a pure function of the state so PNG export can draw the same
 * picture at 2x with another colour set.
 */

import { niceTicks, timeAxisFormatter, formatSI } from './format.js';

const M = { l: 66, r: 14, t: 8, b: 44 };   // plot margins (px); b holds axis labels + cursor handles
const GAP = 8;
const LANES = [
    { id: 'v', label: 'V', unit: 'V', color: 'ch1' },
    { id: 'i', label: 'I', unit: 'A', color: 'ch4' },
    { id: 'p', label: 'P', unit: 'W', color: 'math' },
];
const HANDLE_R = 9;

export class Plot {
    /**
     * @param {HTMLCanvasElement} canvas
     * @param {{onWindow:(a:number,b:number,final:boolean)=>void, onView:(v0:number,v1:number)=>void,
     *          getColors:()=>object, getChannelColors:()=>{v:string,i:string}}} hooks
     */
    constructor(canvas, hooks) {
        this.canvas = canvas;
        this.ctx = canvas.getContext('2d');
        this.hooks = hooks;
        this.meta = null;                 // {t_start, t_end, dt, n}
        this.env = null;                  // server envelope
        this.win = { a: 0, b: 0 };        // cursor times (a <= b)
        this.view = { v0: 0, v1: 1 };
        this.active = 'a';
        this.overlay = [];
        this.hover = null;                // {x}
        this.w = 0;
        this.h = 0;
        this._raf = 0;
        this._drag = null;
        this._laneRange = {};
        new ResizeObserver(() => this._resize()).observe(canvas.parentElement);
        this._bind();
        this._resize();
    }

    // --- data ---

    setCapture(meta) {
        this.meta = meta;
        this.env = null;
        if (meta) {
            this.view = { v0: meta.t_start, v1: meta.t_end };
            if (!(this.win.a >= meta.t_start && this.win.b <= meta.t_end && this.win.b > this.win.a)) {
                this.win = { a: meta.t_start, b: meta.t_end };
            }
        }
        this.draw();
    }
    setEnvelope(env) { this.env = env; this.draw(); }
    setOverlay(lines) { this.overlay = lines; this.draw(); }
    setWindow(a, b) { this.win = { a: Math.min(a, b), b: Math.max(a, b) }; this.draw(); }
    setView(v0, v1) {
        if (!this.meta) return;
        const full = this.meta.t_end - this.meta.t_start;
        const minSpan = Math.max(this.meta.dt * 8, full * 1e-9);
        let span = Math.max(minSpan, Math.min(full, v1 - v0));
        let a = Math.max(this.meta.t_start, Math.min(v0, this.meta.t_end - span));
        this.view = { v0: a, v1: a + span };
        this.draw();
    }
    resetView() { if (this.meta) this.setView(this.meta.t_start, this.meta.t_end); this.hooks.onView(this.view.v0, this.view.v1); }
    zoomToWindow() {
        const a = this.win.a, b = this.win.b;
        const pad = (b - a) * 0.05 || this.meta.dt * 4;
        this.setView(a - pad, b + pad);
        this.hooks.onView(this.view.v0, this.view.v1);
    }
    get plotWidth() { return Math.max(1, Math.floor(this.w - M.l - M.r)); }

    // --- geometry ---

    _resize() {
        const box = this.canvas.parentElement;
        const w = Math.floor(box.clientWidth);
        const h = Math.floor(box.clientHeight);
        if (w < 10 || h < 10) return;
        const dpr = window.devicePixelRatio || 1;
        this.canvas.width = Math.round(w * dpr);
        this.canvas.height = Math.round(h * dpr);
        this.w = w;
        this.h = h;
        this.draw();
        this.hooks.onView?.(this.view.v0, this.view.v1);
    }

    _lanes(h = this.h) {
        const inner = h - M.t - M.b;
        const lh = Math.max(20, (inner - 2 * GAP) / 3);
        return LANES.map((l, k) => ({ ...l, y0: M.t + k * (lh + GAP), y1: M.t + k * (lh + GAP) + lh }));
    }

    _x(t, w = this.w) {
        const { v0, v1 } = this.view;
        return M.l + ((t - v0) / (v1 - v0)) * (w - M.l - M.r);
    }

    _t(x) {
        const { v0, v1 } = this.view;
        return v0 + ((x - M.l) / (this.w - M.l - M.r)) * (v1 - v0);
    }

    // --- drawing ---

    draw() {
        if (this._raf) return;
        this._raf = requestAnimationFrame(() => {
            this._raf = 0;
            const dpr = window.devicePixelRatio || 1;
            this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            this.renderTo(this.ctx, this.w, this.h, this.hooks.getColors(), { hover: true });
        });
    }

    /**
     * Draw everything into `ctx` (logical size w x h). `opts.footer` reserves space for a text line.
     */
    renderTo(ctx, w, h, c, opts = {}) {
        const chColors = this.hooks.getChannelColors ? this.hooks.getChannelColors(c) : { v: c.ch1, i: c.ch4 };
        ctx.save();
        ctx.fillStyle = c['plot-bg'];
        ctx.fillRect(0, 0, w, h);
        const lanes = this._lanes(h);
        const fmtT = timeAxisFormatter(this.view.v1 - this.view.v0);
        const xOf = (t) => M.l + ((t - this.view.v0) / (this.view.v1 - this.view.v0)) * (w - M.l - M.r);
        const env = this.env;

        ctx.font = '11px ui-monospace, Menlo, Consolas, monospace';
        ctx.textBaseline = 'middle';

        const xTicks = niceTicks(this.view.v0, this.view.v1, Math.max(3, Math.floor((w - M.l - M.r) / 110)));

        for (const lane of lanes) {
            const color = lane.id === 'v' ? chColors.v : lane.id === 'i' ? chColors.i : c.math;
            const [lo, hi] = this._range(lane.id, env);
            const yOf = (v) => lane.y1 - ((v - lo) / (hi - lo)) * (lane.y1 - lane.y0);
            this._laneRange[lane.id] = { lo, hi, yOf, lane };

            // frame + grid
            ctx.strokeStyle = c['grid-minor'];
            ctx.lineWidth = 1;
            ctx.beginPath();
            for (const t of xTicks) {
                const x = Math.round(xOf(t)) + 0.5;
                ctx.moveTo(x, lane.y0); ctx.lineTo(x, lane.y1);
            }
            const yt = niceTicks(lo, hi, 4);
            for (const v of yt) {
                const y = Math.round(yOf(v)) + 0.5;
                ctx.moveTo(M.l, y); ctx.lineTo(w - M.r, y);
            }
            ctx.stroke();
            ctx.strokeStyle = c['plot-frame'];
            ctx.strokeRect(M.l + 0.5, lane.y0 + 0.5, w - M.l - M.r - 1, lane.y1 - lane.y0 - 1);

            // y labels
            ctx.fillStyle = c['text-dim'];
            ctx.textAlign = 'right';
            for (const v of yt) {
                const y = yOf(v);
                if (y < lane.y0 + 6 || y > lane.y1 - 6) continue;
                ctx.fillText(formatSI(v, '', 3).trim(), M.l - 6, y);
            }
            ctx.fillStyle = color;
            ctx.font = '600 12px ui-sans-serif, sans-serif';
            ctx.textAlign = 'left';
            ctx.fillText(`${lane.label} [${lane.unit}]`, 6, lane.y0 + 10);
            ctx.font = '11px ui-monospace, Menlo, Consolas, monospace';

            // zero line for I and P lanes
            if (lo < 0 && hi > 0) {
                ctx.strokeStyle = c['grid-center'];
                ctx.beginPath();
                const y0 = Math.round(yOf(0)) + 0.5;
                ctx.moveTo(M.l, y0); ctx.lineTo(w - M.r, y0);
                ctx.stroke();
            }

            // window shading (under the trace)
            if (this.meta) {
                const xa = Math.max(M.l, xOf(this.win.a));
                const xb = Math.min(w - M.r, xOf(this.win.b));
                if (xb > xa) {
                    ctx.fillStyle = c['window-fill'];
                    ctx.fillRect(xa, lane.y0, xb - xa, lane.y1 - lane.y0);
                }
            }

            // trace
            ctx.save();
            ctx.beginPath();
            ctx.rect(M.l, lane.y0, w - M.l - M.r, lane.y1 - lane.y0);
            ctx.clip();
            this._trace(ctx, lane.id, env, xOf, yOf, color);
            ctx.restore();

            if (this.meta && (!env || env.mode === 'empty')) {
                ctx.fillStyle = c['text-dim'];
                ctx.textAlign = 'center';
                ctx.fillText('no data in view', M.l + (w - M.l - M.r) / 2, (lane.y0 + lane.y1) / 2);
            } else if (env && this._allNull(lane.id, env)) {
                ctx.fillStyle = c.warning;
                ctx.textAlign = 'center';
                ctx.fillText(`${lane.label}: every sample is clipped`, M.l + (w - M.l - M.r) / 2, (lane.y0 + lane.y1) / 2);
            }
        }

        // time axis labels
        const axisY = lanes[2].y1 + 12;
        ctx.fillStyle = c['text-dim'];
        ctx.textAlign = 'center';
        for (const t of xTicks) {
            const x = xOf(t);
            if (x < M.l + 18 || x > w - M.r - 18) continue;
            ctx.fillText(fmtT(t), x, axisY);
        }

        // cursors
        if (this.meta) {
            this._drawCursor(ctx, w, lanes, xOf(this.win.a), 'A', c['cursor-a'], this.active === 'a');
            this._drawCursor(ctx, w, lanes, xOf(this.win.b), 'B', c['cursor-b'], this.active === 'b');
        }

        // hover crosshair + readout (screen only)
        if (opts.hover && this.hover && this.meta && !this._drag) this._drawHover(ctx, w, lanes, c, chColors);

        // overlay box (top-right, inside the plot)
        if (this.overlay.length) this._drawOverlay(ctx, w, c, h);

        if (opts.footer) {
            ctx.fillStyle = c['text-dim'];
            ctx.font = '12px ui-sans-serif, sans-serif';
            ctx.textAlign = 'left';
            ctx.textBaseline = 'alphabetic';
            ctx.fillText(opts.footer, 8, h - 8);
        }
        ctx.restore();
    }

    _allNull(id, env) {
        if (env.mode === 'samples') return env[id].every((x) => x === null);
        return env[`${id}_max`].every((x) => x === null);
    }

    _range(id, env) {
        let lo = Infinity, hi = -Infinity;
        if (env && env.mode === 'samples') {
            for (const x of env[id]) if (x !== null) { if (x < lo) lo = x; if (x > hi) hi = x; }
        } else if (env && env.mode === 'envelope') {
            for (const x of env[`${id}_min`]) if (x !== null && x < lo) lo = x;
            for (const x of env[`${id}_max`]) if (x !== null && x > hi) hi = x;
        }
        if (id === 'p') { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
        if (!Number.isFinite(lo) || !Number.isFinite(hi)) { lo = 0; hi = 1; }
        if (hi - lo < 1e-12) { const pad = Math.max(Math.abs(hi) * 0.05, 1e-3); lo -= pad; hi += pad; }
        const pad = (hi - lo) * 0.05;               // 5 % headroom
        return [id === 'p' && lo === 0 ? 0 : lo - pad, hi + pad];
    }

    _trace(ctx, id, env, xOf, yOf, color) {
        if (!env) return;
        ctx.strokeStyle = color;
        ctx.lineWidth = 1;
        ctx.beginPath();
        if (env.mode === 'samples') {
            const ys = env[id];
            let started = false;
            for (let k = 0; k < ys.length; k++) {
                if (ys[k] === null) { started = false; continue; }
                const x = xOf(env.t[k]);
                const y = yOf(ys[k]);
                if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
            }
            ctx.stroke();
            if (ys.length < 200) {                    // few samples: show the points
                ctx.fillStyle = color;
                for (let k = 0; k < ys.length; k++) {
                    if (ys[k] === null) continue;
                    ctx.fillRect(xOf(env.t[k]) - 1.5, yOf(ys[k]) - 1.5, 3, 3);
                }
            }
        } else if (env.mode === 'envelope') {
            const mn = env[`${id}_min`];
            const mx = env[`${id}_max`];
            const n = env.n;
            const W = env.width;
            const dt = this.meta.dt;
            // Walk the buckets as one polyline: enter each column at the extreme nearest
            // to where the previous column ended, leave at the other one. Gaps (clipped
            // samples) break the line.
            let prev = null;
            for (let k = 0; k < W; k++) {
                if (mn[k] === null || mx[k] === null) { prev = null; continue; }
                const first = Math.floor((k * n) / W);
                const last = Math.floor(((k + 1) * n) / W) - 1;
                const t = env.t_a + ((first + last) / 2) * dt;
                const x = Math.round(xOf(t)) + 0.5;
                const yHi = yOf(mx[k]);          // smaller y = higher value
                const yLo = Math.max(yOf(mn[k]), yHi + 1);
                let a = yHi, b = yLo;
                if (prev !== null && Math.abs(prev - yLo) < Math.abs(prev - yHi)) { a = yLo; b = yHi; }
                if (prev === null) ctx.moveTo(x, a); else ctx.lineTo(x, a);
                ctx.lineTo(x, b);
                prev = b;
            }
            ctx.stroke();
        }
    }

    _drawCursor(ctx, w, lanes, x, name, color, active) {
        if (x < M.l - 1 || x > w - M.r + 1) return;
        const top = lanes[0].y0;
        const bottom = lanes[2].y1;
        ctx.strokeStyle = color;
        ctx.lineWidth = active ? 1.75 : 1.25;
        ctx.setLineDash([6, 4]);
        ctx.beginPath();
        ctx.moveTo(Math.round(x) + 0.5, top);
        ctx.lineTo(Math.round(x) + 0.5, bottom);
        ctx.stroke();
        ctx.setLineDash([]);
        // handle under the axis
        const hy = bottom + 28;
        ctx.fillStyle = color;
        ctx.beginPath();
        ctx.moveTo(x, hy - HANDLE_R);
        ctx.lineTo(x - HANDLE_R, hy + 6);
        ctx.lineTo(x + HANDLE_R, hy + 6);
        ctx.closePath();
        ctx.fill();
        ctx.fillStyle = '#ffffff';
        ctx.font = '700 10px ui-sans-serif, sans-serif';
        ctx.textAlign = 'center';
        ctx.fillText(name, x, hy + 2);
        ctx.font = '11px ui-monospace, Menlo, Consolas, monospace';
    }

    _drawHover(ctx, w, lanes, c, chColors) {
        const x = this.hover.x;
        if (x < M.l || x > w - M.r) return;
        ctx.strokeStyle = c['grid-center'];
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(Math.round(x) + 0.5, lanes[0].y0);
        ctx.lineTo(Math.round(x) + 0.5, lanes[2].y1);
        ctx.stroke();
        const t = this._t(x);
        const vals = this._sampleAt(t);
        if (!vals) return;
        const fmt = (r, u) => (r.lo === null ? 'n/a'
            : Math.abs(r.hi - r.lo) <= Math.abs(r.hi) * 0.005 ? formatSI(r.hi, u)
            : `${formatSI(r.lo, u)} … ${formatSI(r.hi, u)}`);
        const fmtT = timeAxisFormatter(this.view.v1 - this.view.v0);
        const text = `t ${fmtT(t)}   V ${fmt(vals.v, 'V')}   I ${fmt(vals.i, 'A')}   P ${fmt(vals.p, 'W')}`;
        ctx.font = '11px ui-monospace, Menlo, Consolas, monospace';
        const tw = ctx.measureText(text).width + 14;
        const bx = Math.min(Math.max(M.l + 4, x + 10), w - M.r - tw - 4);
        ctx.fillStyle = c['overlay-bg'];
        ctx.fillRect(bx, lanes[0].y0 + 24, tw, 20);
        ctx.strokeStyle = c['border-strong'];
        ctx.strokeRect(bx + 0.5, lanes[0].y0 + 24.5, tw - 1, 19);
        ctx.fillStyle = c['text-bright'];
        ctx.textAlign = 'left';
        ctx.fillText(text, bx + 7, lanes[0].y0 + 34);
    }

    /** min/max of V, I, P in the envelope bucket (or sample) under time t. */
    _sampleAt(t) {
        const env = this.env;
        if (!env || env.mode === 'empty' || !this.meta) return null;
        const pick = (id) => {
            if (env.mode === 'samples') {
                const ts = env.t;
                let lo = 0, hi = ts.length - 1;
                while (lo < hi) { const m = (lo + hi) >> 1; if (ts[m] < t) lo = m + 1; else hi = m; }
                const k = lo > 0 && Math.abs(ts[lo - 1] - t) < Math.abs(ts[lo] - t) ? lo - 1 : lo;
                const v = env[id][k];
                return v === null ? { lo: null, hi: null } : { lo: v, hi: v };
            }
            const rel = (t - env.t_a) / this.meta.dt;
            const k = Math.max(0, Math.min(env.width - 1, Math.floor((rel * env.width) / env.n)));
            const a = env[`${id}_min`][k];
            const b = env[`${id}_max`][k];
            return a === null ? { lo: null, hi: null } : { lo: a, hi: b };
        };
        return { v: pick('v'), i: pick('i'), p: pick('p') };
    }

    _drawOverlay(ctx, w, c, h = this.h) {
        ctx.font = '12px ui-monospace, Menlo, Consolas, monospace';
        const pad = 8, lh = 17;
        // never taller than the plot: extra rows collapse into a "+N more" line
        const maxRows = Math.max(1, Math.floor((h - M.t - M.b - 12 - pad * 2) / lh));
        let lines = this.overlay;
        if (lines.length > maxRows) {
            const hidden = lines.length - (maxRows - 1);
            lines = [...lines.slice(0, maxRows - 1), { label: `+${hidden} more`, value: 'see Results' }];
        }
        let wl = 0, wv = 0;
        for (const l of lines) {
            wl = Math.max(wl, ctx.measureText(l.label).width);
            wv = Math.max(wv, ctx.measureText(l.value).width);
        }
        const bw = wl + wv + pad * 3;
        const bh = lines.length * lh + pad * 2 - 3;
        const x = w - M.r - bw - 6;
        const y = M.t + 6;
        ctx.fillStyle = c['overlay-bg'];
        ctx.fillRect(x, y, bw, bh);
        ctx.strokeStyle = c['border-strong'];
        ctx.lineWidth = 1;
        ctx.strokeRect(x + 0.5, y + 0.5, bw - 1, bh - 1);
        ctx.textBaseline = 'middle';
        lines.forEach((l, k) => {
            const yy = y + pad + k * lh + 7;
            ctx.textAlign = 'left';
            ctx.fillStyle = c['text-dim'];
            ctx.fillText(l.label, x + pad, yy);
            ctx.textAlign = 'right';
            ctx.fillStyle = c['text-bright'];
            ctx.fillText(l.value, x + bw - pad, yy);
        });
    }

    // --- interaction ---

    _pt(e) {
        const r = this.canvas.getBoundingClientRect();
        return { x: e.clientX - r.left, y: e.clientY - r.top };
    }

    _hitCursor(p) {
        if (!this.meta) return null;
        const lanes = this._lanes();
        const bottom = lanes[2].y1;
        const inLanes = p.y >= lanes[0].y0 && p.y <= bottom + 40;
        if (!inLanes) return null;
        const da = Math.abs(p.x - this._x(this.win.a));
        const db = Math.abs(p.x - this._x(this.win.b));
        const near = p.y > bottom ? HANDLE_R + 3 : 6;
        if (da <= near && da <= db) return 'a';
        if (db <= near) return 'b';
        return null;
    }

    _setCursorTime(which, t) {
        t = Math.max(this.meta.t_start, Math.min(this.meta.t_end, t));
        if (which === 'a') this.win.a = t; else this.win.b = t;
        if (this.win.a > this.win.b) {              // crossed: swap so a <= b, keep dragging the same pointer
            [this.win.a, this.win.b] = [this.win.b, this.win.a];
            if (this._drag && this._drag.kind === 'cursor') this._drag.which = this._drag.which === 'a' ? 'b' : 'a';
            this.active = this.active === 'a' ? 'b' : 'a';
        }
    }

    moveActiveCursor(px) {
        if (!this.meta) return;
        const perPx = (this.view.v1 - this.view.v0) / this.plotWidth;
        const cur = this.active === 'a' ? this.win.a : this.win.b;
        this._setCursorTime(this.active, cur + px * perPx);
        this.draw();
        this.hooks.onWindow(this.win.a, this.win.b, true);
    }

    switchActiveCursor() { this.active = this.active === 'a' ? 'b' : 'a'; this.draw(); }

    _bind() {
        const cv = this.canvas;
        cv.addEventListener('pointerdown', (e) => {
            if (!this.meta || e.button !== 0 || this._drag) return;   // ignore extra pointers mid-drag
            const p = this._pt(e);
            const hit = this._hitCursor(p);
            cv.setPointerCapture(e.pointerId);
            if (hit) {
                this.active = hit;
                // keep the offset from where the handle was grabbed (no snap to the pointer)
                const cur = hit === 'a' ? this.win.a : this.win.b;
                this._drag = { kind: 'cursor', which: hit, grab: cur - this._t(p.x) };
                this._setCursorTime(hit, cur);
                this.hooks.onWindow(this.win.a, this.win.b, false);
            } else {
                this._drag = { kind: 'pan', x0: p.x, v0: this.view.v0, v1: this.view.v1 };
            }
            cv.focus({ preventScroll: true });
            this.draw();
        });
        cv.addEventListener('pointermove', (e) => {
            const p = this._pt(e);
            if (this._drag?.kind === 'cursor') {
                this._setCursorTime(this._drag.which, this._t(p.x) + this._drag.grab);
                this.hooks.onWindow(this.win.a, this.win.b, false);
                this.draw();
            } else if (this._drag?.kind === 'pan') {
                const span = this._drag.v1 - this._drag.v0;
                const dt = -((p.x - this._drag.x0) / this.plotWidth) * span;
                this.setView(this._drag.v0 + dt, this._drag.v1 + dt);
                this.hooks.onView(this.view.v0, this.view.v1);
            } else {
                this.hover = { x: p.x };
                cv.style.cursor = this._hitCursor(p) ? 'ew-resize' : (this.meta ? 'grab' : 'default');
                this.draw();
            }
        });
        const end = () => {
            if (this._drag?.kind === 'cursor') this.hooks.onWindow(this.win.a, this.win.b, true);
            this._drag = null;
            this.draw();
        };
        cv.addEventListener('pointerup', end);
        cv.addEventListener('pointercancel', end);
        cv.addEventListener('pointerleave', () => { if (!this._drag) { this.hover = null; this.draw(); } });
        cv.addEventListener('wheel', (e) => {
            if (!this.meta) return;
            e.preventDefault();
            const p = this._pt(e);
            const tp = this._t(p.x);
            const k = Math.exp(Math.max(-300, Math.min(300, e.deltaY)) * 0.0016);
            const nv0 = tp - (tp - this.view.v0) * k;
            const nv1 = tp + (this.view.v1 - tp) * k;
            this.setView(nv0, nv1);
            this.hooks.onView(this.view.v0, this.view.v1);
        }, { passive: false });
    }
}

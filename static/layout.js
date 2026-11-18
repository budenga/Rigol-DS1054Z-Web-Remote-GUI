/**
 * layout.js — shared geometry of the scope canvas (CSS pixels).
 *
 * The 12 x 8 grid and its label margins are fractions of the 800 x 480
 * reference size so the picture scales with the container. renderer.js and
 * cursors.js both map through this one object (same transform).
 */

export const REF_W = 800;
export const REF_H = 480;
export const GRID_COLS = 12;
export const GRID_ROWS = 8;
export const SUBDIV = 5;

const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

export const layout = {
    w: REF_W, h: REF_H, dpr: 1,
    ml: 50, mr: 10, mt: 10, mb: 20,
    gw: 740, gh: 450, cw: 740 / GRID_COLS, ch: 450 / GRID_ROWS,
};

export function computeLayout(w, h, dpr = 1) {
    layout.w = w;
    layout.h = h;
    layout.dpr = dpr;
    layout.ml = clamp(50 * w / REF_W, 44, 84);
    layout.mr = clamp(10 * w / REF_W, 10, 24);
    layout.mt = clamp(10 * h / REF_H, 10, 24);
    layout.mb = clamp(20 * h / REF_H, 20, 40);
    layout.gw = Math.max(1, w - layout.ml - layout.mr);
    layout.gh = Math.max(1, h - layout.mt - layout.mb);
    layout.cw = layout.gw / GRID_COLS;
    layout.ch = layout.gh / GRID_ROWS;
}

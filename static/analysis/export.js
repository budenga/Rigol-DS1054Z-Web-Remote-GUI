/**
 * export.js — PNG / CSV / NPZ downloads and file import for the Power analysis view.
 */

import { lightColors } from '../theme.js';

function _stamp() {
    const d = new Date();
    const p = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}_${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
}

export function downloadBlob(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
}

/**
 * PNG of the plot at 2x device pixels, with overlay box, cursor labels and a footer line.
 * @param {import('./plot.js').Plot} plot
 * @param {() => object} getColors current token set
 * @param {string} footer capture source, time stamp, channels, samples, Δt
 * @param {boolean} print render once with the light token set
 */
export async function exportPng(plot, getColors, footer, print = false) {
    const scale = 2;
    const w = plot.w;
    const h = plot.h;
    const strip = 28;
    const colors = print ? lightColors() : getColors();
    const cv = document.createElement('canvas');
    cv.width = w * scale;
    cv.height = (h + strip) * scale;
    const ctx = cv.getContext('2d');
    ctx.scale(scale, scale);
    ctx.fillStyle = colors['plot-bg'];
    ctx.fillRect(0, 0, w, h + strip);
    plot.renderTo(ctx, w, h, colors, { hover: false });
    ctx.fillStyle = colors['text-dim'];
    ctx.font = '12px ui-sans-serif, sans-serif';
    ctx.textBaseline = 'middle';
    ctx.textAlign = 'left';
    ctx.fillText(footer, 8, h + strip / 2);
    const blob = await new Promise((res) => cv.toBlob(res, 'image/png'));
    if (!blob) throw new Error('Could not render the PNG');
    downloadBlob(blob, `DS1054Z_power${print ? '_print' : ''}_${_stamp()}.png`);
}

async function _fetchOk(url) {
    const r = await fetch(url);
    if (!r.ok) {
        let msg = `${r.status} ${r.statusText}`;
        try { msg = (await r.json()).error || msg; } catch { /* not JSON */ }
        throw new Error(msg);
    }
    return r;
}

/** Window-only CSV (t_s,v,i,p) — exactly the samples inside [t0, t1]. */
export async function downloadCsv(t0, t1, rev) {
    const r = await _fetchOk(`/analysis/export.csv?t0=${encodeURIComponent(t0)}&t1=${encodeURIComponent(t1)}&rev=${rev}`);
    downloadBlob(await r.blob(), `DS1054Z_window_${_stamp()}.csv`);
}

export async function downloadNpz(rev) {
    const r = await _fetchOk(`/analysis/export.npz?rev=${rev}`);
    downloadBlob(await r.blob(), `DS1054Z_capture_${_stamp()}.npz`);
}

/**
 * POST a file to /analysis/import. Resolves {ok, ...}; on a "channel missing"
 * answer it resolves {ok:false, channels:[...]} so the caller can ask.
 */
export async function importFile(file, vCh, iCh) {
    const q = new URLSearchParams({ name: file.name });
    if (vCh) q.set('v_ch', vCh);
    if (iCh) q.set('i_ch', iCh);
    let r;
    try {
        r = await fetch(`/analysis/import?${q}`, { method: 'POST', body: file });
    } catch (e) {
        return { ok: false, error: `Upload failed: ${e.message}` };
    }
    try {
        return { status: r.status, ...(await r.json()) };
    } catch {
        return { ok: false, status: r.status, error: `Server answered ${r.status}` };
    }
}

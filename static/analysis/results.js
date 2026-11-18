/**
 * results.js — the Results table (name · value · source tag) and the lines of the
 * overlay box drawn on the image.
 */

import { getSelection, describeEntry } from './params.js';

function _esc(s) {
    return String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

/**
 * @param {HTMLElement} tbody
 * @param {object|null} stats      window_stats() of the server
 * @param {object} scopeParams     scope measurements that came with the capture
 * @param {{windowIsFull:boolean, incoherent:boolean}} opts
 */
export function renderResults(tbody, stats, scopeParams, opts) {
    const rows = [];
    const clippedV = stats?.n_clipped_v || 0;
    const clippedI = stats?.n_clipped_i || 0;
    if (clippedV > 0 || clippedI > 0) {
        rows.push(`<tr class="warn"><td class="r-name">⚠ Clipped samples excluded</td>
            <td class="r-val">V ${clippedV.toLocaleString('en-US')} · I ${clippedI.toLocaleString('en-US')}</td>
            <td class="r-tag">calc</td></tr>`);
    }
    if (opts.incoherent) {
        rows.push(`<tr class="warn"><td class="r-name" colspan="3">⚠ Channels may be from different triggers (Free mode read)</td></tr>`);
    }
    for (const e of getSelection()) {
        if (!e.table) continue;
        const d = describeEntry(e, stats, scopeParams);
        const tag = d.tag === 'scope'
            ? `<span class="tag-scope">scope · full screen</span>${opts.windowIsFull ? '' : '<br>not windowed'}`
            : 'calc';
        rows.push(`<tr${d.warn ? ' class="warn"' : ''}><td class="r-name">${_esc(d.label)}</td>
            <td class="r-val">${_esc(d.text)}</td><td class="r-tag">${tag}</td></tr>`);
    }
    if (!rows.length) rows.push('<tr><td class="r-name" colspan="3" style="color:var(--text-dim)">Nothing selected for the table.</td></tr>');
    tbody.innerHTML = rows.join('');
}

/** [{label, value}] for the parameters marked "Image". */
export function overlayLines(stats, scopeParams, incoherent = false) {
    const out = [];
    // the image must carry the same warnings as the table
    if (stats && (stats.n_clipped_v > 0 || stats.n_clipped_i > 0)) {
        out.push({ label: '\u26a0 clipped excl.', value: `V ${stats.n_clipped_v} \u00b7 I ${stats.n_clipped_i}` });
    }
    if (incoherent) out.push({ label: '\u26a0 not coherent', value: 'free read' });
    for (const e of getSelection()) {
        if (!e.image) continue;
        const d = describeEntry(e, stats, scopeParams);
        out.push({ label: d.label + (d.tag === 'scope' ? '*' : ''), value: d.text });
    }
    return out;
}

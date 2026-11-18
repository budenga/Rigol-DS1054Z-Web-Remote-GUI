/**
 * view.js — the "Power analysis" view: layout wiring, server messages, toolbar.
 *
 * Client → server: raw_config, analysis_config / _hold / _select / _window / _view, acq cancel.
 * Server → client: analysis_meta / _state / _stats / _envelope, raw_progress / raw_done, acq_state.
 * The plot draws from the server envelope; cursor drags send `analysis_window` at most
 * every 50 ms (plus once on release); zoom/pan/resize request a new envelope (debounced).
 */

import { sendMessage } from '../app.js';
import { on, loadPref, savePref } from '../bus.js';
import { toast } from '../toast.js';
import { themeColors } from '../theme.js';
import { setCaptureSource } from '../acquisition.js';
import { registerParamSource, syncParams } from '../paramsync.js';
import { openPopover, closePopover } from '../popover.js';
import { Plot } from './plot.js';
import { initParams, getScopeSpecs } from './params.js';
import { renderResults, overlayLines } from './results.js';
import { formatTime, parseTime, formatInt } from './format.js';
import { exportPng, downloadCsv, downloadNpz, importFile } from './export.js';

const $ = (id) => document.getElementById(id);

let plot = null;
let colors = null;
let meta = null;                 // analysis_meta of the displayed capture
let stats = null;
let scopeParams = {};
let astate = { v_ch: 1, i_ch: 4, i_threshold: 1, hold: false, held_count: 0, has_capture: false };
let acq = null;
let source = 'screen';
let reqW = 0;
let reqV = 0;
let lastThr = null;
let pendingFile = null;
let els = {};

// --- helpers ---

function throttle(fn, ms) {
    let last = 0, timer = 0, args = null;
    return (...a) => {
        args = a;
        const now = performance.now();
        if (now - last >= ms) { last = now; clearTimeout(timer); timer = 0; fn(...a); }
        else if (!timer) {
            timer = setTimeout(() => { timer = 0; last = performance.now(); fn(...args); }, ms - (now - last));
        }
    };
}

function debounce(fn, ms) {
    let t = 0;
    return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

const isVisible = () => !els.view.hidden;
const mdepLabel = (n) => (n >= 1e6 ? `${n / 1e6} Mpts` : `${n / 1e3} kpts`);

// --- init ---

export function initAnalysisView() {
    els = {
        view: $('view-analysis'), canvas: $('an-canvas'), empty: $('an-empty'),
        t0: $('an-t0'), t1: $('an-t1'), ithr: $('an-ithr'), hold: $('an-hold'),
        held: $('an-held'), heldText: $('an-held-text'), progress: $('an-progress'),
        progText: $('an-prog-text'), progBar: $('an-prog-bar'), depth: $('an-depth'),
        chanBtn: $('an-chan-btn'), chanPop: $('an-chan-pop'), vch: $('an-vch'), ich: $('an-ich'),
        incoherent: $('an-incoherent'), info: $('an-info'), resultsBody: $('an-results-body'),
        resultsMeta: $('an-results-meta'), resultsNote: $('an-results-note'),
        file: $('an-file'), csvPop: $('an-csv-pop'),
    };
    colors = themeColors();
    source = loadPref('ds1054z.anSource', 'screen') === 'raw' ? 'raw' : 'screen';

    plot = new Plot(els.canvas, {
        getColors: () => colors,
        getChannelColors: (c) => ({ v: c[`ch${astate.v_ch}`], i: c[`ch${astate.i_ch}`] }),
        onWindow: (a, b, final) => {
            _syncInputs();
            if (final) { flushWindow(a, b); } else sendWindowThrottled(a, b);
            _renderResults();
        },
        onView: () => requestViewDebounced(),
    });
    const sendWindowThrottled = throttle((a, b) => flushWindow(a, b), 50);

    initParams(
        { list: $('an-param-list'), addBtn: $('an-add-btn'), addPop: $('an-add-pop') },
        () => { _renderResults(); syncParams(); },
    );
    registerParamSource(getScopeSpecs);

    _bindToolbar();
    _bindUnderBar();
    _bindKeys();
    _renderSource();
    _renderPill();

    on('analysis_meta', _onMeta);
    on('analysis_state', _onState);
    on('analysis_stats', _onStats);
    on('analysis_envelope', _onEnvelope);
    on('acq_state', _onAcq);
    on('raw_progress', _onProgress);
    on('raw_done', _onRawDone);
    document.addEventListener('themechange', () => { colors = themeColors(); plot.draw(); });

    if (new URLSearchParams(location.search).get('dev') === '1') {
        import('./dev.js').then((m) => m.initDev({ inject: devInject, plot: () => plot })).catch(() => {});
    }
}

/** Called when the view becomes visible. */
export function onViewActive() {
    setCaptureSource(source);
    plot?._resize();
    if (meta) { requestViewDebounced(); }
}

// --- server messages ---

function _onMeta(m) {
    const wasFull = !meta || _windowIsFull();
    meta = m;
    scopeParams = m.scope_params || {};
    stats = null;
    plot.setCapture({ t_start: m.t_start, t_end: m.t_end, dt: m.dt, n: m.n });
    if (wasFull) plot.setWindow(m.t_start, m.t_end);       // "Window = all" follows the new capture
    els.empty.hidden = true;
    els.incoherent.hidden = m.meta?.coherent !== false;
    els.info.hidden = false;
    const label = { screen: 'Screen', raw: 'RAW memory', csv: 'CSV file', npz: 'NPZ file' }[m.source] || m.source;
    els.info.textContent = `${label} · ${formatInt(m.n)} pts · Δt ${formatTime(m.dt, 4)}/sample`;
    _syncInputs();
    flushWindow(plot.win.a, plot.win.b);
    requestView();
    _renderResults();
    _renderNote();
}

function _onState(s) {
    const thrChanged = lastThr !== null && lastThr !== s.i_threshold;
    astate = s;
    lastThr = s.i_threshold;
    if (document.activeElement !== els.ithr) els.ithr.value = String(s.i_threshold);
    els.hold.setAttribute('aria-pressed', String(!!s.hold));
    els.held.hidden = !(s.held_count > 0);
    els.heldText.textContent = `${s.held_count} newer capture${s.held_count === 1 ? '' : 's'} held`;
    _renderPill();
    if (!s.has_capture) { els.empty.hidden = false; }
    if (thrChanged && meta) flushWindow(plot.win.a, plot.win.b);
    plot.draw();
}

function _onStats(d) {
    if (!meta || d.rev !== meta.rev || d.req < reqW) return;
    stats = d.stats;
    _renderResults();
}

function _onEnvelope(d) {
    if (!meta || d.rev !== meta.rev || d.req < reqV) return;
    plot.setEnvelope(d.env);
}

function _onAcq(s) {
    acq = s;
    const opts = s.mdep_options || [];
    if (els.depth.dataset.opts !== opts.join()) {
        els.depth.innerHTML = opts.map((n) => `<option value="${n}">${mdepLabel(n)}</option>`).join('');
        els.depth.dataset.opts = opts.join();
    }
    els.depth.querySelector('option[data-stale]')?.remove();
    if (!opts.includes(s.raw_mdep)) {          // channel count changed: the server resets it on the next arm
        els.depth.insertAdjacentHTML('afterbegin',
            `<option value="${s.raw_mdep}" data-stale disabled>${mdepLabel(s.raw_mdep)} (not valid now)</option>`);
    }
    if (document.activeElement !== els.depth) els.depth.value = String(s.raw_mdep);
    if (!s.busy_raw) els.progress.hidden = true;
}

function _onProgress(d) {
    const chans = [...new Set([astate.v_ch, astate.i_ch])];
    const idx = Math.max(0, chans.indexOf(d.ch));
    const overall = Math.min(1, (idx * d.total + d.done) / (chans.length * d.total));
    els.progress.hidden = false;
    els.progText.textContent = `RAW CH${d.ch} ${Math.round(overall * 100)}% · ETA ${d.eta_s.toFixed(1)} s`;
    els.progBar.style.transform = `scaleX(${overall})`;
}

function _onRawDone(d) {
    els.progress.hidden = true;
    const mbs = (d.bytes_per_s / 1e6).toFixed(2);
    toast(`RAW read: ${formatInt(d.n)} pts × ${d.channels.length} ch in ${d.seconds.toFixed(1)} s (${mbs} MB/s)`, 'ok');
}

// --- requests ---

function flushWindow(a, b) {
    if (!meta || meta.dev) return;
    sendMessage({ type: 'analysis_window', req: ++reqW, t0: a, t1: b });
}

function requestView() {
    if (!meta || meta.dev) return;
    sendMessage({ type: 'analysis_view', req: ++reqV, t0: plot.view.v0, t1: plot.view.v1, width: plot.plotWidth });
}
const requestViewDebounced = debounce(() => requestView(), 50);

// --- rendering ---

function _windowIsFull() {
    if (!meta) return true;
    const h = meta.dt / 2;
    return Math.abs(plot.win.a - meta.t_start) < h && Math.abs(plot.win.b - meta.t_end) < h;
}

function _renderResults() {
    renderResults(els.resultsBody, stats, scopeParams, {
        windowIsFull: _windowIsFull(), incoherent: meta?.meta?.coherent === false,
    });
    els.resultsMeta.textContent = stats && stats.n_window
        ? `Δt ${formatTime(stats.dt_window, 4)} · ${formatInt(stats.n_window)} samples`
        : (meta ? 'empty window' : '');
    plot.setOverlay(stats && stats.n_window ? overlayLines(stats, scopeParams, meta?.meta?.coherent === false) : []);
}

function _renderNote() {
    if (!meta) { els.resultsNote.textContent = ''; return; }
    const m = meta.meta || {};
    const parts = [];
    if (meta.source === 'raw' && m.bytes_per_s) parts.push(`RAW read at ${(m.bytes_per_s / 1e6).toFixed(2)} MB/s`);
    if (m.file) parts.push(m.file);
    if (m.srate) parts.push(`${(m.srate / 1e6).toFixed(3)} MSa/s`);
    els.resultsNote.textContent = parts.join(' · ');
}

function _syncInputs() {
    if (document.activeElement !== els.t0) { els.t0.value = formatTime(plot.win.a); els.t0.classList.remove('invalid'); }
    if (document.activeElement !== els.t1) { els.t1.value = formatTime(plot.win.b); els.t1.classList.remove('invalid'); }
}

function _renderPill() {
    els.chanBtn.textContent = `V: CH${astate.v_ch} · I: CH${astate.i_ch}`;
    els.vch.value = String(astate.v_ch);
    els.ich.value = String(astate.i_ch);
}

function _renderSource() {
    for (const b of document.querySelectorAll('#an-source button')) {
        b.setAttribute('aria-checked', String(b.dataset.source === source));
    }
    els.depth.disabled = source !== 'raw';
    $('an-depth-field').style.opacity = source === 'raw' ? '1' : '0.5';
    setCaptureSource(source);
}

// --- toolbar ---

function _bindToolbar() {
    $('an-source').addEventListener('click', (e) => {
        const b = e.target.closest('button[data-source]');
        if (!b) return;
        source = b.dataset.source;
        savePref('ds1054z.anSource', source);
        _renderSource();
    });
    els.depth.addEventListener('change', () => sendMessage({ type: 'raw_config', mdep: Number(els.depth.value) }));

    $('an-open').addEventListener('click', () => els.file.click());
    els.file.addEventListener('change', () => {
        const f = els.file.files[0];
        els.file.value = '';
        if (f) _openFile(f);
    });

    els.chanBtn.addEventListener('click', () => {
        if (els.chanPop.classList.contains('open')) closePopover(els.chanPop);
        else openPopover(els.chanPop, els.chanBtn);
    });
    $('an-chan-apply').addEventListener('click', () => {
        const v = Number(els.vch.value), i = Number(els.ich.value);
        if (v === i) { toast('Voltage and current must be different channels', 'warn'); return; }
        sendMessage({ type: 'analysis_config', v_ch: v, i_ch: i });
        closePopover(els.chanPop);
    });

    els.ithr.addEventListener('change', () => {
        const v = parseFloat(els.ithr.value.replace(',', '.'));
        if (!Number.isFinite(v) || v < 0) { els.ithr.value = String(astate.i_threshold); return; }
        sendMessage({ type: 'analysis_config', i_threshold: v });
    });

    els.hold.addEventListener('click', () => {
        sendMessage({ type: 'analysis_hold', on: els.hold.getAttribute('aria-pressed') !== 'true' });
    });
    $('an-held-show').addEventListener('click', () => {
        sendMessage({ type: 'analysis_select', source: source === 'raw' ? 'last_raw' : 'last_screen' });
    });
    $('an-prog-cancel').addEventListener('click', () => sendMessage({ type: 'acq', cmd: 'cancel' }));
}

async function _openFile(file) {
    const r = await importFile(file, astate.v_ch, astate.i_ch);
    if (r.status === 422 && Array.isArray(r.channels)) { _askChannels(file, r); return; }
    if (!r.ok) { toast(r.error || 'Could not open the file', 'error'); return; }
    toast(`Opened ${file.name}`, 'ok');
}

function _askChannels(file, r) {
    pendingFile = file;
    $('an-csv-msg').textContent = `${r.error}. Pick the channels that hold voltage and current.`;
    const opts = r.channels.map((c) => `<option value="${c}">CH${c}</option>`).join('');
    $('an-csv-v').innerHTML = opts;
    $('an-csv-i').innerHTML = opts;
    if (r.channels.length > 1) $('an-csv-i').value = String(r.channels[1]);
    openPopover(els.csvPop, $('an-open'));
}

$('an-csv-cancel')?.addEventListener('click', () => closePopover($('an-csv-pop')));

// --- under-plot bar ---

function _bindUnderBar() {
    const apply = (which, el) => {
        const t = parseTime(el.value);
        if (!meta || !Number.isFinite(t)) { el.classList.add('invalid'); return; }
        const c = Math.max(meta.t_start, Math.min(meta.t_end, t));
        const a = which === 'a' ? c : plot.win.a;
        const b = which === 'b' ? c : plot.win.b;
        plot.setWindow(a, b);
        el.classList.remove('invalid');
        _syncInputs();
        flushWindow(plot.win.a, plot.win.b);
        _renderResults();
    };
    for (const [which, el] of [['a', els.t0], ['b', els.t1]]) {
        el.addEventListener('change', () => apply(which, el));
        el.addEventListener('keydown', (e) => { if (e.key === 'Enter') el.blur(); });
        el.addEventListener('blur', () => _syncInputs());
    }
    $('an-all').addEventListener('click', windowAll);
    $('an-zoomwin').addEventListener('click', () => { if (meta) plot.zoomToWindow(); });
    $('an-resetzoom').addEventListener('click', () => { if (meta) plot.resetView(); });

    const footer = () => {
        const when = new Date((meta.ts || Date.now() / 1000) * 1000).toLocaleString();
        const dtw = stats ? formatTime(stats.dt_window, 4) : '-';
        return `${meta.source} · ${when} · V CH${meta.v_ch} · I CH${meta.i_ch} · ` +
               `${formatInt(meta.n)} samples · window Δt ${dtw} · sample interval ${formatTime(meta.dt, 4)}` +
               (stats && (stats.n_clipped_v || stats.n_clipped_i)
                   ? ` \u00b7 clipped samples excluded (V ${stats.n_clipped_v}, I ${stats.n_clipped_i}): energy/charge extrapolated` : '') +
               (meta.meta?.coherent === false ? ' \u00b7 channels may be from different triggers' : '') +
               ' \u00b7 * scope values: whole screen';
    };
    const guard = (fn) => async () => {
        if (!meta) { toast('Nothing to export yet', 'warn'); return; }
        try { await fn(); } catch (e) { toast(e.message, 'error'); }
    };
    $('an-png').addEventListener('click', guard(() => exportPng(plot, () => colors, footer(), false)));
    $('an-png-print').addEventListener('click', guard(() => exportPng(plot, () => colors, footer(), true)));
    $('an-csv').addEventListener('click', guard(() => downloadCsv(plot.win.a, plot.win.b, meta.rev)));
    $('an-npz').addEventListener('click', guard(() => downloadNpz(meta.rev)));

    $('an-csv-ok').addEventListener('click', async () => {
        const f = pendingFile;
        closePopover(els.csvPop);
        if (!f) return;
        const v = Number($('an-csv-v').value), i = Number($('an-csv-i').value);
        if (v === i) { toast('Voltage and current must be different channels', 'warn'); return; }
        const r = await importFile(f, v, i);
        if (!r.ok) toast(r.error || 'Could not open the file', 'error');
        else toast(`Opened ${f.name}`, 'ok');
    });
}

function windowAll() {
    if (!meta) return;
    plot.setWindow(meta.t_start, meta.t_end);
    _syncInputs();
    flushWindow(plot.win.a, plot.win.b);
    _renderResults();
}

// --- keyboard (only while this view is visible; ignored inside inputs) ---

function _bindKeys() {
    document.addEventListener('keydown', (e) => {
        if (!isVisible() || e.metaKey || e.ctrlKey || e.altKey) return;
        const t = e.target;
        if (t && (t.tagName === 'INPUT' || t.tagName === 'SELECT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
        if (e.key === '[' || e.key === ']') {
            e.preventDefault();
            plot.moveActiveCursor((e.key === ']' ? 1 : -1) * (e.shiftKey ? 10 : 1));
            _syncInputs();
            _renderResults();
        } else if (e.key === 'Tab' && !e.shiftKey && (t === document.body || t === els.canvas)) {
            e.preventDefault();
            plot.switchActiveCursor();
        } else if (e.key === 'a' || e.key === 'A') {
            e.preventDefault();
            windowAll();
        } else if (e.key === 'z' || e.key === 'Z') {
            e.preventDefault();
            if (meta) plot.zoomToWindow();
        }
    });
}

/** Dev-only (?dev=1): show synthetic worst-case data without touching the server. */
function devInject({ m, env, st, sp, window: win }) {
    meta = { ...m, dev: true, rev: -1 };
    scopeParams = sp || {};
    stats = st;
    plot.setCapture({ t_start: m.t_start, t_end: m.t_end, dt: m.dt, n: m.n });
    if (win) plot.setWindow(win[0], win[1]); else plot.setWindow(m.t_start, m.t_end);
    plot.setEnvelope(env);
    els.empty.hidden = true;
    els.info.hidden = false;
    els.info.textContent = `DEV \u00b7 ${formatInt(m.n)} pts \u00b7 \u0394t ${formatTime(m.dt, 4)}/sample`;
    _syncInputs();
    _renderResults();
}

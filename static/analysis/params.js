/**
 * params.js — the parameter catalogue and its picker.
 *
 * One list mixes *computed* parameters (from the window, rigol/analysis.py) and
 * *scope* parameters (":MEAS:ITEM? item,source" over the whole screen, queried
 * only at capture time). Each entry has two toggles: Table and Image.
 * The selection persists in localStorage.
 */

import { loadPref, savePref } from '../bus.js';
import { openPopover, closePopover } from '../popover.js';
import { formatSI, formatFixed, formatInt } from './format.js';

const STORE = 'ds1054z.params.v1';

/** Computed parameters: key of window_stats(), label, unit, kind */
export const COMPUTED = [
    { key: 'v_avg', label: 'Vavg', unit: 'V' },
    { key: 'i_avg', label: 'Iavg', unit: 'A' },
    { key: 'p_avg', label: 'Pavg', unit: 'W' },
    { key: 'v_rms', label: 'Vrms', unit: 'V' },
    { key: 'i_rms', label: 'Irms', unit: 'A' },
    { key: 'p_rms', label: 'Prms', unit: 'W' },
    { key: 'p_max', label: 'Ppeak', unit: 'W' },
    { key: 'p_min', label: 'Pmin', unit: 'W' },
    { key: 'energy_j', label: 'Energy', unit: 'J' },
    { key: 'energy_mwh', label: 'Energy', unit: 'mWh', fixed: true },
    { key: 'energy_wh', label: 'Energy', unit: 'Wh' },
    { key: 'charge_mah', label: 'Charge', unit: 'mAh', fixed: true },
    { key: 'charge_c', label: 'Charge', unit: 'C' },
    { key: 'dt_window', label: 'Window Δt', unit: 's' },
    { key: 'v_min', label: 'Vmin', unit: 'V' },
    { key: 'v_max', label: 'Vmax', unit: 'V' },
    { key: 'i_min', label: 'Imin', unit: 'A' },
    { key: 'i_max', label: 'Ipk', unit: 'A' },
    { key: 'crest_i', label: 'Crest I', unit: '', fixed: true },
    { key: 'v_rest', label: 'V rest', unit: 'V' },
    { key: 'v_drop', label: 'V drop', unit: 'V' },
    { key: 'r_app', label: 'R app', unit: 'Ω' },
    { key: 't_above', label: 'Time above threshold', unit: 's' },
    { key: 'n_pulses', label: 'Pulses', unit: '', int: true },
    { key: 't_pulse_max', label: 'Longest pulse', unit: 's' },
    { key: 'n_window', label: 'Samples in window', unit: '', int: true },
    { key: 'n_valid', label: 'Valid samples', unit: '', int: true },
    { key: 'n_clipped_v', label: 'Clipped V samples', unit: '', int: true },
    { key: 'n_clipped_i', label: 'Clipped I samples', unit: '', int: true },
    { key: 'sample_dt', label: 'Sample interval', unit: 's' },
];

export const SCOPE_ITEMS = [
    'VMAX', 'VMIN', 'VPP', 'VTOP', 'VBASe', 'VAMP', 'VAVG', 'VRMS', 'OVERshoot', 'PREShoot',
    'MARea', 'MPARea', 'PERiod', 'FREQuency', 'RTIMe', 'FTIMe', 'PWIDth', 'NWIDth', 'PDUTy',
    'NDUTy', 'RDELay', 'FDELay', 'RPHase', 'FPHase', 'TVMAX', 'TVMIN', 'PSLEWrate', 'NSLEWrate',
    'VUPPer', 'VMID', 'VLOWer', 'VARIance', 'PVRMS', 'PPULses', 'NPULses', 'PEDGes', 'NEDGes',
];
export const TWO_SOURCE_ITEMS = ['RDELay', 'FDELay', 'RPHase', 'FPHase'];
export const SCOPE_SOURCES = ['CHANnel1', 'CHANnel2', 'CHANnel3', 'CHANnel4', 'MATH'];

const DEFAULT_IMAGE = ['v_avg', 'i_avg', 'p_avg', 'v_rms', 'i_rms', 'p_rms', 'p_max', 'energy_j', 'energy_mwh', 'charge_mah', 'dt_window'];
const DEFAULT_TABLE_EXTRA = ['v_drop', 'n_pulses', 'n_clipped_v', 'n_clipped_i'];

let _sel = [];            // [{id, kind:'calc'|'scope', table, image}]
let _onChange = () => {};
let _els = {};

const _calcDef = (key) => COMPUTED.find((c) => c.key === key);

function _defaults() {
    return [
        ...DEFAULT_IMAGE.map((k) => ({ id: k, kind: 'calc', table: true, image: true })),
        ...DEFAULT_TABLE_EXTRA.map((k) => ({ id: k, kind: 'calc', table: true, image: false })),
    ];
}

function _valid(e) {
    if (!e || typeof e.id !== 'string') return false;
    if (e.kind === 'calc') return !!_calcDef(e.id);
    return e.kind === 'scope' && /^[A-Za-z]{3,10},(CHAN\w{0,6}\d?|MATH)(,(CHAN\w{0,6}\d?|MATH))?$/i.test(e.id);
}

/** Canonical SCPI spec for the server: item as given, sources as CHANnelN -> CHANN */
export function scopeSpec(item, srcA, srcB) {
    const s = (x) => (x === 'MATH' ? 'MATH' : x.replace('CHANnel', 'CHAN'));
    return srcB ? `${item},${s(srcA)},${s(srcB)}` : `${item},${s(srcA)}`;
}

export function scopeLabel(spec) {
    const [item, ...srcs] = spec.split(',');
    return `${item} · ${srcs.map((s) => s.replace('CHAN', 'CH')).join(' / ')}`;
}

export function scopeUnit(spec) {
    const [item, src] = spec.split(',');
    const it = item.toUpperCase();
    if (/^(VMAX|VMIN|VPP|VTOP|VBAS|VAMP|VAVG|VRMS|VUPP|VMID|VLOW|PVRMS)/.test(it)) return 'V';
    if (/^(PER|RTIM|FTIM|PWID|NWID|RDEL|FDEL|TVMAX|TVMIN)/.test(it)) return 's';
    if (it.startsWith('FREQ')) return 'Hz';
    if (it.startsWith('MARE') || it.startsWith('MPARE')) return src && src.toUpperCase() === 'MATH' ? 'J' : 'V·s';
    if (/SLEW/.test(it)) return 'V/s';
    if (it.startsWith('VARI')) return 'V²';
    if (/PHAS/.test(it)) return '°';
    return '';
}

export function initParams(els, onChange) {
    _els = els;
    _onChange = onChange;
    const saved = loadPref(STORE, null);
    _sel = Array.isArray(saved) ? saved.filter(_valid) : _defaults();
    if (!_sel.length) _sel = _defaults();
    _migrate(Array.isArray(saved));
    _buildAddPopover();
    _renderList();
}

/** One-time additions for selections saved before a parameter existed in the defaults. */
const MIGRATIONS = [
    { flag: 'ds1054z.params.mig.rms', keys: ['v_rms', 'i_rms', 'p_rms'], after: 'p_avg' },
];

function _migrate(hadSaved) {
    let changed = false;
    for (const m of MIGRATIONS) {
        if (loadPref(m.flag, false)) continue;
        savePref(m.flag, true);
        if (!hadSaved) continue;                       // fresh defaults already include them
        let at = _sel.findIndex((e) => e.kind === 'calc' && e.id === m.after);
        for (const k of m.keys) {
            if (_sel.some((e) => e.kind === 'calc' && e.id === k)) continue;
            const entry = { id: k, kind: 'calc', table: true, image: true };
            if (at >= 0) _sel.splice(++at, 0, entry); else _sel.push(entry);
            changed = true;
        }
    }
    if (changed) savePref(STORE, _sel);
}

export function getSelection() { return _sel; }

/** Scope specs that should be queried at capture time (Table or Image ticked). */
export function getScopeSpecs() {
    return _sel.filter((e) => e.kind === 'scope' && (e.table || e.image)).map((e) => e.id);
}

function _commit() {
    savePref(STORE, _sel);
    _renderList();
    _onChange();
}

/** @returns {{label:string, text:string, tag:'calc'|'scope', warn?:boolean}} */
export function describeEntry(e, stats, scopeParams) {
    if (e.kind === 'calc') {
        const d = _calcDef(e.id);
        const v = stats ? stats[e.id] : undefined;
        let text;
        if (v === undefined) text = '—';
        else if (d.int) text = formatInt(v);
        else if (d.fixed) text = formatFixed(v, d.unit);
        else text = formatSI(v, d.unit);
        const warn = (e.id === 'n_clipped_v' || e.id === 'n_clipped_i') && v > 0;
        return { label: d.label, text, tag: 'calc', warn };
    }
    const v = scopeParams ? scopeParams[e.id] : undefined;
    const unit = scopeUnit(e.id);
    const text = v === undefined ? '—' : v === null ? 'n/a' : formatSI(v, unit);
    return { label: scopeLabel(e.id), text, tag: 'scope' };
}

// --- picker UI ---

function _renderList() {
    const ul = _els.list;
    ul.innerHTML = '';
    _sel.forEach((e, i) => {
        const li = document.createElement('li');
        li.className = 'param-row';
        const name = e.kind === 'calc' ? _calcDef(e.id).label + (_calcDef(e.id).unit ? ` (${_calcDef(e.id).unit})` : '') : scopeLabel(e.id);
        li.innerHTML = `
            <span class="p-name">${_esc(name)}${e.kind === 'scope' ? '<span class="p-tag">scope · full screen</span>' : ''}</span>
            <label><input type="checkbox" data-k="table" ${e.table ? 'checked' : ''}> Table</label>
            <label><input type="checkbox" data-k="image" ${e.image ? 'checked' : ''}> Image</label>
            <span class="p-actions">
                <button class="icon-btn" data-act="up" title="Move up" aria-label="Move up" ${i === 0 ? 'disabled' : ''}>↑</button>
                <button class="icon-btn" data-act="down" title="Move down" aria-label="Move down" ${i === _sel.length - 1 ? 'disabled' : ''}>↓</button>
                <button class="icon-btn" data-act="remove" title="Remove" aria-label="Remove">×</button>
            </span>`;
        li.querySelectorAll('input[type=checkbox]').forEach((cb) => {
            cb.addEventListener('change', () => { e[cb.dataset.k] = cb.checked; _commit(); });
        });
        li.querySelectorAll('button[data-act]').forEach((b) => {
            b.addEventListener('click', () => {
                const act = b.dataset.act;
                if (act === 'remove') _sel.splice(i, 1);
                else if (act === 'up' && i > 0) [_sel[i - 1], _sel[i]] = [_sel[i], _sel[i - 1]];
                else if (act === 'down' && i < _sel.length - 1) [_sel[i + 1], _sel[i]] = [_sel[i], _sel[i + 1]];
                _commit();
            });
        });
        ul.appendChild(li);
    });
    if (!_sel.length) {
        const li = document.createElement('li');
        li.className = 'param-row';
        li.textContent = 'No parameters selected. Use "Add parameter".';
        ul.appendChild(li);
    }
}

function _esc(s) {
    return String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

function _buildAddPopover() {
    const pop = _els.addPop;
    pop.innerHTML = `
        <h3>Computed</h3>
        <ul class="pop-list" id="an-add-calc"></ul>
        <h3 style="margin-top:12px">Scope measurement <small>(whole screen)</small></h3>
        <div class="row"><label for="an-add-item">Item</label><select class="select" id="an-add-item"></select></div>
        <div class="row"><label for="an-add-srca">Source</label><select class="select" id="an-add-srca"></select></div>
        <div class="row" id="an-add-srcb-row" hidden><label for="an-add-srcb">Source B</label><select class="select" id="an-add-srcb"></select></div>
        <div class="actions"><button class="btn" id="an-add-scope">Add scope parameter</button></div>`;
    const item = pop.querySelector('#an-add-item');
    const a = pop.querySelector('#an-add-srca');
    const b = pop.querySelector('#an-add-srcb');
    item.innerHTML = SCOPE_ITEMS.map((i) => `<option>${i}</option>`).join('');
    const srcOpts = SCOPE_SOURCES.map((s) => `<option value="${s}">${s.replace('CHANnel', 'CH')}</option>`).join('');
    a.innerHTML = srcOpts;
    b.innerHTML = srcOpts;
    b.value = 'CHANnel2';
    item.addEventListener('change', () => {
        pop.querySelector('#an-add-srcb-row').hidden = !TWO_SOURCE_ITEMS.includes(item.value);
    });
    pop.querySelector('#an-add-scope').addEventListener('click', () => {
        const two = TWO_SOURCE_ITEMS.includes(item.value);
        if (two && a.value === b.value) { b.focus(); return; }
        const id = scopeSpec(item.value, a.value, two ? b.value : null);
        if (!_sel.some((e) => e.id === id)) _sel.push({ id, kind: 'scope', table: true, image: false });
        _commit();
        closePopover(pop);
    });
    _els.addBtn.addEventListener('click', () => {
        _refreshCalcList();
        if (pop.classList.contains('open')) closePopover(pop);
        else openPopover(pop, _els.addBtn);
    });
}

function _refreshCalcList() {
    const ul = _els.addPop.querySelector('#an-add-calc');
    ul.innerHTML = '';
    const left = COMPUTED.filter((c) => !_sel.some((e) => e.kind === 'calc' && e.id === c.key));
    if (!left.length) ul.innerHTML = '<li style="padding:6px 8px;color:var(--text-dim)">All computed parameters are in the list.</li>';
    for (const c of left) {
        const li = document.createElement('li');
        li.innerHTML = `<button><span>${_esc(c.label)}</span><small>${_esc(c.unit || c.key)}</small></button>`;
        li.firstChild.addEventListener('click', () => {
            _sel.push({ id: c.key, kind: 'calc', table: true, image: false });
            _commit();
            _refreshCalcList();
        });
        ul.appendChild(li);
    }
}

/** Re-render the picker after the selection array was edited from outside (dev tools). */
export function refreshParams() {
    _renderList();
    _onChange();
}

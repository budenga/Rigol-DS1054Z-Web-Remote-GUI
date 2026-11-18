/**
 * measurements.js — scope-measured values that arrived *with the capture*
 * (never polled): Freq, Period, Vpp, Vrms, Vmax, Vmin per displayed channel.
 *
 * "Measure on capture" controls whether those items are requested at all; the
 * specs go to the server through paramsync.js and are queried once per capture.
 */

import { formatSI } from './controls.js';
import { loadPref, savePref } from './bus.js';
import { registerParamSource, syncParams } from './paramsync.js';

const MEAS_ITEMS = [
    { item: 'FREQuency', label: 'Freq',   unit: 'Hz' },
    { item: 'PERiod',    label: 'Period', unit: 's'  },
    { item: 'VPP',       label: 'Vpp',    unit: 'V'  },
    { item: 'VRMS',      label: 'Vrms',   unit: 'V'  },
    { item: 'VMAX',      label: 'Vmax',   unit: 'V'  },
    { item: 'VMIN',      label: 'Vmin',   unit: 'V'  },
];

let _tbody = null;
let _enabled = true;
let _chOn = [false, false, false, false];
let _lastParams = null;

export function initMeasurements() {
    _tbody = document.getElementById('meas-tbody');
    _enabled = loadPref('ds1054z.measOnCapture', true);
    const cb = document.getElementById('meas-enable');
    if (cb) {
        cb.checked = _enabled;
        cb.addEventListener('change', () => {
            _enabled = cb.checked;
            savePref('ds1054z.measOnCapture', _enabled);
            syncParams();
            _render();
        });
    }
    registerParamSource(() => {
        if (!_enabled) return [];
        const out = [];
        for (let ch = 1; ch <= 4; ch++) {
            if (!_chOn[ch - 1]) continue;
            for (const m of MEAS_ITEMS) out.push(`${m.item},CHAN${ch}`);
        }
        return out;
    });
    _build();
}

/** Track which channels are displayed (to query only those). */
export function updateMeasurementChannels(state) {
    const now = [1, 2, 3, 4].map((ch) => !!state[`ch${ch}`]?.on);
    if (now.join() !== _chOn.join()) {
        _chOn = now;
        syncParams();
    }
}

/** @param {Object<string, number|null>} params capture.scope_params */
export function updateMeasurements(params) {
    _lastParams = params || {};
    _render();
}

function _build() {
    if (!_tbody) return;
    _tbody.innerHTML = '';
    for (const m of MEAS_ITEMS) {
        const tr = document.createElement('tr');
        tr.innerHTML = `<td class="meas-label">${m.label}</td>` +
            [1, 2, 3, 4].map((ch) => `<td class="meas-ch${ch}" data-key="${m.item},CHAN${ch}" data-unit="${m.unit}">--</td>`).join('');
        _tbody.appendChild(tr);
    }
}

function _render() {
    if (!_tbody) return;
    for (const td of _tbody.querySelectorAll('td[data-key]')) {
        const v = _lastParams?.[td.dataset.key];
        td.textContent = (v == null || Number.isNaN(v)) ? '--' : formatSI(v, td.dataset.unit);
        td.title = td.textContent;           // full value when the narrow column ellipsizes it
    }
    const note = document.getElementById('meas-note');
    if (note) {
        note.textContent = !_enabled ? 'Not measured (switched off).'
            : _lastParams ? 'Whole screen, at capture time.' : 'Values appear with the next capture.';
    }
}

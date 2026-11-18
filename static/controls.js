/**
 * controls.js — Control panel for Rigol DS1054Z Web Remote.
 *
 * Handles:
 * - Run/Stop/Single/Auto/Force buttons (scope controls; the capture modes live in acquisition.js)
 * - Channel enable toggles
 * - V/div, offset knobs per channel (scroll wheel, 1-2-5 sequence)
 * - Time/div knob (scroll wheel, 1-2-5 sequence)
 * - Coupling, probe selectors per channel
 * - Trigger: source, slope, level, sweep
 * - All controls send JSON via WebSocket
 * - State sync from server updates UI
 */

import { sendControl, sendScpi } from './app.js';

// --- 1-2-5 sequences ---

// V/div: 1mV to 10V in 1-2-5 steps
const VDIV_SEQ = [
    1e-3, 2e-3, 5e-3,
    10e-3, 20e-3, 50e-3, 100e-3, 200e-3, 500e-3,
    1, 2, 5, 10,
];

// Time/div: 5ns to 50s in 1-2-5 steps
const TDIV_SEQ = [
    5e-9, 10e-9, 20e-9, 50e-9,
    100e-9, 200e-9, 500e-9,
    1e-6, 2e-6, 5e-6,
    10e-6, 20e-6, 50e-6,
    100e-6, 200e-6, 500e-6,
    1e-3, 2e-3, 5e-3,
    10e-3, 20e-3, 50e-3,
    100e-3, 200e-3, 500e-3,
    1, 2, 5, 10, 20, 50,
];

// --- State ---

let _state = null;

// --- Throttle for scroll wheel events ---
const _throttleTimers = {};

/**
 * Throttle a callback: only fires once per `ms` milliseconds per key.
 */
function throttled(key, ms, fn) {
    if (_throttleTimers[key]) return; // already pending
    fn();
    _throttleTimers[key] = setTimeout(() => {
        delete _throttleTimers[key];
    }, ms);
}

// --- SI prefix formatter ---

const SI_PREFIXES = [
    { exp: 9,   prefix: 'G' },
    { exp: 6,   prefix: 'M' },
    { exp: 3,   prefix: 'k' },
    { exp: 0,   prefix: ''  },
    { exp: -3,  prefix: 'm' },
    { exp: -6,  prefix: '\u00b5' },
    { exp: -9,  prefix: 'n' },
    { exp: -12, prefix: 'p' },
];

/**
 * Format a value with SI prefix and unit.
 * @param {number} value
 * @param {string} unit
 * @returns {string}
 */
export function formatSI(value, unit) {
    if (value == null || isNaN(value)) return '--';
    if (value === 0) return `0 ${unit}`;
    const abs = Math.abs(value);
    if (!isFinite(value) || abs >= 1e12) return `${value.toExponential(2)} ${unit}`;
    for (const { exp, prefix } of SI_PREFIXES) {
        const threshold = Math.pow(10, exp);
        if (abs >= threshold * 0.999) {
            const scaled = value / threshold;
            const digits = abs >= threshold * 100 ? 0
                         : abs >= threshold * 10  ? 1
                         : abs >= threshold        ? 2
                         : 3;
            return scaled.toFixed(digits) + ' ' + prefix + unit;
        }
    }
    return value.toExponential(2) + ' ' + unit;
}

/**
 * Find nearest index in a sorted sequence.
 */
function nearestIdx(seq, val) {
    let best = 0;
    let bestDist = Infinity;
    for (let i = 0; i < seq.length; i++) {
        const d = Math.abs(Math.log10(seq[i]) - Math.log10(val));
        if (d < bestDist) {
            bestDist = d;
            best = i;
        }
    }
    return best;
}

/**
 * Step up/down in a 1-2-5 sequence.
 */
function stepSeq(seq, current, direction) {
    let idx = nearestIdx(seq, current);
    idx += direction > 0 ? 1 : -1;
    idx = Math.max(0, Math.min(seq.length - 1, idx));
    return seq[idx];
}

// --- Init ---

export function initControls() {
    _bindButtons();
    _bindChannelToggles();
    _buildChannelControls();
    _bindTriggerControls();
    _bindTimebaseKnobs();
    _bindCollapsibleSections();
    _bindDecoderControls();
}

// --- Buttons ---

function _bindButtons() {
    document.getElementById('btn-run').addEventListener('click', () => sendControl('run'));
    document.getElementById('btn-stop').addEventListener('click', () => sendControl('stop'));
    document.getElementById('btn-single').addEventListener('click', () => sendControl('single'));
    document.getElementById('btn-auto').addEventListener('click', () => sendControl('autoscale'));
    document.getElementById('btn-force').addEventListener('click', () => sendControl('tforce'));
}

// --- Channel toggles ---

function _bindChannelToggles() {
    const container = document.getElementById('ch-toggles');
    container.querySelectorAll('.ch-toggle').forEach(el => {
        el.addEventListener('click', () => {
            const ch = parseInt(el.dataset.ch, 10);
            const currentlyOn = el.classList.contains('on');
            sendControl('set_channel_display', { ch, on: !currentlyOn });
        });
    });
}

// --- Per-channel controls ---

function _buildChannelControls() {
    const container = document.getElementById('channel-controls');
    container.innerHTML = '';

    for (let ch = 1; ch <= 4; ch++) {
        const group = document.createElement('div');
        group.className = `channel-group collapsed`;
        group.id = `ch-group-${ch}`;
        group.innerHTML = `
            <div class="channel-group-header">
                <span style="font-weight:600; font-size:12px; color:var(--ch${ch})">CH${ch}</span>
            </div>
            <div class="channel-group-body">
                <div class="knob-control ch${ch}" data-knob="vdiv" data-ch="${ch}">
                    <span class="knob-label">V/div</span>
                    <span class="knob-value" id="ch${ch}-vdiv-val">1 V</span>
                </div>
                <div class="knob-control ch${ch}" data-knob="offset" data-ch="${ch}">
                    <span class="knob-label">Offset</span>
                    <span class="knob-value" id="ch${ch}-offset-val">0 V</span>
                </div>
                <div class="ctrl-select">
                    <label>Coupling</label>
                    <select id="ch${ch}-coupling">
                        <option value="DC">DC</option>
                        <option value="AC">AC</option>
                        <option value="GND">GND</option>
                    </select>
                </div>
                <div class="ctrl-select">
                    <label>Probe</label>
                    <select id="ch${ch}-probe">
                        <option value="0.01">0.01x</option>
                        <option value="0.02">0.02x</option>
                        <option value="0.05">0.05x</option>
                        <option value="0.1">0.1x</option>
                        <option value="0.2">0.2x</option>
                        <option value="0.5">0.5x</option>
                        <option value="1" selected>1x</option>
                        <option value="2">2x</option>
                        <option value="5">5x</option>
                        <option value="10">10x</option>
                        <option value="20">20x</option>
                        <option value="50">50x</option>
                        <option value="100">100x</option>
                        <option value="200">200x</option>
                        <option value="500">500x</option>
                        <option value="1000">1000x</option>
                    </select>
                </div>
            </div>
        `;
        container.appendChild(group);

        // V/div scroll wheel (throttled — max 1 command per 300ms)
        const vdivKnob = group.querySelector(`[data-knob="vdiv"][data-ch="${ch}"]`);
        vdivKnob.addEventListener('wheel', (e) => {
            e.preventDefault();
            throttled(`vdiv-${ch}`, 300, () => {
                const cs = _state?.[`ch${ch}`];
                if (!cs) return;
                const dir = e.deltaY < 0 ? 1 : -1;
                const newVal = stepSeq(VDIV_SEQ, cs.scale, dir);
                if (newVal === cs.scale) return; // already at limit
                console.log(`CH${ch} V/div: ${cs.scale} → ${newVal}`);
                sendControl('set_channel_scale', { ch, val: newVal });
            });
        }, { passive: false });

        // Offset scroll wheel (throttled)
        const offKnob = group.querySelector(`[data-knob="offset"][data-ch="${ch}"]`);
        offKnob.addEventListener('wheel', (e) => {
            e.preventDefault();
            throttled(`offset-${ch}`, 300, () => {
                const cs = _state?.[`ch${ch}`];
                if (!cs) return;
                const step = cs.scale * 0.4; // bigger step for faster response
                const dir = e.deltaY < 0 ? 1 : -1;
                const newVal = Math.round((cs.offset + step * dir) * 1e6) / 1e6;
                console.log(`CH${ch} offset: ${cs.offset} → ${newVal}`);
                sendControl('set_channel_offset', { ch, val: newVal });
            });
        }, { passive: false });

        // Coupling selector
        const coupSel = group.querySelector(`#ch${ch}-coupling`);
        coupSel.addEventListener('change', () => {
            sendControl('set_channel_coupling', { ch, mode: coupSel.value });
        });

        // Probe selector
        const probeSel = group.querySelector(`#ch${ch}-probe`);
        probeSel.addEventListener('change', () => {
            sendControl('set_channel_probe', { ch, val: parseFloat(probeSel.value) });
        });
    }
}

// --- Trigger controls ---

function _bindTriggerControls() {
    const srcSel = document.getElementById('trig-source');
    srcSel.addEventListener('change', () => {
        sendControl('set_trigger_source', { src: srcSel.value });
    });

    const slopeSel = document.getElementById('trig-slope');
    slopeSel.addEventListener('change', () => {
        sendControl('set_trigger_slope', { slope: slopeSel.value });
    });

    const sweepSel = document.getElementById('trig-sweep');
    sweepSel.addEventListener('change', () => {
        sendControl('set_trigger_sweep', { sweep: sweepSel.value });
    });

    // Trigger level knob (scroll wheel, throttled)
    const trigLevelKnob = document.getElementById('trig-level-knob');
    trigLevelKnob.addEventListener('wheel', (e) => {
        e.preventDefault();
        throttled('trig-level', 300, () => {
            if (!_state) return;
            const src = _state.trigger?.source || 'CHAN1';
            const chNum = parseInt(src.replace('CHAN', ''), 10) || 1;
            const cs = _state[`ch${chNum}`];
            const step = cs ? cs.scale * 0.04 : 0.1;
            const dir = e.deltaY < 0 ? 1 : -1;
            const newVal = (_state.trigger?.level || 0) + step * dir;
            sendControl('set_trigger_level', { val: newVal });
        });
    }, { passive: false });
}

// --- Timebase knobs ---

function _bindTimebaseKnobs() {
    const tdivKnob = document.getElementById('time-div-knob');
    tdivKnob.addEventListener('wheel', (e) => {
        e.preventDefault();
        throttled('tdiv', 300, () => {
            if (!_state) return;
            const dir = e.deltaY < 0 ? 1 : -1;
            const newVal = stepSeq(TDIV_SEQ, _state.timebase_scale, dir);
            if (newVal === _state.timebase_scale) return;
            console.log(`Time/div: ${_state.timebase_scale} → ${newVal}`);
            sendControl('set_timebase_scale', { val: newVal });
        });
    }, { passive: false });

    const tOffKnob = document.getElementById('time-offset-knob');
    tOffKnob.addEventListener('wheel', (e) => {
        e.preventDefault();
        throttled('toff', 300, () => {
            if (!_state) return;
            const step = _state.timebase_scale;
            const dir = e.deltaY < 0 ? 1 : -1;
            const newVal = _state.timebase_offset + step * dir;
            console.log(`Time offset: ${_state.timebase_offset} → ${newVal}`);
            sendControl('set_timebase_offset', { val: newVal });
        });
    }, { passive: false });
}

// --- Decoder controls ---

/**
 * Show the config panel that matches the selected decoder mode, hide others.
 */
function _showDecoderConfig(n, mode) {
    const protocols = ['uart', 'iic', 'spi', 'par'];
    for (const p of protocols) {
        const el = document.getElementById(`dec${n}-${p}-cfg`);
        if (el) el.style.display = 'none';
    }
    if (mode !== 'OFF') {
        const key = mode === 'PAR' ? 'par' : mode.toLowerCase();
        const cfg = document.getElementById(`dec${n}-${key}-cfg`);
        if (cfg) cfg.style.display = '';
    }
}

function _bindDecoderControls() {
    for (let n = 1; n <= 2; n++) {
        // Collapsible header
        const header = document.querySelector(`.decoder-slot-header[data-dec="${n}"]`);
        const body = document.getElementById(`dec-body-${n}`);
        if (header && body) {
            header.addEventListener('click', () => {
                const collapsed = body.style.display === 'none';
                body.style.display = collapsed ? '' : 'none';
                const icon = header.querySelector('.decoder-collapse-icon');
                if (icon) icon.textContent = collapsed ? '\u25BC' : '\u25B6';
            });
        }

        // Mode selector
        const modeSel = document.getElementById(`dec${n}-mode`);
        if (modeSel) {
            modeSel.addEventListener('change', () => {
                const mode = modeSel.value;
                sendControl('set_decoder_mode', { dec: n, mode });
                _showDecoderConfig(n, mode);
            });
        }

        // Display toggle
        const dispToggle = document.getElementById(`dec${n}-display`);
        if (dispToggle) {
            dispToggle.addEventListener('change', () => {
                sendControl('set_decoder_display', { dec: n, on: dispToggle.checked });
            });
        }

        // Format selector
        const fmtSel = document.getElementById(`dec${n}-format`);
        if (fmtSel) {
            fmtSel.addEventListener('change', () => {
                sendControl('set_decoder_format', { dec: n, fmt: fmtSel.value });
            });
        }

        // --- UART config ---
        _bindSelect(`dec${n}-uart-tx`, (v) => sendScpi(`:DEC${n}:UART:TX ${v}`));
        _bindSelect(`dec${n}-uart-rx`, (v) => sendScpi(`:DEC${n}:UART:RX ${v}`));
        _bindInput(`dec${n}-uart-baud`, (v) => sendScpi(`:DEC${n}:UART:BAUD ${v}`));
        _bindSelect(`dec${n}-uart-pol`, (v) => sendScpi(`:DEC${n}:UART:POL ${v}`));
        _bindSelect(`dec${n}-uart-width`, (v) => sendScpi(`:DEC${n}:UART:WIDT ${v}`));
        _bindSelect(`dec${n}-uart-stop`, (v) => sendScpi(`:DEC${n}:UART:STOP ${v}`));
        _bindSelect(`dec${n}-uart-par`, (v) => sendScpi(`:DEC${n}:UART:PAR ${v}`));
        _bindSelect(`dec${n}-uart-end`, (v) => sendScpi(`:DEC${n}:UART:END ${v}`));

        // --- IIC config ---
        _bindSelect(`dec${n}-iic-clk`, (v) => sendScpi(`:DEC${n}:IIC:CLK ${v}`));
        _bindSelect(`dec${n}-iic-data`, (v) => sendScpi(`:DEC${n}:IIC:DATA ${v}`));
        _bindSelect(`dec${n}-iic-addr`, (v) => sendScpi(`:DEC${n}:IIC:ADDR ${v}`));

        // --- SPI config ---
        _bindSelect(`dec${n}-spi-clk`, (v) => sendScpi(`:DEC${n}:SPI:CLK ${v}`));
        _bindSelect(`dec${n}-spi-miso`, (v) => sendScpi(`:DEC${n}:SPI:MISO ${v}`));
        _bindSelect(`dec${n}-spi-mosi`, (v) => sendScpi(`:DEC${n}:SPI:MOSI ${v}`));
        _bindSelect(`dec${n}-spi-cs`, (v) => sendScpi(`:DEC${n}:SPI:CS ${v}`));
        _bindSelect(`dec${n}-spi-edge`, (v) => sendScpi(`:DEC${n}:SPI:EDGE ${v}`));
        _bindSelect(`dec${n}-spi-pol`, (v) => sendScpi(`:DEC${n}:SPI:POL ${v}`));
        _bindSelect(`dec${n}-spi-end`, (v) => sendScpi(`:DEC${n}:SPI:END ${v}`));
        _bindInput(`dec${n}-spi-width`, (v) => sendScpi(`:DEC${n}:SPI:WIDT ${v}`));

        // --- PARALLEL config ---
        _bindSelect(`dec${n}-par-clk`, (v) => sendScpi(`:DEC${n}:PAR:CLK ${v}`));
        _bindSelect(`dec${n}-par-edge`, (v) => sendScpi(`:DEC${n}:PAR:EDGE ${v}`));
        _bindInput(`dec${n}-par-width`, (v) => sendScpi(`:DEC${n}:PAR:WIDT ${v}`));
    }
}

/** Bind a <select> element change event. */
function _bindSelect(id, handler) {
    const el = document.getElementById(id);
    if (el) el.addEventListener('change', () => handler(el.value));
}

/** Bind a number <input> element change event. */
function _bindInput(id, handler) {
    const el = document.getElementById(id);
    if (el) el.addEventListener('change', () => handler(el.value));
}

// --- Collapsible sections ---

function _bindCollapsibleSections() {
    const header = document.getElementById('decoder-header');
    const body = document.getElementById('decoder-body');
    const arrow = header?.querySelector('.collapse-arrow');
    if (!header || !body) return;

    let isOpen = false;
    header.addEventListener('click', () => {
        isOpen = !isOpen;
        body.style.display = isOpen ? 'block' : 'none';
        if (arrow) {
            arrow.textContent = isOpen ? '\u25BC' : '\u25B6'; // ▼ or ▶
        }
    });
}

// --- State update (called from app.js on "state" message) ---

export function updateControlsState(state) {
    _state = state;

    // Acquisition buttons — highlight based on scope trigger status
    // TD = triggered, AUTO = auto-triggering, RUN = running → Run is active
    // STOP = stopped → Stop is active
    // WAIT = waiting for trigger (single mode) → Single is active
    const trigStatus = state.trigger?.status || '';

    const btnRun = document.getElementById('btn-run');
    const btnStop = document.getElementById('btn-stop');
    const btnSingle = document.getElementById('btn-single');

    // Clear all active
    [btnRun, btnStop, btnSingle].forEach(b => { if (b) b.classList.remove('active'); });

    // Set the one that matches scope state
    if (trigStatus === 'STOP') {
        if (btnStop) btnStop.classList.add('active');
    } else if (trigStatus === 'WAIT') {
        if (btnSingle) btnSingle.classList.add('active');
    } else if (['TD', 'AUTO', 'RUN'].includes(trigStatus)) {
        if (btnRun) btnRun.classList.add('active');
    }

    // Channel toggles
    for (let ch = 1; ch <= 4; ch++) {
        const el = document.querySelector(`.ch-toggle[data-ch="${ch}"]`);
        const cs = state[`ch${ch}`];
        if (!el || !cs) continue;

        if (cs.on) {
            el.classList.remove('off');
            el.classList.add('on');
        } else {
            el.classList.remove('on');
            el.classList.add('off');
        }

        // Show/hide channel group
        const group = document.getElementById(`ch-group-${ch}`);
        if (group) {
            if (cs.on) {
                group.classList.remove('collapsed');
            } else {
                group.classList.add('collapsed');
            }
        }

        if (cs.on) {
            // V/div
            const vdivEl = document.getElementById(`ch${ch}-vdiv-val`);
            if (vdivEl) vdivEl.textContent = formatSI(cs.scale, 'V');

            // Offset
            const offEl = document.getElementById(`ch${ch}-offset-val`);
            if (offEl) offEl.textContent = formatSI(cs.offset, 'V');

            // Coupling
            const coupSel = document.getElementById(`ch${ch}-coupling`);
            if (coupSel && coupSel.value !== cs.coupling) {
                coupSel.value = cs.coupling;
            }

            // Probe
            const probeSel = document.getElementById(`ch${ch}-probe`);
            if (probeSel) {
                const probeStr = cs.probe.toString();
                if (probeSel.value !== probeStr) {
                    probeSel.value = probeStr;
                }
            }
        }
    }

    // Trigger
    const trig = state.trigger;
    if (trig) {
        const srcSel = document.getElementById('trig-source');
        if (srcSel && srcSel.value !== trig.source) srcSel.value = trig.source;

        const slopeSel = document.getElementById('trig-slope');
        if (slopeSel && slopeSel.value !== trig.slope) slopeSel.value = trig.slope;

        const sweepSel = document.getElementById('trig-sweep');
        if (sweepSel && sweepSel.value !== trig.sweep) sweepSel.value = trig.sweep;

        const trigLvlEl = document.getElementById('trig-level-val');
        if (trigLvlEl) trigLvlEl.textContent = formatSI(trig.level, 'V');
    }

    // Timebase
    const tdivEl = document.getElementById('time-div-val');
    if (tdivEl) tdivEl.textContent = formatSI(state.timebase_scale, 's');

    const tOffEl = document.getElementById('time-offset-val');
    if (tOffEl) tOffEl.textContent = formatSI(state.timebase_offset, 's');

    // Status bar
    _updateStatusBar(state);
}

function _updateStatusBar(state) {
    // Trigger status badge
    const trigEl = document.getElementById('status-trig');
    if (trigEl && state.trigger) {
        const st = state.trigger.status;
        trigEl.textContent = st;
        trigEl.className = 'status-trigger';
        if (st === 'TD' || st === 'RUN') trigEl.classList.add('run');
        else if (st === 'STOP') trigEl.classList.add('stop');
        else if (st === 'WAIT') trigEl.classList.add('wait');
        else if (st === 'AUTO') trigEl.classList.add('auto');
        else trigEl.classList.add('td');
    }

    // Time/div
    const timeEl = document.getElementById('status-time');
    if (timeEl) timeEl.textContent = formatSI(state.timebase_scale, 's') + '/div';

    // Sample rate
    const srateEl = document.getElementById('status-srate');
    if (srateEl) srateEl.textContent = state.sample_rate ? formatSI(state.sample_rate, 'Sa/s') : '--';

    // Memory depth
    const memEl = document.getElementById('status-mem');
    if (memEl) {
        const md = state.memory_depth;
        if (md && md !== 'AUTO') {
            const val = parseFloat(md);
            memEl.textContent = isNaN(val) ? md : formatSI(val, 'pts');
        } else {
            memEl.textContent = md || '--';
        }
    }
}

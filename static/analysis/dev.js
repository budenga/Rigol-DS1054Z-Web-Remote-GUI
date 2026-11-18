/**
 * dev.js — worst-case demo data for the Power analysis view, only loaded with ?dev=1.
 * Synthetic data are shown client-side; the server is not involved ("Demo data / Worst case").
 */

import { getSelection, refreshParams } from './params.js';
import { updateMeasurements } from '../measurements.js';

const SCENARIOS = {
    'normal-demo': 'Demo data (normal)',
    'big-12m': 'Worst case: 12 M-point capture',
    'all-clipped': 'Worst case: all-clipped V channel',
    'zero-window': 'Worst case: zero-length window',
    'extreme': 'Worst case: huge / tiny values',
    'many-params': 'Worst case: 30+ parameters, long names',
    'scope-extreme': 'Worst case: scope view measurements',
};

function emptyStats(n) {
    return {
        v_avg: null, i_avg: null, p_avg: null, energy_j: null, n_window: 0, dt_window: 0, n_valid: 0,
        n_clipped_v: n, n_clipped_i: 0, n_pulses: 0, sample_dt: 1e-6,
    };
}

function makeEnv(width, fn, n) {
    const o = { mode: 'envelope', n, t_a: 0, t_b: 1, width, v_min: [], v_max: [], i_min: [], i_max: [], p_min: [], p_max: [] };
    for (let k = 0; k < width; k++) {
        const { v, i } = fn(k / width);
        const vs = v === null ? [null, null] : [v - 0.05, v + 0.05];
        o.v_min.push(vs[0]); o.v_max.push(vs[1]);
        o.i_min.push(i - 0.02); o.i_max.push(i + 0.4);
        o.p_min.push(v === null ? null : (v * (i - 0.02)));
        o.p_max.push(v === null ? null : (v * (i + 0.4)));
    }
    return o;
}

const normalSignal = (x) => {
    const spike = [0.12, 0.47, 0.8].some((c) => Math.abs(x - c) < 0.004) ? 9 : 0;
    const burst = x > 0.3 && x < 0.38 ? 3 : 0.3;
    const i = Math.max(spike, burst);
    return { v: 23 - 0.1 * i, i };
};

const baseStats = (n, dt) => ({
    v_avg: 22.75, i_avg: 1.198, p_avg: 26.97, p_max: 233.2, p_min: -0.2, energy_j: 32.36, energy_wh: 0.00899,
    energy_mwh: 8.99, charge_c: 1.4376, charge_mah: 0.3993, dt_window: n * dt, n_window: n, n_valid: n,
    n_clipped_v: 0, n_clipped_i: 0, v_rms: 22.75, i_rms: 1.98, p_rms: 44.05, v_min: 21.4, v_max: 23.4, i_min: -0.2, i_max: 12.4,
    crest_i: 6.3, v_rest: 23.0, v_drop: 1.6, r_app: 0.13, t_above: 0.12, n_pulses: 7, t_pulse_max: 0.05, sample_dt: dt,
});

export function initDev({ inject, plot }) {
    const box = document.createElement('div');
    box.className = 'dev-toggle';
    box.innerHTML = `<label for="dev-sel">Demo data / Worst case</label><select class="select" id="dev-sel"></select>
        <button class="btn" id="dev-go">Show</button><button class="btn" id="dev-reset">Normal</button>`;
    document.body.appendChild(box);
    const sel = box.querySelector('select');
    sel.innerHTML = Object.entries(SCENARIOS).map(([k, v]) => `<option value="${k}">${v}</option>`).join('');
    box.querySelector('#dev-reset').addEventListener('click', () => location.reload());

    box.querySelector('#dev-go').addEventListener('click', () => {
        const id = sel.value;
        let n = 1200, dt = 1e-3;
        let m = () => ({ id: 1, ts: Date.now() / 1000, source: 'raw', n, dt, t_start: -n * dt / 2, t_end: n * dt / 2, v_ch: 1, i_ch: 4,
                         clipped: { v: 0, i: 0 }, scope_params: {}, meta: { coherent: true } });
        const toT = (env, mm) => { env.t_a = mm.t_start; env.t_b = mm.t_end; return env; };
        let env, st, sp = { 'VAVG,CHAN1': 22.75, 'VPP,CHAN1': 2.0 }, win;
        if (id === 'normal-demo') {
            const mm = m();
            env = toT(makeEnv(900, normalSignal, n), mm);
            st = baseStats(n, dt);
            inject({ m: mm, env, st, sp });
        } else if (id === 'big-12m') {
            n = 12_000_000; dt = 1e-7;
            const mm = m();
            env = toT(makeEnv(4000, normalSignal, n), mm);
            st = baseStats(n, dt);
            st.n_window = n; st.n_valid = n; st.dt_window = n * dt;
            inject({ m: mm, env, st, sp });
        } else if (id === 'all-clipped') {
            const mm = m();
            env = toT(makeEnv(900, (x) => ({ v: null, i: normalSignal(x).i }), n), mm);
            st = { ...baseStats(n, dt), v_avg: null, v_rms: null, v_min: null, v_max: null, p_avg: null, p_rms: null, p_max: null, p_min: null,
                   energy_j: null, energy_wh: null, energy_mwh: null, v_rest: null, v_drop: null, r_app: null,
                   n_clipped_v: n, n_valid: 0 };
            mm.clipped.v = n;
            inject({ m: mm, env, st, sp });
        } else if (id === 'zero-window') {
            const mm = m();
            env = toT(makeEnv(900, normalSignal, n), mm);
            st = emptyStats(0);
            inject({ m: mm, env, st, sp, window: [0.1, 0.1] });
        } else if (id === 'extreme') {
            const mm = m();
            env = toT(makeEnv(900, (x) => ({ v: 1.2e9 * (1 + x), i: 3e-12 * (1 + x) }), n), mm);
            st = { ...baseStats(n, dt), v_avg: 1.8e9, i_avg: 4.5e-12, p_avg: 8.1e-3, p_max: 1.2e308, energy_j: 1e-15,
                   energy_mwh: 2.5e-19, charge_mah: 1.2e12, dt_window: 3e9, n_window: 2 ** 40, r_app: 1e15 };
            inject({ m: mm, env, st, sp: { 'VAVG,CHAN1': 1e300, 'VPP,CHAN1': -1e-300 } });
        } else if (id === 'many-params') {
            const mm = m();
            env = toT(makeEnv(900, normalSignal, n), mm);
            st = baseStats(n, dt);
            const sel2 = getSelection();
            const items = ['VMAX', 'VMIN', 'VPP', 'VAVG', 'VRMS', 'VTOP', 'VBASe', 'VAMP', 'OVERshoot', 'PREShoot'];
            sp = {};
            for (const it of items) for (const c of ['CHAN1', 'CHAN2', 'CHAN3', 'CHAN4']) {
                const id2 = `${it},${c}`;
                if (!sel2.some((e) => e.id === id2)) sel2.push({ id: id2, kind: 'scope', table: true, image: true });
                sp[id2] = Math.random() * 1000 - 500;
            }
            const long = 'RDELay,CHAN1,CHAN2';
            if (!sel2.some((e) => e.id === long)) sel2.push({ id: long, kind: 'scope', table: true, image: true });
            sp[long] = 1.234e-6;
            refreshParams();
            inject({ m: mm, env, st, sp });
        } else if (id === 'scope-extreme') {
            updateMeasurements({
                'FREQuency,CHAN1': 1.2345e308, 'PERiod,CHAN1': 1e-300, 'VPP,CHAN1': -9.9999e99, 'VRMS,CHAN1': 0,
                'VMAX,CHAN4': 123456789.123, 'VMIN,CHAN4': -1e-15, 'VPP,CHAN2': null, 'VRMS,CHAN3': NaN,
            });
        }
        plot().draw();
    });
}

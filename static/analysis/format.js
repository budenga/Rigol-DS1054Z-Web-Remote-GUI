/**
 * format.js — number formatting for the Power analysis view.
 * SI prefixes, fixed significant digits (stable width), tabular figures come from CSS.
 */

const PREFIXES = [
    [9, 'G'], [6, 'M'], [3, 'k'], [0, ''], [-3, 'm'], [-6, 'µ'], [-9, 'n'], [-12, 'p'],
];

/** Decimals needed so that `abs/scale` shows `sig` significant digits. */
function _decimals(scaled, sig) {
    const a = Math.abs(scaled);
    if (a === 0) return Math.max(0, sig - 1);
    const mag = Math.floor(Math.log10(a));
    return Math.max(0, Math.min(9, sig - 1 - mag));
}

/**
 * @param {number|null|undefined} value
 * @param {string} unit  e.g. 'V', 'A', 's'
 * @param {number} [sig] significant digits (default 4)
 * @returns {string} e.g. "22.75 V", "1.198 A", "32.36 J", "n/a" for null
 */
export function formatSI(value, unit = '', sig = 4) {
    if (value === null || value === undefined || Number.isNaN(value)) return 'n/a';
    if (!Number.isFinite(value)) return value > 0 ? '+∞ ' + unit : '−∞ ' + unit;
    if (value === 0) return `${(0).toFixed(Math.max(0, sig - 1))} ${unit}`.trim();
    const abs = Math.abs(value);
    if (abs >= 1e12 || abs < 1e-15) return `${value.toExponential(sig - 1)} ${unit}`.trim();   // beyond the SI prefixes
    let exp = 0;
    let prefix = '';
    for (const [e, p] of PREFIXES) {
        if (abs >= Math.pow(10, e) * 0.9995) { exp = e; prefix = p; break; }
        exp = e; prefix = p;                          // below pico: stay at pico
    }
    let scaled = value / Math.pow(10, exp);
    let dec = _decimals(scaled, sig);
    // rounding can push 999.96 → "1000": move up one prefix
    if (Math.abs(Number(scaled.toFixed(dec))) >= 1000 && exp < 9) {
        const idx = PREFIXES.findIndex(([e]) => e === exp);
        if (idx > 0) {
            exp = PREFIXES[idx - 1][0];
            prefix = PREFIXES[idx - 1][1];
            scaled = value / Math.pow(10, exp);
            dec = _decimals(scaled, sig);
        }
    }
    return `${scaled.toFixed(dec)} ${prefix}${unit}`.trim();
}

/** Fixed-unit value without SI prefix scaling (mWh, mAh, %): `sig` significant digits. */
export function formatFixed(value, unit = '', sig = 4) {
    if (value === null || value === undefined || Number.isNaN(value)) return 'n/a';
    if (!Number.isFinite(value)) return '∞ ' + unit;
    const a = Math.abs(value);
    if (a !== 0 && (a < 1e-3 || a >= 1e9)) return `${value.toExponential(sig - 1)} ${unit}`.trim();   // no "0.000000000"
    return `${value.toFixed(_decimals(value, sig))} ${unit}`.trim();
}

export function formatInt(value) {
    if (value === null || value === undefined || Number.isNaN(value)) return 'n/a';
    return Math.round(value).toLocaleString('en-US');
}

/** Time value for the numeric inputs: SI unit chosen from magnitude, 6 significant digits. */
export function formatTime(t, sig = 6) {
    return formatSI(t, 's', sig);
}

/**
 * Parse "12.5 ms", "-204e-3", "1.2us", "3 s" (also "µs") into seconds; NaN if invalid.
 */
export function parseTime(text) {
    const m = /^\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?)\s*([a-zA-Zµμ]*)\s*$/i.exec(String(text));
    if (!m) return NaN;
    const v = parseFloat(m[1]);
    const u = m[2].toLowerCase().replace('µ', 'u').replace('μ', 'u');
    const mult = { '': 1, s: 1, ms: 1e-3, us: 1e-6, ns: 1e-9, ps: 1e-12, ks: 1e3 }[u];
    return mult === undefined ? NaN : v * mult;
}

/** Axis tick formatter for a time span: one common unit so labels line up. */
export function timeAxisFormatter(span) {
    const a = Math.abs(span);
    let unit = 's', scale = 1;
    if (a < 1e-6) { unit = 'ns'; scale = 1e9; }
    else if (a < 1e-3) { unit = 'µs'; scale = 1e6; }
    else if (a < 1) { unit = 'ms'; scale = 1e3; }
    const dec = Math.max(0, Math.min(6, 2 - Math.floor(Math.log10((a * scale) / 8 || 1))));
    return (t) => `${(t * scale).toFixed(dec)} ${unit}`;
}

/** "Nice" tick positions covering [min, max] with about `count` ticks. */
export function niceTicks(min, max, count) {
    if (!(max > min)) return [min];
    const raw = (max - min) / Math.max(1, count);
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const norm = raw / mag;
    const step = (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * mag;
    const out = [];
    for (let v = Math.ceil(min / step) * step; v <= max + step * 1e-9; v += step) {
        out.push(Math.abs(v) < step * 1e-9 ? 0 : v);
    }
    return out;
}

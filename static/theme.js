/**
 * theme.js — system / light / dark theme switch.
 *
 * `data-theme` on <html> is "light" or "dark"; for "system" the attribute is
 * removed so the CSS prefers-color-scheme media query decides. Canvas modules
 * read colours from the CSS tokens (themeColors) on init and on `themechange`.
 */

const KEY = 'ds1054z.theme';
const MODES = ['system', 'light', 'dark'];
const LABELS = { system: 'System', light: 'Light', dark: 'Dark' };
const TOKENS = [
    'bg', 'surface-1', 'surface-2', 'surface-3', 'border', 'border-strong', 'text', 'text-dim',
    'text-bright', 'accent', 'danger', 'warning', 'success', 'grid-major', 'grid-minor',
    'grid-center', 'plot-bg', 'plot-frame', 'ch1', 'ch2', 'ch3', 'ch4', 'math', 'cursor-a',
    'cursor-b', 'window-fill', 'overlay-bg',
];

let _mode = 'system';
let _btn = null;

function _read() {
    try {
        const v = localStorage.getItem(KEY);
        if (MODES.includes(v)) return v;
    } catch { /* ignore */ }
    return 'system';
}

function _write(v) {
    try { localStorage.setItem(KEY, v); } catch { /* ignore */ }
}

export function getThemeMode() { return _mode; }

export function applyTheme(mode, persist = true) {
    _mode = MODES.includes(mode) ? mode : 'system';
    const root = document.documentElement;
    if (_mode === 'system') root.removeAttribute('data-theme');
    else root.setAttribute('data-theme', _mode);
    if (persist) _write(_mode);
    if (_btn) {
        _btn.textContent = `Theme: ${LABELS[_mode]}`;
        _btn.title = `Theme: ${LABELS[_mode]} (T to change)`;
    }
    document.dispatchEvent(new CustomEvent('themechange', { detail: { mode: _mode } }));
}

export function cycleTheme() {
    applyTheme(MODES[(MODES.indexOf(_mode) + 1) % MODES.length]);
}

export function initTheme(button) {
    _btn = button;
    _mode = _read();
    applyTheme(_mode, false);
    button?.addEventListener('click', cycleTheme);
    // "system" follows the OS live
    window.matchMedia?.('(prefers-color-scheme: dark)').addEventListener?.('change', () => {
        if (_mode === 'system') document.dispatchEvent(new CustomEvent('themechange', { detail: { mode: _mode } }));
    });
}

/** Read all colour tokens from an element's computed style as {name: value}. */
export function themeColors(el = document.documentElement) {
    const cs = getComputedStyle(el);
    const out = {};
    for (const t of TOKENS) out[t] = cs.getPropertyValue('--' + t).trim();
    return out;
}

let _lightProbe = null;

/** The light token set regardless of the active theme (for "PNG (print)"). */
export function lightColors() {
    if (!_lightProbe) {
        _lightProbe = document.createElement('div');
        _lightProbe.setAttribute('data-theme', 'light');
        _lightProbe.style.display = 'none';
        document.body.appendChild(_lightProbe);
    }
    return themeColors(_lightProbe);
}

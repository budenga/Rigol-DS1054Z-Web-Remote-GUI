/**
 * bus.js — tiny publish/subscribe hub so modules do not import each other.
 *
 * app.js re-publishes every server JSON message as an event named after its
 * `type` (detail = msg.data, or the whole message for messages without data).
 */

const _bus = new EventTarget();

export function on(type, fn) {
    _bus.addEventListener(type, (e) => fn(e.detail));
}

export function emit(type, detail) {
    _bus.dispatchEvent(new CustomEvent(type, { detail }));
}

/** localStorage helpers that never throw (private windows, blocked storage). */
export function loadPref(key, fallback) {
    try {
        const v = localStorage.getItem(key);
        return v === null ? fallback : JSON.parse(v);
    } catch {
        return fallback;
    }
}

export function savePref(key, value) {
    try {
        localStorage.setItem(key, JSON.stringify(value));
    } catch { /* storage unavailable: preference just is not remembered */ }
}

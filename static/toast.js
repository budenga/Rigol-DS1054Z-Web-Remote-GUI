/**
 * toast.js — small non-blocking notifications (errors, warnings, RAW done).
 * Enter and leave from the same (right) edge, <= 250 ms; opacity + transform only.
 */

let _stack = null;
const _recent = new Map();

function _ensure() {
    if (!_stack) {
        _stack = document.createElement('div');
        _stack.className = 'toast-stack';
        _stack.setAttribute('role', 'status');
        _stack.setAttribute('aria-live', 'polite');
        document.body.appendChild(_stack);
    }
    return _stack;
}

/**
 * @param {string} msg
 * @param {'info'|'warn'|'error'|'ok'} [level]
 * @param {number} [ms] auto-dismiss delay (errors stay longer)
 */
export function toast(msg, level = 'info', ms) {
    if (!msg) return;
    const now = performance.now();
    if (_recent.get(msg) > now - 2000) return;     // collapse bursts of identical messages
    _recent.set(msg, now);

    const el = document.createElement('div');
    el.className = `toast ${level === 'warn' || level === 'error' || level === 'ok' ? level : 'info'}`;
    el.textContent = msg;
    const stack = _ensure();
    while (stack.children.length >= 4) stack.firstChild.remove();
    stack.appendChild(el);
    requestAnimationFrame(() => requestAnimationFrame(() => el.classList.add('show')));

    const life = ms ?? (level === 'error' ? 8000 : 4500);
    const close = () => {
        if (!el.isConnected) return;
        el.classList.remove('show');
        el.classList.add('leaving');
        setTimeout(() => el.remove(), 200);
    };
    const timer = setTimeout(close, life);
    el.addEventListener('click', () => { clearTimeout(timer); close(); });
}

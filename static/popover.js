/**
 * popover.js — open/close helper for menus anchored to a trigger button.
 *
 * Enter: opacity 0→1 and scale 0.96→1 (CSS, 170 ms, ease-out); transform-origin
 * at the trigger. Exit is faster (120 ms). Closes on outside click and Escape.
 */

const _open = new Set();

function _place(pop, trigger) {
    const r = trigger.getBoundingClientRect();
    const pw = pop.offsetWidth;
    const ph = pop.offsetHeight;
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    let left = Math.min(Math.max(8, r.left), vw - pw - 8);
    let top = r.bottom + 6;
    let originY = 0;
    if (top + ph > vh - 8 && r.top - ph - 6 > 8) {
        top = r.top - ph - 6;
        originY = ph;
    }
    pop.style.left = `${left}px`;
    pop.style.top = `${top}px`;
    pop.style.transformOrigin = `${Math.max(0, r.left + r.width / 2 - left)}px ${originY}px`;
}

export function isOpen(pop) { return pop.classList.contains('open'); }

export function openPopover(pop, trigger) {
    for (const p of [..._open]) if (p !== pop) closePopover(p);
    pop.hidden = false;
    _place(pop, trigger);
    // next frame so the closed state is painted first and the transition runs
    requestAnimationFrame(() => pop.classList.add('open'));
    pop._trigger = trigger;
    _open.add(pop);
}

export function closePopover(pop) {
    pop.classList.remove('open');
    _open.delete(pop);
    pop._trigger?.focus?.({ preventScroll: true });
}

document.addEventListener('pointerdown', (e) => {
    for (const p of [..._open]) {
        if (!p.contains(e.target) && !p._trigger?.contains(e.target)) {
            p.classList.remove('open');
            _open.delete(p);
        }
    }
}, true);

document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') for (const p of [..._open]) closePopover(p);
});

window.addEventListener('resize', () => {
    for (const p of _open) if (p._trigger) _place(p, p._trigger);
});

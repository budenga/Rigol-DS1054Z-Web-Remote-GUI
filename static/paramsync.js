/**
 * paramsync.js — tells the server which scope (:MEAS:ITEM?) parameters to query
 * at capture time. Several modules contribute specs; the union is sent.
 */

import { sendMessage } from './app.js';

const _sources = new Set();
let _timer = 0;

/** @param {() => string[]} fn returns the specs ("ITEM,SRC") this module wants */
export function registerParamSource(fn) {
    _sources.add(fn);
}

export function syncParams() {
    clearTimeout(_timer);
    _timer = setTimeout(sendParamsNow, 80);
}

export function sendParamsNow() {
    const items = new Set();
    for (const fn of _sources) for (const s of fn()) items.add(s);
    sendMessage({ type: 'params', items: [...items] });
}

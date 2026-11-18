/**
 * screenshot.js — Screenshot overlay and save functions for Rigol DS1054Z Web Remote.
 *
 * Features:
 * - Toggle button to show/hide screenshot overlay on the canvas
 * - Opacity slider (0-100%)
 * - Receive screenshot data (base64 PNG) from WebSocket
 * - Save buttons: screenshot (normal, inverted, grayscale), canvas PNG, CSV data
 */

import { sendMessage } from './app.js';
import {
    setScreenshotImage,
    setScreenshotVisible,
    setScreenshotOpacity,
    getChannelData,
} from './renderer.js';
import { formatSI } from './controls.js';

let _overlayVisible = false;
let _currentImg = null;

/**
 * Initialise the screenshot module. Call once after DOM ready.
 */
export function initScreenshot() {
    // Toggle
    const toggle = document.getElementById('screenshot-toggle');
    const opacityRow = document.getElementById('opacity-row');
    toggle.addEventListener('change', () => {
        _overlayVisible = toggle.checked;
        setScreenshotVisible(_overlayVisible);
        opacityRow.style.display = _overlayVisible ? 'flex' : 'none';

        // If enabling and we have no screenshot yet, request one
        if (_overlayVisible && !_currentImg) {
            sendMessage({ type: 'screenshot', args: {} });
        }
    });

    // Opacity slider
    const slider = document.getElementById('opacity-slider');
    const sliderVal = document.getElementById('opacity-val');
    slider.addEventListener('input', () => {
        const pct = parseInt(slider.value, 10);
        sliderVal.textContent = pct + '%';
        setScreenshotOpacity(pct / 100);
    });

    // Save buttons
    document.getElementById('btn-screenshot').addEventListener('click', () => {
        sendMessage({ type: 'screenshot', args: {} });
    });
    document.getElementById('btn-screenshot-inv').addEventListener('click', () => {
        sendMessage({ type: 'screenshot', args: { invert: true } });
    });
    document.getElementById('btn-screenshot-gray').addEventListener('click', () => {
        sendMessage({ type: 'screenshot', args: { grayscale: true } });
    });
    document.getElementById('btn-save-canvas').addEventListener('click', _saveCanvas);
    document.getElementById('btn-save-csv').addEventListener('click', _saveCSV);
}

/**
 * Handle incoming screenshot data (base64 PNG string).
 * @param {string} b64data
 */
export function handleScreenshotData(b64data) {
    const img = new Image();
    img.onload = () => {
        _currentImg = img;
        setScreenshotImage(img);

        // If the overlay is visible, the renderer will draw it.
        // Also offer a download.
        _downloadBlob(_b64toBlob(b64data, 'image/png'), _screenshotFilename());
    };
    img.src = 'data:image/png;base64,' + b64data;
}

// --- Save Canvas as PNG ---

function _saveCanvas() {
    const canvas = document.getElementById('scope-canvas');
    canvas.toBlob((blob) => {
        if (blob) _downloadBlob(blob, _canvasFilename());
    }, 'image/png');
}

// --- Save Data as CSV ---

function _saveCSV() {
    const channelData = getChannelData();
    if (channelData.size === 0) return;

    const lines = ['Channel,Sample,Raw,Volts,Time_s'];

    for (const [ch, data] of channelData) {
        const { raw, preamble } = data;
        for (let i = 0; i < raw.length; i++) {
            const val = raw[i];
            let volts = '';
            if (val !== 0 && val !== 255) {
                volts = ((val - preamble.yorig - preamble.yref) * preamble.yinc).toFixed(6);
            }
            const time = (preamble.xorig + i * preamble.xinc).toExponential(11);
            lines.push(`CH${ch},${i},${val},${volts},${time}`);
        }
    }

    const csv = lines.join('\n');
    const blob = new Blob([csv], { type: 'text/csv' });
    _downloadBlob(blob, _csvFilename());
}

// --- Helpers ---

function _downloadBlob(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
}

function _b64toBlob(b64, mime) {
    const bin = atob(b64);
    const arr = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) {
        arr[i] = bin.charCodeAt(i);
    }
    return new Blob([arr], { type: mime });
}

function _timestamp() {
    const d = new Date();
    return d.getFullYear().toString()
        + (d.getMonth() + 1).toString().padStart(2, '0')
        + d.getDate().toString().padStart(2, '0')
        + '_'
        + d.getHours().toString().padStart(2, '0')
        + d.getMinutes().toString().padStart(2, '0')
        + d.getSeconds().toString().padStart(2, '0');
}

function _screenshotFilename() {
    return `DS1054Z_screenshot_${_timestamp()}.png`;
}

function _canvasFilename() {
    return `DS1054Z_canvas_${_timestamp()}.png`;
}

function _csvFilename() {
    return `DS1054Z_data_${_timestamp()}.csv`;
}

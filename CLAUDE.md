# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Web-based remote control GUI and CLI for the Rigol DS1054Z oscilloscope (and DS1000Z/MSO1000Z series). Communicates via raw TCP SCPI on port 5555 — not VXI-11, not VISA. Provides a browser GUI with two views (**Scope** and **Power analysis**), a CLI, a fake scope for hardware-free development, and a macOS double-click launcher.

A sister project for the Rigol DL3021A electronic load reuses the same `rigol/transport.py`, `rigol/scheduler.py`, and `rigol/discovery.py` (the transport/scheduler API changed, see below, when porting).

`PLAN_power_analysis.md` (local only, git-ignored; not in the public repo) is the plan this version was built from (decisions, SCPI facts, formulas, acceptance checks).

## Running

```bash
# Runtime is Python 3.11 (Homebrew)
pip3.11 install -r requirements.txt -r requirements-dev.txt

# Web GUI — auto-discovers scope on the network
python3.11 run.py
python3.11 run.py 192.168.1.100          # specific scope IP
python3.11 run.py --fake                 # built-in fake scope (127.0.0.1:5556), no hardware is touched
python3.11 run.py --fake --no-browser --port 8082   # what .claude/launch.json uses
python3.11 run.py --debug [--log-dir DIR]           # DEBUG log file (full SCPI trace), default ~/Library/Logs/Rigol_DS1054Z
python3.11 run.py discover               # network discovery only

# CLI
python3.11 ds1054z-cli.py discover
python3.11 ds1054z-cli.py <ip> info
python3.11 ds1054z-cli.py <ip> shell
python3.11 ds1054z-cli.py <ip> screenshot --invert
python3.11 ds1054z-cli.py <ip> save-data --channels 1,2
python3.11 ds1054z-cli.py <ip> raw-data --channels 1,4 --mdep 600000 --out capture.npz [--single]
python3.11 ds1054z-cli.py --scope-port 5556 127.0.0.1 info    # against the fake (option goes before the IP)

# Tests (no hardware needed; they start the fake scope on an ephemeral port)
python3.11 -m pytest
python3.11 -c "import ast,sys; [ast.parse(open(f).read()) for f in sys.argv[1:]]" rigol/*.py server/*.py ds1054z-cli.py run.py
```

Never send commands to the real scope while developing: use `--fake`.

## Architecture

### Data flow

```
Browser (vanilla JS ES modules + Canvas 2D)
    ↕ WebSocket   binary 0x43 capture frames + JSON (control, acq, analysis, state, ...)
    ↕ HTTP        index.html, static assets, /analysis/import|export.*, /screenshot
FastAPI/uvicorn (Python 3.11)        server/app.py, server/ws_handler.py
    ↕
ClientHub (broadcast)  ·  AnalysisService (current analysis capture)   server/hub.py, server/analysis_service.py
    ↕
AcquisitionManager (one per scope, shared by all browser tabs)        rigol/acquisition.py
    ↕                          ↘ raw_reader.py (RAW memory)   ↘ analysis.py (numpy, no asyncio)
SCPIScheduler (asyncio.PriorityQueue, single consumer)                rigol/scheduler.py
    ↕
RawTCPTransport → DS1054Z scope on port 5555 (or rigol/fake_scope.py on 5556)
```

### Critical design constraint: single SCPI session

The DS1054Z only handles one SCPI connection at a time. **All** scope communication goes through the `SCPIScheduler` (one consumer coroutine). Never send SCPI outside it. Non-user SCPI traffic (trigger watcher, snapshot, RAW read, 5 s state poll) is owned by the `AcquisitionManager`; browser sockets never poll the scope.

### Transport (`rigol/transport.py`)

- **One persistent socket** for text *and* binary commands (the old disposable-socket scheme caused timeouts and desync; do not reintroduce it).
- Binary block replies (`#<n><len><payload>`) are followed by trailing bytes (`\n`, sometimes `\n\n\x00`). `_flush_trailing` drains them with a 5 ms-per-read timeout, stopping at the first quiet gap (no fixed 100 ms wait). `_read_block` and `_read_line` also tolerate stray leading `\n`/`\x00`.
- `query_waveform(ch)` = SOUR+MODE NORM+FORM BYTE+`:WAV:PRE?`+`:WAV:DATA?` in one write/one lock → `(payload, preamble_str)`.
- `query_preamble_raw(ch)` and `query_waveform_raw_chunk(ch, start, stop, timeout=30)` for RAW mode. The RAW preamble must be read after selecting RAW mode (YINC/YORIG/YREF differ from NORM).
- `TransportError.fatal` says whether the link is dead; the scheduler only reconnects when `transport.connected` is False (a scope "ERROR" reply to a RAW read leaves the stream in sync).

### Scheduler

While the link is down queued commands fail immediately (`TransportError(fatal=True)`) and the reconnect runs in a background task, so nothing replays after a Cancel; the trigger watcher rides out outages up to `OUTAGE_GRACE_S`. `AcquisitionManager._gen` is bumped by cancel/stop/run so an `arm()` stuck on a dead scope gives up when it resumes.

Priority levels: `CONTROL=0` (user buttons), `STATE=5`, `WAVEFORM=10`, `MEASURE=15`, `SCREENSHOT=20`. `CmdType` includes `QUERY_WAVEFORM`, `PREAMBLE_RAW`, `WAVEFORM_RAW_CHUNK`; structured arguments travel in `CommandTask.args`.

### Acquisition manager (`rigol/acquisition.py`)

Modes: **single** (arm `:SING`, wait for STOP, snapshot, stay stopped), **repeat** (re-arm after each capture, ≥ 200 ms apart), **free** (`:RUN`, nothing read automatically; `read_screen` snapshots without stopping and marks the capture `coherent=False`). All channels of a single/repeat capture are read while stopped, so they come from the *same acquisition*.
- Trigger watcher polls `:TRIG:STAT?` at ~5 Hz: after `:SING` wait for a non-STOP status or 600 ms, then for STOP. RUN/AUTO while armed (someone pressed RUN) → switch to free.
- `cancel()` restores RUN if the scope was running before the first `arm()`, otherwise stops it.
- Concurrent commands from several tabs are serialised by a lock; arming while armed with the same mode is idempotent. `cancel` bypasses the per-socket command queue.
- RAW flow: `raw_config` sets the depth (validated against `MDEP_VALID` for the enabled channel count; applied with `:ACQ:MDEP` *before* arming and read back — a differing read-back raises a toast), `raw_capture` arms, snapshots the screen, then `read_raw` for V and I. Controls are rejected while a RAW read runs ("the oscilloscope cannot be operated during the reading process").
- State: full `get_full_state()` on connect, then every 5 s; each control re-reads only the parameter it changed.

### RAW reader (`rigol/raw_reader.py`)

`read_raw(scope, channels, mdep, progress_cb, cancel_event)`: requires STOP, reads the RAW preamble per channel, then `:WAV:STAR/STOP` chunks of 250 000 bytes into a preallocated `uint8` array, verifying every chunk length (one retry, then `RawReadError`). Reports progress/ETA and total throughput (bytes/s).

### Analysis engine (`rigol/analysis.py`)

Pure numpy. `AnalysisCapture` (v, i float32 with NaN where raw is 0/255; `p = v*i` lazily). `window_stats(cap, t0, t1, cfg)` gives V/I/P averages, rms, peaks, energy (J, Wh, mWh), charge (C, mAh), crest, V rest/drop, apparent R, pulse stats; `p_avg = mean(v·i)`, `energy = p_avg · n_window · dt`. `envelope()` for drawing; `.npz` save/load (raw bytes + preambles → bit-exact), `.csv` import of the app's export, window CSV export (refuses > 2 M samples); also `v_rms`, `i_rms`, `p_rms = sqrt(mean((v·i)²))` (no physical use, shown for comparison; energy always uses `p_avg`). Golden values from `tests/fixtures/DS1054Z_data_20261118_103155.csv` are in `tests/test_analysis.py`.

### Logging (`rigol/logsetup.py`)

`setup_logging(debug, log_dir, app)` is idempotent (run.py calls it early to print the path, the server `lifespan` calls it again after uvicorn has reset its own loggers and removes the handlers at shutdown). Console INFO; with `--debug` a `RotatingFileHandler` (5 MB × 5, one file per run, newest 20 kept) records DEBUG for `rigol`/`server` plus uvicorn INFO. Debug traces live in `transport.py` (`SEND/DONE/BLOCK header/QUERY`) and the trigger watcher. `POST /logs/open` (loopback clients only) opens the log folder via `logsetup.open_folder`; the top-bar **Logs** button calls it. Tests: `tests/test_logging.py`. Never log binary payloads (lengths only) and never write logs inside the project folder.

### Fake scope (`rigol/fake_scope.py`)

Asyncio SCPI server: state queries, `:RUN/:STOP/:SING`, `:TRIG:STAT?` (WAIT → TD → STOP), `:ACQ:MDEP` validation per channel count, NORM and RAW `:WAV:*` (RAW only in STOP, ≤ 250 000 points per read, RAW preamble deliberately differs from NORM), `:MEAS:ITEM?`, `:DISP:DATA?`. 24 V rail with load-dependent sag. Tests use `tests/helpers.py::scope_stack()`.

### WebSocket protocol

**Server → browser binary** (one frame per channel of a screen capture; committed when the JSON `capture` arrives):
```
[0x43 'C', channel u8, capture_id u32 LE, 44-byte preamble <ddiddii, ...raw bytes]
```
Preamble `<ddiddii` = xinc, xorig, xref, yinc, yorig, yref, points.

**Server → browser JSON** `{"type", "data"}`: `state`, `acq_state` {mode, armed, trig_status, capture_count, last_ts, busy_raw, raw_mdep, mdep_options, scope_ok}, `capture` {id, ts, mode, coherent, channels, srate, mdep, tdiv, scope_params}, `raw_progress` {ch, done, total, eta_s}, `raw_done` {capture_id, n, channels, seconds, bytes_per_s}, `analysis_meta`, `analysis_state` {hold, held_count, v_ch, i_ch, i_threshold, ...}, `analysis_stats` {req, rev, stats}, `analysis_envelope` {req, rev, env}, `toast`, `error` (`msg`), `screenshot`, `scpi_response`.

**Browser → server JSON**: `control` {cmd, args}, `acq` {cmd: single|repeat|free|cancel|read_screen}, `params` {items: ["VAVG,CHAN1", ...]} (union across clients is queried at capture time), `raw_config` {mdep}, `raw_capture` {mode}, `raw_cancel`, `analysis_select|config|hold|window|view`, `screenshot`, `scpi`.

`analysis_window` / `analysis_view` are answered only for the *latest* request per socket (superseded ones are dropped, so dragging never builds a queue).

HTTP: `POST /analysis/import?name=&v_ch=&i_ch=` (raw body, `.csv`/`.npz`; 422 with `channels` when the chosen channels are missing), `GET /analysis/export.npz`, `GET /analysis/export.csv?t0=&t1=`.

### Frontend (`static/`)

Vanilla JS ES modules, no build step. `app.js` is the entry (WebSocket, auto-reconnect 2 s, re-publishes every JSON message on `bus.js`); `acquisition.js` (top-bar Single/Repeat/Free + Capture + status pill); `renderer.js` + `layout.js` + `cursors.js` (scope canvas: responsive via ResizeObserver/devicePixelRatio, grid margins as fractions of 800×480, cursors stored as grid fractions, redraw on demand); `controls.js`, `measurements.js` (scope params that arrived with the capture), `screenshot.js`; `theme.js`, `toast.js`, `popover.js`, `paramsync.js`; `analysis/` (`view.js`, `plot.js`, `results.js`, `params.js`, `format.js`, `export.js`). `?dev=1` enables a worst-case data toggle (`analysis/dev.js`), never shown otherwise.

### Theme tokens

Design tokens live in `static/style.css` (`:root,[data-theme="dark"]`, `[data-theme="light"]`, and `@media (prefers-color-scheme: light) { :root:not([data-theme]) }`). `theme.js` cycles system → light → dark (`T`), stores it in localStorage and fires `themechange`; canvas code reads tokens through `getComputedStyle`. Channel colours (CH1..CH4): dark `#f0c020 #00e0ff #ff40ff #3d8bff`; light `#8f6700 #00718a #b0009e #0058d6` (the plan's `#a87900`/`#007f99` gave only 3.9:1/4.1:1 on white); `--math` (P lane) `#a070ff`/`#6a3fd0`; cursors A orange / B cyan.

Motion rules: only `transform` and `opacity` are animated, never `transition: all`; buttons `transform 160ms ease-out` + `:active scale(0.97)`; popovers scale 0.96→1 + fade (170 ms enter, 120 ms exit, origin at the trigger); toasts slide from the right edge (≤ 250 ms); the only continuous animation is the pulsing "waiting for trigger" dot (opacity); no animation for cursor drag, zoom/pan, view/theme switch or keyboard actions; hover styles inside `@media (hover: hover) and (pointer: fine)`; `prefers-reduced-motion` removes transforms.

## SCPI reference

The `Docs/` folder contains the official Rigol MSO1000Z/DS1000Z Programming Guide PDF. Facts used by the code:

- **One query per write** (verified on hardware, fw 00.04.04.SP4): queries sent in the same write are answered as ONE compound reply joined with `;` (e.g. `:WAV:PRE?\n:WAV:DATA?\n` → `<preamble>;#9...<block>\n`), so a line read never ends and times out. Setters may share a write with one trailing query. The fake scope reproduces this and `tests/test_transport.py` guards it.

- `:WAV:MODE RAW` reads internal memory, **only in STOP**; max points per `:WAV:DATA?`: BYTE 250 000 (WORD 125 000, ASCii 15 625); chunks with `:WAV:STAR`/`:WAV:STOP` (1-based, inclusive). Voltage `(raw - yorig - yref) * yinc`; raw 0/255 = clipped. Read `:WAV:PRE?` per channel after selecting the mode.
- `:ACQ:MDEP` valid (analog): 1 ch `12k/120k/1.2M/12M/24M`, 2 ch `6k/60k/600k/6M/12M`, 3–4 ch `3k/30k/300k/3M/6M` (1.2 M is not valid with 2 channels). Depth = sample rate × 12 × time/div.
- `:TRIG:STAT?` → `TD|WAIT|RUN|AUTO|STOP`.
- `:MEAS:ITEM? <item>,<src>` — whole screen (no gating); `9.9e37` = not available; `MARea` on MATH (CH1×CH4) is energy in J.
- Screenshot: `:DISP:DATA? ON,OFF,PNG` (800×480 PNG), `:STOR:IMAG:INV|COL|TYPE`.
- Channels `:CHAN<n>:DISP|SCAL|OFFS|COUP|PROB|BWL|INV`; timebase `:TIM:SCAL|OFFS|MODE`; trigger `:TRIG:<mode>:SOUR|LEV`, `:TRIG:EDGE:SLOP`, `:TRIG:SWE`; decoder `:DEC<n>:...`; cursors `:CURS:...`; system `*IDN?`, `:RUN`, `:STOP`, `:SING`, `:TFOR`, `:AUT`.

## Conventions

- `server/app.py` module globals `SCOPE_HOST`/`SCOPE_PORT`/`USE_FAKE` are set by `run.py` before uvicorn starts.
- The CLI wraps the async `rigol/` package with `asyncio.run()` and does not import FastAPI.
- Canvas coordinates in the scope view are CSS pixels (the context is scaled by devicePixelRatio).
- Decoder control messages send `{dec: n, ...}`; the server accepts `n` or `dec`.
- Y cursor readout: true voltage = screen position × V/div − offset (matches how the renderer draws `(V + offset)/scale`).

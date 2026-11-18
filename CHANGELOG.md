# Changelog

All notable changes to this project are listed here. Versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- GitHub Actions workflow that runs the test suite on Linux and macOS.

## [0.1.0] - 2026-11-18

First usable release.

### Added
- Scope view: channel, timebase and trigger controls, trigger-driven Single/Repeat/Free captures, measurements, X/Y cursors, protocol decoder config, screenshots and exports.
- Power analysis view: V, I and P = v·i lanes with A/B window cursors; average and RMS V and I, average power (and Prms), peak power, energy (J, mWh), charge (mAh), pulse statistics; PNG, CSV and `.npz` export and import.
- RAW memory reads (6 k to 12 M points) in 250 000-point chunks.
- Built-in fake scope (`run.py --fake`) for development and tests without hardware.
- `--debug` file logging with a full SCPI trace, `--log-dir`, and a **Logs** button that opens the log folder.
- Command-line interface with discovery, SCPI shell and `raw-data`.
- Light, dark and system themes.
- pytest suite (73 tests), MIT license.

### Fixed
- NORM waveform reads timed out on the real scope: the DS1054Z answers two queries sent in one write with a single `;`-joined reply, so the transport now sends one query per write.

[Unreleased]: https://github.com/budenga/Rigol-DS1054Z-Web-Remote-GUI/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/budenga/Rigol-DS1054Z-Web-Remote-GUI/releases/tag/v0.1.0

"""Logging setup shared by run.py, the server and the CLI.

Console stays at INFO. With ``debug=True`` a per-run file handler records DEBUG for
``rigol.*`` and ``server.*`` (full SCPI trace) plus uvicorn at INFO. The setup is
idempotent: calling it again (run.py first, then the server lifespan) reuses the same
file and never duplicates handlers.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import subprocess
import sys
import time
from pathlib import Path

CONSOLE_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
FILE_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)s %(name)s: %(message)s"
FILE_DATEFMT = "%Y-%m-%d %H:%M:%S"
MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 5
KEEP_FILES = 20                      # newest per-run logs kept in the log directory
_UVICORN = ("uvicorn", "uvicorn.error", "uvicorn.access")
_TAG = "_rigol_handler"

_file_handler: logging.Handler | None = None
_log_path: Path | None = None


def default_log_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "Rigol_DS1054Z"
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "rigol_ds1054z" / "logs"


class _ConsoleFilter(logging.Filter):
    """INFO and above, plus rigol.transport DEBUG when DS1054Z_DEBUG is set (old behaviour)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.INFO:
            return True
        return bool(os.environ.get("DS1054Z_DEBUG")) and record.name == "rigol.transport"


def _prune(log_dir: Path, keep: int) -> None:
    try:
        files = sorted(log_dir.glob("ds1054z_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[keep:]:
            for f in [old, *old.parent.glob(old.name + ".*")]:
                f.unlink(missing_ok=True)
    except OSError:
        pass


def _remove_tagged(logger: logging.Logger) -> None:
    for h in list(logger.handlers):
        if getattr(h, _TAG, False):
            logger.removeHandler(h)
            if h is not _file_handler:
                h.close()


def setup_logging(debug: bool = False, log_dir: str | os.PathLike | None = None,
                  app: str = "ds1054z") -> Path | None:
    """Configure logging. Returns the log file path when ``debug`` is on, else None."""
    global _file_handler, _log_path

    root = logging.getLogger()
    for lg in [root, *(logging.getLogger(n) for n in _UVICORN)]:
        _remove_tagged(lg)

    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG)
    console.addFilter(_ConsoleFilter())
    console.setFormatter(logging.Formatter(CONSOLE_FORMAT))
    setattr(console, _TAG, True)
    root.setLevel(logging.INFO)
    root.addHandler(console)

    for name in ("rigol", "server"):
        logging.getLogger(name).setLevel(logging.INFO)
    if os.environ.get("DS1054Z_DEBUG"):
        logging.getLogger("rigol.transport").setLevel(logging.DEBUG)

    if not debug:
        close_file_handler()
        return None

    if _file_handler is None:
        d = Path(log_dir).expanduser() if log_dir else default_log_dir()
        d.mkdir(parents=True, exist_ok=True)
        _prune(d, KEEP_FILES - 1)
        _log_path = d / f"{app}_{time.strftime('%Y%m%d_%H%M%S')}.log"
        _file_handler = logging.handlers.RotatingFileHandler(
            _log_path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8")
        _file_handler.setLevel(logging.DEBUG)
        _file_handler.setFormatter(logging.Formatter(FILE_FORMAT, FILE_DATEFMT))
        setattr(_file_handler, _TAG, True)

    root.addHandler(_file_handler)
    for name in ("rigol", "server"):
        logging.getLogger(name).setLevel(logging.DEBUG)
    for name in _UVICORN:                 # uvicorn loggers do not propagate to root
        lg = logging.getLogger(name)
        lg.setLevel(logging.INFO)
        lg.addHandler(_file_handler)
    return _log_path


def close_file_handler() -> None:
    global _file_handler, _log_path
    if _file_handler is not None:
        for lg in [logging.getLogger(), *(logging.getLogger(n) for n in _UVICORN)]:
            lg.removeHandler(_file_handler)
        _file_handler.close()
        _file_handler = None
        _log_path = None


def shutdown_logging() -> None:
    root = logging.getLogger()
    for lg in [root, *(logging.getLogger(n) for n in _UVICORN)]:
        _remove_tagged(lg)
    close_file_handler()


def log_path() -> Path | None:
    return _log_path


def open_folder(path: os.PathLike | str) -> bool:
    """Show ``path`` in the system file manager (Finder / xdg-open). False if unsupported."""
    cmd = ["open", str(path)] if sys.platform == "darwin" else ["xdg-open", str(path)]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except OSError:
        return False

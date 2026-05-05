"""
sufi2.logger
============
Centralized logging for SUFI-2.

- Writes structured logs to  sufi2_results/logs/sufi2.log
- Streams INFO+ to stdout (visible in Render logs)
- Captures unhandled exceptions automatically
- Rotating file handler: 5 MB max, 3 backups

Usage (in api.py / core.py):
    from sufi2.logger import get_logger
    log = get_logger(__name__)
    log.info("Starting calibration")
    log.error("Something failed", exc_info=True)
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import traceback
from pathlib import Path


# ─────────────────────────────────────────────────────────────────────────────
# Log directory — writable on Render (ephemeral) and locally
# ─────────────────────────────────────────────────────────────────────────────

def _log_dir() -> Path:
    """
    Resolve a writable log directory.
    Priority:
      1. $LOG_DIR env var (user-defined)
      2. ./sufi2_results/logs/  (alongside results)
      3. /tmp/sufi2_logs/       (fallback — always writable)
    """
    env = os.environ.get("LOG_DIR")
    if env:
        p = Path(env)
        p.mkdir(parents=True, exist_ok=True)
        return p
    for candidate in [Path("sufi2_results/logs"), Path("/tmp/sufi2_logs")]:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except OSError:
            continue
    return Path("/tmp")


LOG_DIR  = _log_dir()
LOG_FILE = LOG_DIR / "sufi2.log"

# ─────────────────────────────────────────────────────────────────────────────
# Formatters
# ─────────────────────────────────────────────────────────────────────────────

FILE_FMT = logging.Formatter(
    fmt="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

CONSOLE_FMT = logging.Formatter(
    fmt="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)


# ─────────────────────────────────────────────────────────────────────────────
# Root logger setup (called once)
# ─────────────────────────────────────────────────────────────────────────────

_configured = False

def _configure_root():
    global _configured
    if _configured:
        return
    _configured = True

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # ── Rotating file handler ─────────────────────────────────────────────
    try:
        fh = logging.handlers.RotatingFileHandler(
            LOG_FILE,
            maxBytes=5 * 1024 * 1024,   # 5 MB
            backupCount=3,
            encoding="utf-8",
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(FILE_FMT)
        root.addHandler(fh)
    except OSError as e:
        print(f"[sufi2.logger] WARNING: Could not open log file {LOG_FILE}: {e}", file=sys.stderr)

    # ── Console (stdout) handler — INFO+ so Render shows it ──────────────
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(CONSOLE_FMT)
    root.addHandler(ch)

    # ── Capture unhandled exceptions ──────────────────────────────────────
    def _excepthook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        root.critical(
            "Unhandled exception",
            exc_info=(exc_type, exc_value, exc_tb),
        )
    sys.excepthook = _excepthook

    root.info("Logging initialised — file: %s", LOG_FILE)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def get_logger(name: str) -> logging.Logger:
    """Return a named logger, ensuring root is configured."""
    _configure_root()
    return logging.getLogger(name)


def log_exception(logger: logging.Logger, msg: str, exc: Exception):
    """Log an exception with full traceback to file, short message to console."""
    logger.error("%s: %s", msg, exc)
    logger.debug("Traceback:\n%s", traceback.format_exc())


# ─────────────────────────────────────────────────────────────────────────────
# Log file reader — used by GET /logs/file endpoint
# ─────────────────────────────────────────────────────────────────────────────

def read_log_tail(n_lines: int = 200) -> str:
    """Return the last N lines of the log file as a string."""
    if not LOG_FILE.exists():
        return "(no log file yet)"
    try:
        lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n_lines:])
    except OSError as e:
        return f"(could not read log file: {e})"


def get_log_path() -> str:
    return str(LOG_FILE)

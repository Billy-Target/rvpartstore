"""Shared runner for main.py jobs. Ported from francis-shopify's job_runner.py
(R17), with relative imports and WITHOUT the sys.path manipulation (this
project has no sibling packages to reach — everything lives inside the
rvpartstore package)."""

import logging
import time

from .app_logging import setup_logging

# Known-noise log lines to drop regardless of which logger/stream they arrive on.
_NOISE_SUBSTRINGS = (
    "file_cache is only supported with oauth2client",  # googleapiclient
)


class _DropNoise(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        return not any(s in msg for s in _NOISE_SUBSTRINGS)


def attempt(name, func, tries=3, delay=60, failures=None):
    """Run ``func`` up to ``tries`` times, ``delay`` seconds apart. Returns True
    on success. Never raises — the caller decides what a failure means."""
    log = logging.getLogger()
    for i in range(1, tries + 1):
        try:
            func()
            if i > 1:
                log.info("%s OK on attempt %d/%d", name, i, tries)
            return True
        except Exception:
            log.exception("%s attempt %d/%d failed", name, i, tries)
            if i < tries:
                time.sleep(delay)
    log.error("%s FAILED after %d attempts", name, tries)
    if failures is not None:
        failures.append(name)
    return False


def run(name, func, label=None):
    """Run one job. ``name`` picks the log file (D:\\log_folder\\<name>.log);
    ``label`` (defaults to ``name``) is what START/FINISHED/FAILED report.
    Re-raises on failure so Task Scheduler records the task as failed."""
    label = label or name
    log = setup_logging(name)
    for handler in log.handlers:
        handler.addFilter(_DropNoise())
    log.info("=" * 60)
    log.info("START %s", label)
    t0 = time.time()
    try:
        func()
        log.info("FINISHED %s in %.1f s", label, time.time() - t0)
    except Exception:
        log.exception("FAILED %s after %.1f s", label, time.time() - t0)
        raise

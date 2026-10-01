"""Reusable logging setup for scheduled jobs. Ported from francis-shopify's
app_logging.py (R17), unchanged behaviour.

Not on the tracking import path's "must stay side-effect free" list itself,
but importing this module still does nothing until setup_logging() is called
— tracking.py never imports it.
"""

import logging
import logging.handlers
import os
import sys

# All projects log into this shared folder; the file name is the project name.
# Rotated backups are moved into the "backup" subfolder.
LOG_DIR = r"D:\log_folder"


class _StreamToLogger:
    """File-like object that forwards writes (e.g. print) to a logger."""

    def __init__(self, logger, level):
        self._logger = logger
        self._level = level
        self._buffer = ""

    def write(self, message):
        self._buffer += message
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            if line.strip():
                self._logger.log(self._level, line)

    def flush(self):
        if self._buffer.strip():
            self._logger.log(self._level, self._buffer.strip())
        self._buffer = ""


class _BackupDirTimedRotatingFileHandler(logging.handlers.TimedRotatingFileHandler):
    """Like TimedRotatingFileHandler, but keeps rotated backups in a ``backup``
    subfolder instead of leaving them next to the active log."""

    def __init__(self, filename, backup_dir, **kwargs):
        self._backup_dir = backup_dir
        os.makedirs(backup_dir, exist_ok=True)
        super().__init__(filename, **kwargs)

    def rotation_filename(self, default_name):
        return os.path.join(self._backup_dir, os.path.basename(default_name))

    def getFilesToDelete(self):
        prefix = os.path.basename(self.baseFilename) + "."
        result = []
        for name in os.listdir(self._backup_dir):
            if name.startswith(prefix):
                suffix = name[len(prefix):]
                if self.extMatch.match(suffix):
                    result.append(os.path.join(self._backup_dir, name))
        if self.backupCount <= 0 or len(result) <= self.backupCount:
            return []
        result.sort()
        return result[:len(result) - self.backupCount]


def setup_logging(project_name, log_dir=LOG_DIR, level=logging.INFO, capture_print=True):
    """Configure the root logger. Returns a logger you can call .info()/.exception() on."""
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, project_name + ".log")

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    file_handler = _BackupDirTimedRotatingFileHandler(
        log_path,
        backup_dir=os.path.join(log_dir, "backup"),
        when="W0",
        backupCount=52,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    original_stdout = sys.__stdout__
    if original_stdout is not None:
        console_handler = logging.StreamHandler(original_stdout)
        console_handler.setFormatter(fmt)
        root.addHandler(console_handler)

    if capture_print:
        sys.stdout = _StreamToLogger(logging.getLogger("stdout"), logging.INFO)
        sys.stderr = _StreamToLogger(logging.getLogger("stderr"), logging.ERROR)

    return root

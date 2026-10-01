"""MySQL connection helper. Stdlib + mysql.connector only (R3 — tracking import path)."""

import time
import logging

import mysql.connector

log = logging.getLogger(__name__)


class DbError(Exception):
    pass


def connect(settings, max_attempts=3, delay_s=5):
    """Bounded-retry MySQL connect. Raises DbError after max_attempts (never
    loops forever — the old code's `while True: try/except: pass` is explicitly
    disallowed by the design spec)."""
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            return mysql.connector.connect(
                host=settings.mysql_host,
                user=settings.mysql_user,
                password=settings.mysql_password,
                database=settings.mysql_database,
            )
        except mysql.connector.Error as exc:
            last_exc = exc
            log.warning("MySQL connect attempt %d/%d failed: %s", attempt, max_attempts, exc)
            if attempt < max_attempts:
                time.sleep(delay_s)
    raise DbError("could not connect to MySQL after %d attempts: %s" % (max_attempts, last_exc))


def run_query(conn, query, params=None):
    """Execute a SELECT, return (rows, column_names)."""
    cursor = conn.cursor()
    try:
        cursor.execute(query, params or ())
        rows = cursor.fetchall()
        columns = [c[0] for c in cursor.description] if cursor.description else []
        return rows, columns
    finally:
        cursor.close()


class Db:
    """Thin settings-bound connection factory, so callers (sources.py) don't
    have to thread `settings` through every loader call individually."""

    def __init__(self, settings, max_attempts=3, delay_s=5):
        self._settings = settings
        self._max_attempts = max_attempts
        self._delay_s = delay_s

    def connect(self):
        return connect(self._settings, self._max_attempts, self._delay_s)


def run_write(conn, query, params=None, many=False):
    """Execute an INSERT/UPDATE/DELETE (optionally executemany) and commit."""
    cursor = conn.cursor()
    try:
        if many:
            cursor.executemany(query, params or [])
        else:
            cursor.execute(query, params or ())
        conn.commit()
        return cursor.rowcount
    finally:
        cursor.close()

"""Black-box tests for main.py's quiet-window helper (design spec sec. 11,
R14; reviewer item 9): the overnight window must correctly handle BOTH a
window that crosses midnight (e.g. 23:30-06:30: quiet while now >= start or
now < end) and a same-day window (e.g. 01:00-05:00: quiet while
start <= now < end), with the end boundary exclusive in both cases.

`_in_quiet_window(settings)` is main.py's own helper (found by inspecting
main.py's public/private names per the task); it's called with only a
`settings` object exposing `quiet_start`/`quiet_end` as "HH:MM" strings and
reads the current time via `datetime.datetime.now()` -- frozen here via
monkeypatch so the tests are deterministic.
"""
import datetime
import types

import pytest

import main as main_mod


def _settings(quiet_start, quiet_end):
    return types.SimpleNamespace(quiet_start=quiet_start, quiet_end=quiet_end)


def _freeze(monkeypatch, hour, minute, second=0):
    frozen = datetime.datetime(2024, 1, 1, hour, minute, second)

    class _Frozen(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen

    monkeypatch.setattr(main_mod.datetime, "datetime", _Frozen)


# ---------------------------------------------------------------------------
# crossing-midnight window (23:30-06:30)
# ---------------------------------------------------------------------------
def test_crossing_midnight_window_quiet_late_at_night(monkeypatch):
    _freeze(monkeypatch, 23, 45)
    assert main_mod._in_quiet_window(_settings("23:30", "06:30")) is True


def test_crossing_midnight_window_quiet_early_morning(monkeypatch):
    _freeze(monkeypatch, 2, 0)
    assert main_mod._in_quiet_window(_settings("23:30", "06:30")) is True


def test_crossing_midnight_window_not_quiet_at_noon(monkeypatch):
    _freeze(monkeypatch, 12, 0)
    assert main_mod._in_quiet_window(_settings("23:30", "06:30")) is False


def test_crossing_midnight_window_boundary_at_start_is_quiet(monkeypatch):
    _freeze(monkeypatch, 23, 30)
    assert main_mod._in_quiet_window(_settings("23:30", "06:30")) is True


def test_crossing_midnight_window_just_before_start_is_not_quiet(monkeypatch):
    _freeze(monkeypatch, 23, 29, 59)
    assert main_mod._in_quiet_window(_settings("23:30", "06:30")) is False


def test_crossing_midnight_window_boundary_at_end_is_not_quiet_exclusive(monkeypatch):
    _freeze(monkeypatch, 6, 30)
    assert main_mod._in_quiet_window(_settings("23:30", "06:30")) is False


def test_crossing_midnight_window_just_before_end_is_quiet(monkeypatch):
    _freeze(monkeypatch, 6, 29, 59)
    assert main_mod._in_quiet_window(_settings("23:30", "06:30")) is True


# ---------------------------------------------------------------------------
# same-day window (01:00-05:00)
# ---------------------------------------------------------------------------
def test_same_day_window_quiet_inside(monkeypatch):
    _freeze(monkeypatch, 2, 0)
    assert main_mod._in_quiet_window(_settings("01:00", "05:00")) is True


def test_same_day_window_not_quiet_before_start(monkeypatch):
    _freeze(monkeypatch, 0, 30)
    assert main_mod._in_quiet_window(_settings("01:00", "05:00")) is False


def test_same_day_window_not_quiet_after_end(monkeypatch):
    _freeze(monkeypatch, 5, 30)
    assert main_mod._in_quiet_window(_settings("01:00", "05:00")) is False


def test_same_day_window_boundary_at_start_is_quiet(monkeypatch):
    _freeze(monkeypatch, 1, 0)
    assert main_mod._in_quiet_window(_settings("01:00", "05:00")) is True


def test_same_day_window_boundary_at_end_is_not_quiet_exclusive(monkeypatch):
    _freeze(monkeypatch, 5, 0)
    assert main_mod._in_quiet_window(_settings("01:00", "05:00")) is False


# ---------------------------------------------------------------------------
# B4: upload() runs the Brakex step as its FINAL step even when the RV part
# aborts early, and a Brakex-step exception never fails the whole upload job.
# `_load_settings`/`_upload_rv`/`_run_brakex` are main.py's own seams (found
# by inspecting main.py's names); monkeypatching them keeps this test free of
# any real network/DB/.env dependency while still exercising upload()'s own
# orchestration contract (not the internals of either step).
# ---------------------------------------------------------------------------
def _upload_settings(brakex_enabled=True):
    return types.SimpleNamespace(
        quiet_start="23:30", quiet_end="06:30", brakex_enabled=brakex_enabled, dry_run=False,
    )


def test_brakex_step_runs_even_when_rv_part_aborts_early(monkeypatch):
    calls = []
    monkeypatch.setattr(main_mod, "_load_settings", lambda force_dry_run=False: _upload_settings())

    def fake_upload_rv(settings, force_breaker):
        # Simulates the real early-abort path (e.g. empty snapshot): logs and
        # returns without raising, without touching pricing/writes at all.
        calls.append("rv")
        return None

    def fake_run_brakex(settings):
        calls.append("brakex")
        return {"inserted": 0, "skipped": 0, "errors": 0}

    monkeypatch.setattr(main_mod, "_upload_rv", fake_upload_rv)
    monkeypatch.setattr(main_mod, "_run_brakex", fake_run_brakex)

    main_mod.upload(ignore_quiet=True)

    assert calls == ["rv", "brakex"]


def test_brakex_step_exception_does_not_fail_the_upload_job(monkeypatch, caplog):
    monkeypatch.setattr(main_mod, "_load_settings", lambda force_dry_run=False: _upload_settings())
    monkeypatch.setattr(main_mod, "_upload_rv", lambda settings, force_breaker: None)

    def boom(settings):
        raise RuntimeError("brakex: connection refused")

    monkeypatch.setattr(main_mod, "_run_brakex", boom)

    with caplog.at_level("ERROR"):
        main_mod.upload(ignore_quiet=True)  # must not raise

    assert any("brakex" in r.message.lower() for r in caplog.records)


def test_brakex_step_skipped_entirely_when_disabled(monkeypatch):
    monkeypatch.setattr(main_mod, "_load_settings", lambda force_dry_run=False: _upload_settings(brakex_enabled=False))
    monkeypatch.setattr(main_mod, "_upload_rv", lambda settings, force_breaker: None)

    def must_not_be_called(settings):
        raise AssertionError("_run_brakex must not be called when brakex_enabled=False")

    monkeypatch.setattr(main_mod, "_run_brakex", must_not_be_called)

    main_mod.upload(ignore_quiet=True)  # must not raise


# ---------------------------------------------------------------------------
# Review item 6: `python main.py brakex_orders` validates only MySQL +
# Brakex keys -- deliberately does NOT go through _load_settings()/
# validate_upload() (which would require the full RV Amazon/Google
# credential set this job never touches).
# ---------------------------------------------------------------------------
def _mysql_and_brakex_only_settings(**overrides):
    # Intentionally has NO amazon_*/google_*/store_type/txn_prefix attributes
    # at all -- if brakex_orders() ever accidentally called validate_upload()
    # (or _load_settings()), that would raise (either MissingSettingsError
    # for the "missing" attrs, defaulted to "" by getattr, or AttributeError)
    # and fail this test.
    base = dict(
        mysql_host="h", mysql_user="u", mysql_password="p", mysql_database="inventory",
        brakex_shop="brakex.myshopify.com", brakex_access_token="shpat_abc",
        brakex_order_table="shopify_brakex_order", brakex_txn_prefix="BRX_SP_37",
        brakex_store_type="shopifycom_brx", brakex_usd_to_cad=1.35,
        brakex_enabled=True, dry_run=False,
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


def test_standalone_brakex_job_does_not_require_rv_upload_keys(monkeypatch):
    settings = _mysql_and_brakex_only_settings()
    monkeypatch.setattr(main_mod.config, "load_settings", lambda: settings)

    calls = []
    monkeypatch.setattr(main_mod, "_run_brakex", lambda s: calls.append(s))

    main_mod.brakex_orders()  # must not raise despite no RV/Amazon/Google keys

    assert calls == [settings]


def test_standalone_brakex_job_still_requires_mysql_keys(monkeypatch):
    # mysql_password missing entirely -> getattr(...,"") -> "" -> _missing()
    # flags it -> validate_mysql() raises, before _run_brakex is ever reached.
    settings = types.SimpleNamespace(
        mysql_host="h", mysql_user="u", mysql_database="inventory",
        brakex_enabled=True, dry_run=False,
    )
    monkeypatch.setattr(main_mod.config, "load_settings", lambda: settings)

    def must_not_be_called(s):
        raise AssertionError("_run_brakex must not be called when MySQL keys are missing")

    monkeypatch.setattr(main_mod, "_run_brakex", must_not_be_called)

    with pytest.raises(main_mod.config.MissingSettingsError):
        main_mod.brakex_orders()


def test_standalone_brakex_job_noop_when_disabled(monkeypatch):
    settings = _mysql_and_brakex_only_settings(brakex_enabled=False)
    monkeypatch.setattr(main_mod.config, "load_settings", lambda: settings)

    def must_not_be_called(s):
        raise AssertionError("_run_brakex must not be called when BRAKEX_ENABLED=false")

    monkeypatch.setattr(main_mod, "_run_brakex", must_not_be_called)

    main_mod.brakex_orders()  # must not raise

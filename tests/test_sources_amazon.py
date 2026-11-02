"""load_amazon_report: download into a temp file, swap in on success; on a failed
download fall back to the previous report if it is <= 3h old, else GuardError."""
import os
import time
import types

import pytest

from rvpartstore import sources

REPORT = "seller-sku\tprice\tquantity\tstatus\nAU-IB-012345678905\t10.00\t1\tActive\n"


def _settings(tmp_path):
    return types.SimpleNamespace(amazon_report_path=str(tmp_path / "amazon_report.txt"))


def _write_old_report(path, age_s, text=REPORT):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    stamp = time.time() - age_s
    os.utime(path, (stamp, stamp))


def _ok_download(text):
    def fake(settings, out_path):
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(text)
        return out_path
    return fake


def _failing_download(settings, out_path):
    raise RuntimeError("amazon report 123 still IN_QUEUE after 600s")


def test_successful_download_replaces_previous_report(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    _write_old_report(s.amazon_report_path, 3600, REPORT.replace("10.00", "99.00"))
    monkeypatch.setattr(sources, "create_amazon_report", _ok_download(REPORT))

    df = sources.load_amazon_report(s)

    assert float(df.loc[0, "price"]) == 10.00
    assert not os.path.exists(s.amazon_report_path + ".tmp")


def test_failed_download_falls_back_to_recent_report(tmp_path, monkeypatch, caplog):
    s = _settings(tmp_path)
    _write_old_report(s.amazon_report_path, 2 * 3600)  # 2h old: still usable
    monkeypatch.setattr(sources, "create_amazon_report", _failing_download)

    df = sources.load_amazon_report(s)

    assert df.loc[0, "seller-sku"] == "AU-IB-012345678905"
    assert "using previous report" in caplog.text


def test_failed_download_with_stale_report_raises_guard(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    _write_old_report(s.amazon_report_path, 4 * 3600)  # 4h old: too old
    monkeypatch.setattr(sources, "create_amazon_report", _failing_download)

    with pytest.raises(sources.GuardError):
        sources.load_amazon_report(s)


def test_failed_download_without_previous_report_raises_guard(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    monkeypatch.setattr(sources, "create_amazon_report", _failing_download)

    with pytest.raises(sources.GuardError):
        sources.load_amazon_report(s)


def test_partial_download_does_not_clobber_previous_report(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    _write_old_report(s.amazon_report_path, 1800)

    def partial_then_fail(settings, out_path):
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("seller-sku\tpri")
        raise RuntimeError("connection reset mid-download")

    monkeypatch.setattr(sources, "create_amazon_report", partial_then_fail)

    df = sources.load_amazon_report(s)

    assert float(df.loc[0, "price"]) == 10.00
    assert not os.path.exists(s.amazon_report_path + ".tmp")


def test_empty_download_falls_back(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    _write_old_report(s.amazon_report_path, 600)
    monkeypatch.setattr(sources, "create_amazon_report", _ok_download(""))

    df = sources.load_amazon_report(s)

    assert float(df.loc[0, "price"]) == 10.00


# --- save_amazon_ib_prices (RMA rev 3, R2/R6) ---------------------------------

import datetime

import pandas


class _FakeConn:
    def __init__(self):
        self.closed = False


def _ib_df():
    return pandas.DataFrame({
        "seller-sku": ["AU-IB-0123", "AU-IB-0123-LN", "AU-IB-0456", "AU-IB-0789", "XX-1", "AU-IB-0999", "AU-IB-0555"],
        "price": ["10.5", "9", "20", "0", "5", "abc", "30"],
        "status": ["Active", "Active", "Inactive", "Active", "Active", "Active", "Active"],
    })


def _capture(monkeypatch):
    calls = []
    conn = _FakeConn()
    conn.close = lambda: setattr(conn, "closed", True)
    db = types.SimpleNamespace(connect=lambda: conn)
    monkeypatch.setattr(sources.db_mod, "replace_all_rows",
                        lambda c, d, i, rows: calls.append((c, d, i, rows)))
    return db, conn, calls


def test_save_filters_active_exact_new_sku_positive_price(monkeypatch):
    db, conn, calls = _capture(monkeypatch)
    ts = datetime.datetime(2026, 10, 16, 12, 0)

    n = sources.save_amazon_ib_prices(db, _ib_df(), ts)

    assert n == 2
    # two snapshots now: used first (empty here, still replaced), then new
    (c1, del_used, ins_used, rows_used), (c2, delete_q, insert_q, rows) = calls
    assert c1 is conn and c2 is conn and conn.closed
    assert del_used.startswith("DELETE") and "amazon_ib_used_price" in ins_used
    assert rows_used == []
    assert delete_q.startswith("DELETE") and "inventory.amazon_ib_price" in insert_q
    assert "amazon_ib_used_price" not in insert_q
    assert rows == [("0123", "AU-IB-0123", 10.5, ts), ("0555", "AU-IB-0555", 30.0, ts)]


def test_save_empty_filtered_set_does_not_delete(monkeypatch, caplog):
    db, conn, calls = _capture(monkeypatch)
    df = pandas.DataFrame({"seller-sku": ["AU-IB-1-LN"], "price": ["5"], "status": ["Active"]})

    assert sources.save_amazon_ib_prices(db, df, datetime.datetime.now()) == 0
    assert calls == [] and "snapshot not replaced" in caplog.text


def test_save_dry_run_does_not_write(monkeypatch):
    db, conn, calls = _capture(monkeypatch)

    assert sources.save_amazon_ib_prices(db, _ib_df(), datetime.datetime.now(), dry_run=True) == 0
    assert calls == [] and not conn.closed


def test_amazon_report_mtime_is_file_mtime(tmp_path):
    s = _settings(tmp_path)
    _write_old_report(s.amazon_report_path, 600)

    got = sources.amazon_report_mtime(s)

    assert abs((datetime.datetime.now() - got).total_seconds() - 600) < 5


def test_replace_all_rows_is_one_transaction_and_rolls_back():
    from rvpartstore import db as db_mod

    events = []

    class Cur:
        def execute(self, q): events.append("exec")
        def executemany(self, q, rows): raise RuntimeError("boom")
        def close(self): events.append("close")

    class Conn:
        def cursor(self): return Cur()
        def commit(self): events.append("commit")
        def rollback(self): events.append("rollback")

    with pytest.raises(RuntimeError):
        db_mod.replace_all_rows(Conn(), "DELETE", "INSERT", [(1,)])
    assert events == ["exec", "rollback", "close"]


# --- used snapshot (amazon_ib_used_price) -------------------------------------

TS = datetime.datetime(2026, 10, 29, 9, 0)


def _df(skus, prices=None, statuses=None):
    n = len(skus)
    return pandas.DataFrame({
        "seller-sku": skus,
        "price": prices or ["10"] * n,
        "status": statuses or ["Active"] * n,
    })


def _is_used(call):
    return "amazon_ib_used_price" in call[2]


def _split(calls):
    return [c for c in calls if _is_used(c)], [c for c in calls if not _is_used(c)]


def test_used_rows_have_expected_values(monkeypatch):
    db, conn, calls = _capture(monkeypatch)
    df = _df(["AU-IB-5", "AU-IB-5-OB", "AU-IB-5-UE", "AU-IB-5-UG"], ["10", "8.5", "7", "6.25"])

    sources.save_amazon_ib_prices(db, df, TS)

    used, new = _split(calls)
    assert sorted(used[0][3]) == sorted([
        ("5", "OB", "AU-IB-5-OB", 8.5, TS),
        ("5", "UE", "AU-IB-5-UE", 7.0, TS),
        ("5", "UG", "AU-IB-5-UG", 6.25, TS),
    ])
    assert new[0][3] == [("5", "AU-IB-5", 10.0, TS)]


@pytest.mark.parametrize("bad_sku", ["AU-IB-5-FBA", "AU-IB-5-LN", "au-ib-5-ob", "AU-IB-5-OBX",
                                     "AU-IB-5-OB\nX", "AU-IB-5-OB X", "AU-IB-5-OB-1", "AU-IB--OB", "AU-IB-5A-OB", "AU-IB-5-ob"])
def test_used_rejects_non_matching_skus(monkeypatch, bad_sku):
    db, conn, calls = _capture(monkeypatch)

    sources.save_amazon_ib_prices(db, _df(["AU-IB-1", bad_sku]), TS)

    used, _ = _split(calls)
    assert used[0][3] == []


def test_used_inactive_and_nonpositive_price_excluded(monkeypatch):
    db, conn, calls = _capture(monkeypatch)
    df = _df(["AU-IB-1", "AU-IB-1-OB", "AU-IB-1-UE", "AU-IB-1-UG", "AU-IB-1-OB"], ["10", "5", "0", "-3", "abc"],
             ["Active", "Inactive", "Active", "Active", "Active"])

    sources.save_amazon_ib_prices(db, df, TS)

    used, _ = _split(calls)
    assert used[0][3] == []


def test_used_does_not_leak_into_new_snapshot(monkeypatch):
    db, conn, calls = _capture(monkeypatch)

    n = sources.save_amazon_ib_prices(db, _df(["AU-IB-1", "AU-IB-1-OB", "AU-IB-1-UE"]), TS)

    _, new = _split(calls)
    assert n == 1 and new[0][3] == [("1", "AU-IB-1", 10.0, TS)]


def test_used_first_listing_per_upc_grade_wins(monkeypatch):
    db, conn, calls = _capture(monkeypatch)
    df = _df(["AU-IB-1", "AU-IB-1-OB", "AU-IB-1-OB", "AU-IB-1-UE", "AU-IB-1-OB"], ["10", "7", "3", "4", "2"])

    sources.save_amazon_ib_prices(db, df, TS)

    used, _ = _split(calls)
    assert sorted(used[0][3]) == sorted([("1", "OB", "AU-IB-1-OB", 7.0, TS), ("1", "UE", "AU-IB-1-UE", 4.0, TS)])


def test_used_invalid_earlier_duplicate_does_not_shadow_valid_later(monkeypatch):
    db, conn, calls = _capture(monkeypatch)
    df = _df(["AU-IB-1", "AU-IB-1-OB", "AU-IB-1-OB"], ["10", "9", "4"], ["Active", "Inactive", "Active"])

    sources.save_amazon_ib_prices(db, df, TS)

    used, _ = _split(calls)
    assert used[0][3] == [("1", "OB", "AU-IB-1-OB", 4.0, TS)]


def test_used_leading_zeros_kept(monkeypatch):
    db, conn, calls = _capture(monkeypatch)

    sources.save_amazon_ib_prices(db, _df(["AU-IB-000123", "AU-IB-000123-UG"]), TS)

    used, new = _split(calls)
    assert used[0][3][0][0] == "000123"
    assert new[0][3][0][0] == "000123"


def test_used_written_before_new(monkeypatch):
    db, conn, calls = _capture(monkeypatch)

    sources.save_amazon_ib_prices(db, _df(["AU-IB-1", "AU-IB-1-OB"]), TS)

    assert len(calls) == 2
    assert _is_used(calls[0]) and not _is_used(calls[1])
    assert calls[0][1].startswith("DELETE") and calls[1][1].startswith("DELETE")


def test_empty_used_set_still_replaces_with_delete(monkeypatch):
    db, conn, calls = _capture(monkeypatch)

    sources.save_amazon_ib_prices(db, _df(["AU-IB-1"]), TS)

    used, new = _split(calls)
    assert len(used) == 1 and used[0][3] == [] and used[0][1].startswith("DELETE")
    assert len(new) == 1


def test_no_new_rows_touches_neither_table(monkeypatch):
    db, conn, calls = _capture(monkeypatch)
    df = _df(["AU-IB-1-OB", "AU-IB-1-UE", "AU-IB-2-LN"])  # used rows only, no NEW sku

    assert sources.save_amazon_ib_prices(db, df, TS) == 0
    assert calls == []


def test_used_write_failure_is_logged_and_new_still_written(monkeypatch, caplog):
    conn = _FakeConn()
    conn.close = lambda: setattr(conn, "closed", True)
    db = types.SimpleNamespace(connect=lambda: conn)
    calls = []

    def fake(c, d, i, rows):
        if "amazon_ib_used_price" in i:
            raise RuntimeError("table amazon_ib_used_price missing")
        calls.append((c, d, i, rows))

    monkeypatch.setattr(sources.db_mod, "replace_all_rows", fake)

    n = sources.save_amazon_ib_prices(db, _df(["AU-IB-1", "AU-IB-1-OB"]), TS)

    assert n == 1
    assert len(calls) == 1 and "amazon_ib_used_price" not in calls[0][2]
    assert calls[0][3] == [("1", "AU-IB-1", 10.0, TS)]
    assert "used" in caplog.text.lower()


def test_dry_run_writes_nothing_with_used_rows(monkeypatch):
    db, conn, calls = _capture(monkeypatch)

    sources.save_amazon_ib_prices(db, _df(["AU-IB-1", "AU-IB-1-OB"]), TS, dry_run=True)

    assert calls == []


def test_return_value_is_number_of_new_rows(monkeypatch):
    db, conn, calls = _capture(monkeypatch)
    df = _df(["AU-IB-1", "AU-IB-2", "AU-IB-2-OB", "AU-IB-2-UE", "AU-IB-3-UG"])

    assert sources.save_amazon_ib_prices(db, df, TS) == 2


def test_used_sql_file_defines_pk_upc_grade():
    import pathlib
    import re
    path = pathlib.Path(__file__).resolve().parent.parent / "sql" / "amazon_ib_used_price.sql"
    text = path.read_text(encoding="utf-8")
    assert "inventory.amazon_ib_used_price" in text
    assert re.search(r"PRIMARY\s+KEY\s*\(\s*`?upc`?\s*,\s*`?grade`?\s*\)", text, re.I)

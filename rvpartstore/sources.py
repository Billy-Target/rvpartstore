"""I/O loaders for pricing inputs (design spec sec. 5). Thin: each function
fetches data and does the minimal shape work described in the spec; all the
actual pricing logic lives in pricing.py.

R7: guard checks live here too (abort the pricing/push step, no writes, when
an input isn't ready) — each loader raises GuardError so main.py can catch one
exception type and log+abort.
"""

import logging
import datetime
import os
import re
import time

import pandas

from . import db as db_mod
from .amazon_sp_api import create_amazon_report
from .google_sheets import get_info_from_google_docs

log = logging.getLogger(__name__)


class GuardError(Exception):
    """Raised when an upstream input isn't ready; caller must abort the
    pricing/push step without writing anything (R7)."""


_MERGED_FEED_TABLES = (
    "merged_feed_quantity",
    "merged_feed_cost",
    "merged_feed_special_handling_charge",
    "merged_feed_retail_min_price",
    "merged_feed_map_price",
    "merged_feed_brand",
    "merged_feed_part_number",
    "merged_feed_dimension",
    "merged_feed_ltl",
)


def _query_df(conn, query):
    rows, columns = db_mod.run_query(conn, query)
    return pandas.DataFrame(rows, columns=columns)


def load_merged_feed(db):
    """Port of get_feed_from_merged_feed(sql_server=False): 9 MySQL tables for
    curdate(), drop CreateDate, successive inner merges. R7: raise GuardError
    if ANY of the 9 tables has 0 rows for today (P1: inputs via parameter, no
    inline DB read in pricing.py; this replaces price_modifier's SQL-server
    branch entirely — CAD store only)."""
    conn = db.connect()
    try:
        frames = []
        for table in _MERGED_FEED_TABLES:
            query = "SELECT * FROM inventory.{} where CreateDate=curdate();".format(table)
            frame = _query_df(conn, query)
            if frame.empty:
                raise GuardError("inventory.{} has 0 rows for today; feed not ready".format(table))
            frames.append(frame.drop(columns=["CreateDate"]))
    finally:
        conn.close()

    total_df = frames[0]
    for frame in frames[1:]:
        total_df = pandas.merge(total_df, frame)
    return total_df


AMAZON_REPORT_MAX_AGE_S = 3 * 60 * 60


def _report_age_s(path):
    """Age in seconds of a usable (existing, non-empty) report file, else None."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return None
    return time.time() - os.path.getmtime(path)


def load_amazon_report(settings, download=True):
    """If download, run the ported create_amazon_report() into a temp file and
    swap it into AMAZON_REPORT_PATH only once complete; read with utf-8-sig then
    ISO-8859-1 fallback, converters={'seller-sku': str}.

    If the download fails (Amazon sometimes leaves the report queued for a long
    time), fall back to the last successfully downloaded report as long as it is
    no older than 3h; only then raise GuardError (R7: abort, no writes)."""
    path = settings.amazon_report_path
    if download:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp_path = path + ".tmp"
        try:
            create_amazon_report(settings, tmp_path)
            if _report_age_s(tmp_path) is None:
                raise RuntimeError("downloaded report is missing/empty")
            os.replace(tmp_path, path)
        except Exception as exc:
            age_s = _report_age_s(path)
            if age_s is None or age_s > AMAZON_REPORT_MAX_AGE_S:
                raise GuardError("amazon report download failed and no report from the last 3h to fall "
                                 "back on: {}".format(exc))
            log.warning("amazon report download failed (%s); using previous report from %.0f min ago",
                        exc, age_s / 60)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

        age_s = _report_age_s(path)
        if age_s is None:
            raise GuardError("amazon report missing/empty: {}".format(path))
        if age_s > AMAZON_REPORT_MAX_AGE_S:
            raise GuardError("amazon report is older than 3h: {}".format(path))

    try:
        return pandas.read_table(path, encoding="utf-8-sig", low_memory=False, converters={"seller-sku": str})
    except Exception:
        return pandas.read_table(path, encoding="ISO-8859-1", low_memory=False, converters={"seller-sku": str})


def amazon_report_mtime(settings):
    """Local-clock datetime of the report file load_amazon_report just used.
    A fallback to the previous report still reads the same path, so its mtime
    is that of the file actually used."""
    return datetime.datetime.fromtimestamp(os.path.getmtime(settings.amazon_report_path))


_AU_IB_NEW_SKU = re.compile(r"^AU-IB-(\d+)$")

_AMAZON_IB_PRICE_DELETE = "DELETE FROM inventory.amazon_ib_price"
_AMAZON_IB_PRICE_INSERT = (
    "INSERT INTO inventory.amazon_ib_price (upc, sku, price, report_time) VALUES (%s, %s, %s, %s)"
)


def save_amazon_ib_prices(db, amazon_df, report_time, dry_run=False):
    """Snapshot the Active AU-IB-<upc> (NEW sku only, exact match, price > 0)
    prices into inventory.amazon_ib_price for the IBS order-placement side.
    DELETE + INSERT in one transaction. An empty filtered set deletes nothing
    (guards against a bad report). report_time is the report file's mtime in
    machine-local time (assumed to be the same clock as the DB's DATETIME).
    Returns the number of rows saved (0 when skipped / dry run)."""
    if not {"seller-sku", "price", "status"}.issubset(amazon_df.columns):
        log.warning("amazon_ib_price: report lacks seller-sku/price/status columns; snapshot not replaced")
        return 0

    sku = amazon_df["seller-sku"].astype(str).str.strip()
    upc = sku.str.extract(_AU_IB_NEW_SKU, expand=False)
    price = pandas.to_numeric(amazon_df["price"], errors="coerce")
    active = amazon_df["status"].astype(str).str.strip() == "Active"
    keep = upc.notna() & active & (price > 0)

    rows = []
    seen = set()
    for u, s, p in zip(upc[keep], sku[keep], price[keep]):
        if u in seen:  # upc is the PK; first listing wins
            continue
        seen.add(u)
        rows.append((u, s, round(float(p), 2), report_time))

    if not rows:
        log.warning("amazon_ib_price: no Active AU-IB-<upc> rows with price > 0 in the report; "
                    "snapshot not replaced")
        return 0
    if dry_run:
        log.info("dry-run: would replace inventory.amazon_ib_price with %d row(s) (report_time %s)",
                 len(rows), report_time)
        return 0

    conn = db.connect()
    try:
        db_mod.replace_all_rows(conn, _AMAZON_IB_PRICE_DELETE, _AMAZON_IB_PRICE_INSERT, rows)
    finally:
        conn.close()
    log.info("amazon_ib_price: replaced snapshot with %d row(s) (report_time %s)", len(rows), report_time)
    return len(rows)


def load_rma_sheet(settings):
    """R7: raise GuardError if the sheet returns 0 rows with a UPC."""
    df = get_info_from_google_docs(settings, settings.rma_sheet_range, settings.rma_sheet_id)
    has_upc = df["UPC"].astype(str).str.strip().ne("") if "UPC" in df.columns else pandas.Series([], dtype=bool)
    if not bool(has_upc.any()):
        raise GuardError("RMA sheet returned 0 rows with a UPC")
    return df


def load_restricted_skus(db):
    """R7: raise GuardError if 0 rows."""
    conn = db.connect()
    try:
        df = _query_df(conn, "select SKU from inventory.restricted_skus;")
    finally:
        conn.close()
    if df.empty:
        raise GuardError("inventory.restricted_skus returned 0 rows")
    return df


def load_map_list(db):
    """select * from inventory.ca_map_price_sheet -> columns ['upc','map_price']
    (the old xlsx read was dead code; dropped). R7: raise GuardError if 0 rows."""
    conn = db.connect()
    try:
        rows, _columns = db_mod.run_query(conn, "select * from inventory.ca_map_price_sheet;")
    finally:
        conn.close()
    df = pandas.DataFrame(rows, columns=["upc", "map_price"])
    if df.empty:
        raise GuardError("inventory.ca_map_price_sheet returned 0 rows")
    return df


def load_upc_exceptions(settings):
    df = pandas.read_excel(settings.upc_exception_path, index_col=None, dtype=str)
    return df

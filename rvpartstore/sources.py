"""I/O loaders for pricing inputs (design spec sec. 5). Thin: each function
fetches data and does the minimal shape work described in the spec; all the
actual pricing logic lives in pricing.py.

R7: guard checks live here too (abort the pricing/push step, no writes, when
an input isn't ready) — each loader raises GuardError so main.py can catch one
exception type and log+abort.
"""

import logging
import os
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


def load_amazon_report(settings, download=True):
    """If download, run the ported create_amazon_report() writing to
    AMAZON_REPORT_PATH; read with utf-8-sig then ISO-8859-1 fallback,
    converters={'seller-sku': str}. R7: raise GuardError if the download
    raises, or the file is missing/empty/older than 3h after download."""
    path = settings.amazon_report_path
    if download:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        try:
            create_amazon_report(settings, path)
        except Exception as exc:
            raise GuardError("amazon report download failed: {}".format(exc))

        if not os.path.exists(path) or os.path.getsize(path) == 0:
            raise GuardError("amazon report missing/empty after download: {}".format(path))
        age_s = time.time() - os.path.getmtime(path)
        if age_s > 3 * 60 * 60:
            raise GuardError("amazon report is older than 3h after download: {}".format(path))

    try:
        return pandas.read_table(path, encoding="utf-8-sig", low_memory=False, converters={"seller-sku": str})
    except Exception:
        return pandas.read_table(path, encoding="ISO-8859-1", low_memory=False, converters={"seller-sku": str})


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

"""Pull open orders -> ORDER_TABLE (design spec sec. 8), with R4/R10/R15/V7.

ORDER_TABLE is the SAME MySQL table the old store writes to (shared;
distinguished by TxnID prefix). ORDER_COLUMNS below match its schema exactly
(verified with SHOW CREATE TABLE inventory.shopify_rvmarines_order).
"""

import datetime
import json
import logging
import os

import pandas

from . import db as db_mod
from .shopify_client import ShopifyError

log = logging.getLogger(__name__)

ORDER_COLUMNS = [
    "TxnID", "shopifyOrderId", "itemId", "fulfillmentOrderId", "fulfillmentOrderLineItemId",
    "product_id", "Items_Name", "Items_Desc", "Items_Quantity", "Items_Amount", "Items_Shipping",
    "StoreType", "company", "To_Name", "To_Street", "To_Street2", "To_State", "To_City",
    "To_ZipCode", "To_CountryCode", "To_PhoneNumber", "email", "Total_Total", "Details_PaymentDate",
    "PaymentStatus", "riskLevel", "Promise_Date",
]

_FULFILLMENT_DAYS = {
    # BRX (B3, shared here per the old code's single unconditional vendor
    # table — brakex_orders.py reuses this dict/function as-is, no RV
    # behaviour change since RV vendor codes never include "BRX").
    "IB": 1, "CW": 5, "ME": 3, "KS": 1, "MS": 2, "TF": 5, "PA": 5,
    "CBK": 3, "PR": 3, "WE": 5, "backorder": 28, "BRX": 2,
}
_DEFAULT_FULFILLMENT_DAYS = 4

_RISK_RANK = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}


class OrderBuildError(Exception):
    """A single order failed to build (e.g. sku missing from the FO line-item
    map); the caller logs and skips just that order (design spec sec. 8)."""


def _highest_risk(levels):
    """R4 fallback: max of risk.assessments[].riskLevel; NONE/PENDING/empty -> LOW; never null."""
    best = "LOW"
    best_rank = 0
    for level in levels or []:
        rank = _RISK_RANK.get(level, 0)
        if rank > best_rank:
            best = level
            best_rank = rank
    return best


def _promise_date(order_date, vendor, today):
    """Port of the old Promise_Date logic, verbatim, except `datetime.datetime.today()`
    is replaced by the injected `today` parameter so this stays pure/testable."""
    fulfillment_time = _FULFILLMENT_DAYS.get(vendor, _DEFAULT_FULFILLMENT_DAYS)
    if fulfillment_time >= 5:
        fulfillment_time = fulfillment_time + (fulfillment_time // 5) * 2
    else:
        today_index = int(today.strftime("%u"))
        fulfillment_index = int((today + datetime.timedelta(days=fulfillment_time)).strftime("%u"))
        if fulfillment_index <= today_index:
            fulfillment_time += 2
        elif fulfillment_index >= 6:
            fulfillment_time += 2
    return (order_date + datetime.timedelta(days=fulfillment_time)).strftime("%Y-%m-%d")


def build_order_rows(order_json, fo_info, vendor_lookup, today, txn_prefix="RVM_SP_N", store_type="shopifyca_rv"):
    """Pure. order_json: REST order dict. fo_info: {'risk_level', 'fulfillment_order_ids',
    'line_item_map': {sku: fulfillmentOrderLineItemId}}. vendor_lookup: {product_id: vendor_code_or_None}.
    Raises OrderBuildError if a line item's sku isn't in fo_info['line_item_map']."""
    shipping = order_json.get("shipping_address") or {}
    company = shipping.get("company")
    to_name = shipping.get("name")
    to_street = shipping.get("address1")
    to_street2 = shipping.get("address2")
    to_state = shipping.get("province_code")
    to_city = shipping.get("city")
    to_zipcode = shipping.get("zip")
    to_countrycode = shipping.get("country_code")
    to_phone = shipping.get("phone")
    email = order_json.get("email")
    total = order_json.get("total_price")

    txn_id = txn_prefix + str(order_json["order_number"])
    processed_at = order_json.get("processed_at") or order_json.get("created_at")
    detail_payment = pandas.to_datetime(processed_at).date()

    fulfillment_order_id = "|".join(fo_info.get("fulfillment_order_ids") or [])
    line_item_map = fo_info.get("line_item_map") or {}
    risk_level = fo_info.get("risk_level") or "LOW"

    rows = []
    for item in order_json.get("line_items", []):
        # V7: Items_Quantity = current_quantity (fallback quantity); R15:
        # skip lines with 0 remaining.
        original_quantity = item.get("quantity") or 0
        current_quantity = item.get("current_quantity", original_quantity)
        if not current_quantity:
            continue

        sku = item.get("sku")
        line_item_id = line_item_map.get(sku)
        if line_item_id is None:
            raise OrderBuildError("sku %r not found in fulfillment order line items for order %s" % (sku, txn_id))

        discount_allocations = item.get("discount_allocations") or []
        discount_amount = float(discount_allocations[0]["amount"]) if discount_allocations else 0.0
        # V7: per-unit discount uses the ORIGINAL quantity, not current_quantity.
        item_amount = round(float(item["price"]) - (discount_amount / original_quantity if original_quantity else 0.0), 2)

        product_id = item.get("product_id")
        # Item 10 (reviewer fix): build_order_rows is a pure, directly
        # black-box-tested seam — its documented contract is a plain lookup
        # by whatever type product_id naturally is (int, as Shopify REST line
        # items give it). The str/int mismatch this item is actually about
        # (snapshot's str productId vs REST's int product_id) is normalized
        # by the CALLER, pull_open_orders, when it populates vendor_lookup —
        # see there — so this function's contract doesn't need to change.
        vendor = vendor_lookup.get(product_id)
        promise_date = _promise_date(detail_payment, vendor, today)

        rows.append({
            "TxnID": txn_id,
            "shopifyOrderId": order_json["id"],
            "itemId": item.get("id"),
            "fulfillmentOrderId": fulfillment_order_id,
            "fulfillmentOrderLineItemId": line_item_id,
            "product_id": product_id,
            "Items_Name": sku,
            "Items_Desc": item.get("title"),
            "Items_Quantity": current_quantity,
            "Items_Amount": item_amount,
            "Items_Shipping": 0,
            "StoreType": store_type,
            "company": company,
            "To_Name": to_name,
            "To_Street": to_street,
            "To_Street2": to_street2,
            "To_State": to_state,
            "To_City": to_city,
            "To_ZipCode": to_zipcode,
            "To_CountryCode": to_countrycode,
            "To_PhoneNumber": to_phone,
            "email": email,
            "Total_Total": total,
            "Details_PaymentDate": detail_payment.strftime("%Y-%m-%d"),
            "PaymentStatus": "Awaiting Fulfillment",
            "riskLevel": risk_level,
            "Promise_Date": promise_date,
        })
    return rows


def _load_existing_order_ids(conn, db_mod, table_name):
    """Shared with brakex_orders.py (B3) — takes a table name directly rather
    than settings, since the two callers read different tables."""
    rows, _columns = db_mod.run_query(conn, "select shopifyOrderId from {};".format(table_name))
    return set(str(r[0]) for r in rows)


def _iter_open_orders(client):
    params = {"status": "open", "limit": 250}
    for page in client.rest_get_paginated("orders.json", params):
        for order in page.get("orders", []):
            yield order


def _fetch_order_detail(client, shopify_order_id):
    """Raises ShopifyError (never TypeError) if the order comes back null or
    riskLevel is null on both the primary and fallback query (code review
    fix: a null `data["order"]` used to blow up with an uncaught TypeError
    instead of being treated as a fetch failure)."""
    gid = "gid://shopify/Order/{}".format(shopify_order_id)
    query = """query($id: ID!) {
        order(id: $id) {
            id
            riskLevel
            fulfillmentOrders(first: 10) {
                edges { node { id lineItems(first: 50) { edges { node { id sku } } } } }
            }
        }
    }"""
    order = None
    risk_level = None
    try:
        data = client.graphql(query, {"id": gid})
        order = (data or {}).get("order")
        if order is None:
            raise ShopifyError("order(id: %s) returned null" % gid)
        risk_level = order.get("riskLevel")
        if risk_level is None:
            raise ShopifyError("order(id: %s).riskLevel is null" % gid)
    except ShopifyError:
        log.warning("order_id=%s: Order.riskLevel query failed/null, falling back to risk.assessments", shopify_order_id)
        fallback_query = """query($id: ID!) {
            order(id: $id) {
                id
                risk { assessments { riskLevel } }
                fulfillmentOrders(first: 10) {
                    edges { node { id lineItems(first: 50) { edges { node { id sku } } } } }
                }
            }
        }"""
        data = client.graphql(fallback_query, {"id": gid})
        order = (data or {}).get("order")
        if order is None:
            raise ShopifyError("order(id: %s) returned null on fallback query too" % gid)
        levels = [a["riskLevel"] for a in ((order.get("risk") or {}).get("assessments") or [])]
        risk_level = _highest_risk(levels)

    fo_ids = []
    line_item_map = {}
    for edge in (order.get("fulfillmentOrders") or {}).get("edges", []):
        fo = edge["node"]
        fo_ids.append(fo["id"].split("/")[-1])
        for li_edge in fo["lineItems"]["edges"]:
            sku = li_edge["node"]["sku"]
            line_item_map[sku] = li_edge["node"]["id"].split("/")[-1]
    return {"risk_level": risk_level, "fulfillment_order_ids": fo_ids, "line_item_map": line_item_map}


def _resolve_vendor(client, vendor_lookup, product_id, warned):
    """product_id: the int product_id as Shopify REST gives it. vendor_lookup
    is keyed by int product_id throughout (item 10 reviewer fix) — the
    snapshot-sourced entries seeded in pull_open_orders are cast to int at
    that one seeding point so both populations use the same key type."""
    if product_id in vendor_lookup:
        return vendor_lookup[product_id]
    query = """query($id: ID!) {
        product(id: $id) { metafield(namespace: "custom", key: "vendorname") { value } }
    }"""
    vendor = None
    try:
        data = client.graphql(query, {"id": "gid://shopify/Product/{}".format(product_id)})
        metafield = (data.get("product") or {}).get("metafield")
        if metafield:
            try:
                parsed = json.loads(metafield["value"])
                vendor = parsed[0] if isinstance(parsed, list) and parsed else metafield["value"]
            except (ValueError, TypeError):
                vendor = metafield["value"]
    except ShopifyError:
        log.exception("productId=%s: failed fetching fallback vendorname metafield", product_id)

    if vendor is None and product_id not in warned:
        log.warning("productId=%s: no vendorname metafield found; Promise_Date will use the default %d-day fulfillment time",
                    product_id, _DEFAULT_FULFILLMENT_DAYS)
        warned.add(product_id)
    vendor_lookup[product_id] = vendor
    return vendor


def pull_open_orders(client, db, settings, snapshot_df):
    """db: rvpartstore.db.Db instance. snapshot_df: from snapshot.fetch_snapshot()
    (may be None if the snapshot step failed — orders still get pulled, with an
    empty vendor lookup, per main.py's R6-adjacent step ordering)."""
    vendor_lookup = {}
    if snapshot_df is not None and not snapshot_df.empty:
        for row in snapshot_df.itertuples(index=False):
            if row.metafieldValue:
                # Item 10 (reviewer fix): snapshot.py's productId is a numeric
                # string but REST line items' product_id (the key
                # build_order_rows actually looks up by) is an int — normalize
                # to int here, at the one place these two populations meet,
                # rather than changing build_order_rows' tested int-keyed
                # contract.
                vendor_lookup[int(row.productId)] = row.metafieldValue

    conn = db.connect()
    try:
        existing_ids = _load_existing_order_ids(conn, db_mod, settings.order_table)

        inserted = 0
        skipped = 0
        errors = 0
        dry_run_rows = []
        warned_products = set()
        cutoff = datetime.datetime.now() - datetime.timedelta(days=settings.order_max_age_days)
        today = datetime.date.today()

        for order in _iter_open_orders(client):
            try:
                order_id = str(order["id"])
                if order_id in existing_ids:
                    # Item 10 (reviewer fix): count expected skips, not just errors.
                    skipped += 1
                    continue
                if order_id in settings.order_id_blocklist:
                    skipped += 1
                    continue
                try:
                    created_at = datetime.datetime.strptime(order["created_at"][:10], "%Y-%m-%d")
                except (KeyError, ValueError):
                    log.exception("order_id=%s: unparseable created_at; skipping", order_id)
                    errors += 1
                    continue
                if created_at < cutoff:
                    skipped += 1
                    continue

                # R10: protected customer data guard.
                shipping = order.get("shipping_address")
                if not shipping or not shipping.get("name") or not shipping.get("address1"):
                    log.error("order_id=%s: possibly missing protected customer data access "
                             "(no shipping_address/name/address1); skipping, will retry next run", order_id)
                    errors += 1
                    continue

                try:
                    fo_info = _fetch_order_detail(client, order_id)
                except ShopifyError:
                    log.exception("order_id=%s: failed fetching fulfillment orders/risk; skipping", order_id)
                    errors += 1
                    continue

                for item in order.get("line_items", []):
                    # Item 10: vendor_lookup is keyed by int product_id (REST's
                    # native type — see the snapshot-seeding block above).
                    _resolve_vendor(client, vendor_lookup, item.get("product_id"), warned_products)

                try:
                    rows = build_order_rows(order, fo_info, vendor_lookup, today, settings.txn_prefix, settings.store_type)
                except OrderBuildError:
                    log.exception("order_id=%s: failed building order rows; skipping", order_id)
                    errors += 1
                    continue

                if not rows:
                    # Item 6 (review fix): an order whose lines are all
                    # current_quantity==0 is an expected skip, not an error.
                    log.warning("order_id=%s: no line items with positive current_quantity; skipping", order_id)
                    skipped += 1
                    continue

                financial_status = order.get("financial_status")
                if financial_status not in ("paid", "partially_paid"):
                    log.warning("order_id=%s: financial_status=%r is not paid/partially_paid", order_id, financial_status)

                if settings.dry_run:
                    dry_run_rows.extend(rows)
                    inserted += len(rows)
                    continue

                try:
                    _insert_order_rows(conn, db_mod, settings.order_table, ORDER_COLUMNS, rows)
                    inserted += len(rows)
                except Exception:
                    log.exception("order_id=%s: DB insert failed", order_id)
                    errors += 1
            except Exception:
                # Code review fix (item 1): per-order isolation — anything
                # unexpected that escaped the specific handlers above must
                # still only skip THIS order, never abort the whole run.
                log.exception("order_id=%s: unexpected error processing order; skipping", order.get("id"))
                errors += 1
                continue

        if settings.dry_run and dry_run_rows:
            _write_dry_run_csv(settings, dry_run_rows, ORDER_COLUMNS, kind="orders")

        return {"inserted": inserted, "skipped": skipped, "errors": errors}
    finally:
        conn.close()


def _insert_order_rows(conn, db_mod, table_name, columns, rows):
    """Shared with brakex_orders.py (B3) — table name/columns passed in
    directly since the two callers write different tables/schemas."""
    placeholders = ", ".join(["%s"] * len(columns))
    query = "insert ignore into {} ({}) values ({});".format(
        table_name, ", ".join(columns), placeholders
    )
    params = [tuple(row[col] for col in columns) for row in rows]
    db_mod.run_write(conn, query, params, many=True)


def _write_dry_run_csv(settings, rows, columns, kind="orders"):
    """Shared with brakex_orders.py (B3)."""
    df = pandas.DataFrame(rows, columns=columns)
    out_dir = os.path.join(settings.project_root, "data", "dry_run")
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(out_dir, ts, kind + ".csv")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df.to_csv(path, index=False)
    log.info("DRY_RUN: wrote %d %s row(s) to %s (no DB insert made)", len(df.index), kind, path)

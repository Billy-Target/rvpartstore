"""Drop-in replacement for tracking_automation's ``shopify_tracking.ship_orders``.

R3: stdlib + requests + mysql.connector ONLY (imported at runtime by another
project — tracking_automation — on cut-over day; must not drag in pandas).
Import-safe: importing this module must not configure logging, read .env, or
touch the network. Settings/client are created lazily on first call.

V1 (routing): the route decision happens BEFORE loading any settings, against
the hard-coded constant NEW_STORE_PREFIX ("RVM_SP_N") — not against
config.TXN_PREFIX (that would require loading settings just to route). The
upload job asserts TXN_PREFIX == NEW_STORE_PREFIX at startup (config.validate_tracking).
Legacy path is NOT wrapped in try/except — exceptions from the old
shopify_tracking.ship_orders propagate exactly as today. Only ImportError of
that legacy module is caught.
"""

import importlib
import logging

log = logging.getLogger(__name__)

# V1: hard-coded, checked before settings/config are touched.
NEW_STORE_PREFIX = "RVM_SP_N"

_client = None
_settings = None


def _get_client_and_settings():
    global _client, _settings
    if _client is None:
        # Local imports: keep this module's own import (at package-load time)
        # free of anything beyond stdlib, per R3. config/db/shopify_client are
        # themselves stdlib+requests+mysql.connector only, so this is safe.
        from . import config as _config
        from . import shopify_client as _shopify_client

        _settings = _config.load_settings()
        _config.validate_tracking(_settings)
        _client = _shopify_client.ShopifyClient(_settings)
    return _client, _settings


def ship_orders(order_id, sku, tracking, carrier):
    """Same signature/semantics as the old shopify_tracking.ship_orders.

    sku: '' -> all rows of the TxnID; 'a|b' -> those skus; else that one sku.
    Returns True on success, False on failure. Never raises for the new-store
    path (errors are logged and False is returned so the caller's loop keeps
    going, same as the old code's swallow-everything behaviour). The legacy
    path is NOT wrapped — its exceptions propagate as before (R1/V1).
    """
    if not order_id.startswith(NEW_STORE_PREFIX):
        # R1/V1: legacy (old store) in-flight order -> delegate to the
        # caller's own existing shopify_tracking module, resolvable because
        # tracking_automation's directory is on sys.path when it runs. NOT
        # wrapped in try/except beyond the ImportError check (V1): exceptions
        # from the legacy module propagate exactly as today.
        try:
            legacy = importlib.import_module("shopify_tracking")
        except ImportError:
            log.error("legacy order_id=%s but 'shopify_tracking' module not importable", order_id)
            return False
        return legacy.ship_orders(order_id, sku, tracking, carrier)

    # Item 1 (reviewer fix): wrap the ENTIRE new-store path in one
    # try/except so nothing here can ever raise to the caller — settings/
    # client construction, DB access, every GraphQL call, and response
    # parsing (e.g. a null fulfillmentCreate/fulfillment payload) included.
    try:
        return _ship_new_store(order_id, sku, tracking, carrier)
    except Exception:
        log.exception("ship_orders: unexpected error for order_id=%s sku=%r", order_id, sku)
        return False


def _ship_new_store(order_id, sku, tracking, carrier):
    from . import db as _db

    client, settings = _get_client_and_settings()

    rows = _fetch_order_rows(_db, settings, order_id, sku)
    if not rows:
        log.error("ship_orders: no matching rows for order_id=%s sku=%r", order_id, sku)
        return False

    fo_lookup = _fetch_fulfillment_orders(client, order_id, rows)

    fulfillment_inputs, shipped_rows = build_fulfillment_input(rows, fo_lookup)
    if not fulfillment_inputs:
        log.error("ship_orders: nothing remaining to ship for order_id=%s sku=%r", order_id, sku)
        return False

    mutation = """mutation fulfillmentCreate($fulfillment: FulfillmentInput!) {
        fulfillmentCreate(fulfillment: $fulfillment) {
            fulfillment { id status }
            userErrors { field message }
        }
    }"""
    variables = {
        "fulfillment": {
            "lineItemsByFulfillmentOrder": fulfillment_inputs,
            "notifyCustomer": True,
            "trackingInfo": {"company": carrier, "number": tracking},
        }
    }

    result = client.graphql(mutation, variables)
    payload = (result or {}).get("fulfillmentCreate") or {}
    if payload.get("userErrors"):
        log.error("ship_orders: fulfillmentCreate userErrors for order_id=%s: %s", order_id, payload["userErrors"])
        return False

    fulfillment = payload.get("fulfillment")
    if not fulfillment or not fulfillment.get("id"):
        log.error("ship_orders: fulfillmentCreate returned no fulfillment for order_id=%s: %s", order_id, result)
        return False

    fulfillment_id = fulfillment["id"].split("/")[-1]
    try:
        _record_fulfillment(_db, settings, order_id, fulfillment_id, shipped_rows)
    except Exception:
        log.exception("ship_orders: shipped on Shopify but failed recording to %s for order_id=%s",
                      settings.fulfillment_table, order_id)
        # Shopify fulfillment already happened; still report success (matches
        # old code, which also only logged-and-continued on the DB step).
    return True


def _fetch_order_rows(db, settings, order_id, sku):
    """Parameterized SQL (the old code used string %-formatting)."""
    conn = db.connect(settings)
    try:
        base = (
            "select TxnID, shopifyOrderId, fulfillmentOrderId, fulfillmentOrderLineItemId, "
            "Items_Name, Items_Quantity from {} where TxnID = %s".format(settings.order_table)
        )
        if sku == "":
            query = base + ";"
            params = (order_id,)
        elif "|" in sku:
            sku_list = [s for s in sku.split("|") if s != ""]
            placeholders = " or ".join(["Items_Name = %s"] * len(sku_list))
            query = base + " and (" + placeholders + ");"
            params = tuple([order_id] + sku_list)
        else:
            query = base + " and Items_Name = %s;"
            params = (order_id, sku)

        rows, columns = db.run_query(conn, query, params)
        return [dict(zip(columns, row)) for row in rows]
    finally:
        conn.close()


def _fetch_fulfillment_orders(client, order_id, rows):
    """fulfillmentOrderId in the DB row may hold 'a|b' (all FO ids of the
    order) so the FO for each line item must come from Shopify, not that
    column: fetch the order's fulfillment orders via GraphQL and map each
    stored fulfillmentOrderLineItemId to its FO (design spec sec. 9). Returns
    {fulfillment_order_line_item_id: (fo_gid, remainingQuantity)}."""
    shopify_order_id = str(rows[0]["shopifyOrderId"])
    query = """query($id: ID!) {
        order(id: $id) {
            fulfillmentOrders(first: 10) {
                edges { node {
                    id
                    status
                    lineItems(first: 50) { edges { node { id remainingQuantity } } }
                } }
            }
        }
    }"""
    data = client.graphql(query, {"id": "gid://shopify/Order/{}".format(shopify_order_id)})
    order = data.get("order")
    lookup = {}
    if not order:
        return lookup
    for edge in order["fulfillmentOrders"]["edges"]:
        fo = edge["node"]
        if fo["status"] not in ("OPEN", "IN_PROGRESS"):
            continue
        for li_edge in fo["lineItems"]["edges"]:
            node = li_edge["node"]
            line_item_id = node["id"].split("/")[-1]
            lookup[line_item_id] = (fo["id"], node["remainingQuantity"])
    return lookup


def build_fulfillment_input(rows, fo_lookup):
    """Pure: group DB rows by the fulfillment order actually returned by
    Shopify (old code sent only the first FO's id with ALL line items — a bug
    when an order has >1 FO). quantity = min(stored qty, remainingQuantity);
    only OPEN/IN_PROGRESS FOs (already filtered into fo_lookup); rows with
    nothing remaining are skipped. Returns (fulfillment_inputs, shipped_rows)."""
    by_fo = {}
    shipped_rows = []
    for row in rows:
        line_item_id = str(row["fulfillmentOrderLineItemId"])
        entry = fo_lookup.get(line_item_id)
        if entry is None:
            continue
        fo_gid, remaining = entry
        if remaining <= 0:
            continue
        quantity = min(int(row["Items_Quantity"]), remaining)
        if quantity <= 0:
            continue
        by_fo.setdefault(fo_gid, []).append({
            "id": "gid://shopify/FulfillmentOrderLineItem/{}".format(line_item_id),
            "quantity": quantity,
        })
        shipped_rows.append((row["Items_Name"], quantity))

    fulfillment_inputs = [
        {"fulfillmentOrderId": fo_gid, "fulfillmentOrderLineItems": items}
        for fo_gid, items in by_fo.items()
    ]
    return fulfillment_inputs, shipped_rows


def _record_fulfillment(db, settings, order_id, fulfillment_id, shipped_rows):
    conn = db.connect(settings)
    try:
        query = (
            "insert ignore into {} (TxnID, fulfillmentId, sku, quantity, deleted, CreateDate) "
            "values (%s, %s, %s, %s, 0, curdate())".format(settings.fulfillment_table)
        )
        params = [(order_id, fulfillment_id, sku, qty) for sku, qty in shipped_rows]
        db.run_write(conn, query, params, many=True)
    finally:
        conn.close()

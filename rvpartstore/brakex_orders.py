"""Pull Brakex (brakex.myshopify.com) open orders -> BRAKEX_ORDER_TABLE
(design spec sec. B, B1-B5). Port of the brakex branch of francis-shopify's
get_orders.py::get_open_orders.

Shares the generic fetch/skip/DB-write helpers with orders.py (R10 shipping
guard, R4 riskLevel fallback, pagination, insert/dry-run-CSV plumbing) WITHOUT
changing any RV behaviour — those helpers were generalized to take a table
name/columns instead of assuming the RV settings/table (see orders.py). The
only things genuinely specific to Brakex are: TxnID/StoreType/PaymentMethod,
the paypal total/item-amount currency-conversion branches, and that vendor is
always 'BRX' (no vendor_lookup / no custom.vendorname metafield lookup).

DROPPED from the old code (B3): the shopify_brakex_sku_upc_mapping lookup —
its only use was an inventory decrement the old code never actually applied
for brakex, and it crashed the whole run whenever a product was unmapped.
"""

import datetime
import logging

import pandas

from . import db as db_mod
from .orders import (
    OrderBuildError,
    _fetch_order_detail,
    _insert_order_rows,
    _iter_open_orders,
    _load_existing_order_ids,
    _promise_date,
    _write_dry_run_csv,
)
from .shopify_client import ShopifyError

log = logging.getLogger(__name__)

# B — 28 columns, confirmed via SHOW CREATE TABLE inventory.shopify_brakex_order.
BRAKEX_ORDER_COLUMNS = [
    "TxnID", "shopifyOrderId", "itemId", "fulfillmentOrderId", "fulfillmentOrderLineItemId",
    "product_id", "Items_Name", "Items_Desc", "Items_Quantity", "Items_Amount", "Items_Shipping",
    "StoreType", "company", "To_Name", "To_Street", "To_Street2", "To_State", "To_City",
    "To_ZipCode", "To_CountryCode", "To_PhoneNumber", "email", "Total_Total", "Details_PaymentDate",
    "PaymentMethod", "PaymentStatus", "riskLevel", "Promise_Date",
]


def _discount_dict(item):
    """Old paypal branch's discount_dict: {"CAD": 0, "USD": 0} when there are
    no discount_allocations, else whichever of shop_money/presentment_money
    currencies are present in discount_allocations[0].amount_set."""
    allocations = item.get("discount_allocations") or []
    if not allocations:
        return {"CAD": 0, "USD": 0}
    amount_set = allocations[0].get("amount_set") or {}
    discount_dict = {}
    shop_money = amount_set.get("shop_money")
    if shop_money:
        discount_dict[shop_money["currency_code"]] = shop_money["amount"]
    presentment_money = amount_set.get("presentment_money")
    if presentment_money:
        discount_dict[presentment_money["currency_code"]] = presentment_money["amount"]
    return discount_dict


def _require_presentment_amount(presentment_money, context):
    """Item 2 (review fix): a paypal order's presentment_money.amount must be
    present — silently treating a missing amount as 0.0 would write a wrong
    (understated) Total/Items_Amount instead of flagging the order. Raises
    OrderBuildError (skip+log just this order) instead."""
    amount = (presentment_money or {}).get("amount")
    if amount is None:
        raise OrderBuildError("missing presentment_money.amount for %s" % context)
    return float(amount)


def _paypal_item_amount(item, original_quantity, rate, context):
    """B3 paypal item-amount branch, ported verbatim (modulo the item 2 guard above)."""
    item_presentment = (item.get("total_discount_set") or {}).get("presentment_money") or {}
    discount_dict = _discount_dict(item)
    price_presentment_amount = _require_presentment_amount(
        (item.get("price_set") or {}).get("presentment_money"), context
    )

    if item_presentment.get("currency_code") == "USD":
        if "USD" in discount_dict:
            discount_allocations = float(discount_dict["USD"]) * rate
        else:
            discount_allocations = float(discount_dict.get("CAD", 0))
        item_amount = price_presentment_amount * rate - (
            discount_allocations / original_quantity if original_quantity else 0.0
        )
    else:
        if "CAD" in discount_dict:
            discount_allocations = float(discount_dict["CAD"])
        else:
            discount_allocations = float(discount_dict.get("USD", 0)) * rate
        item_amount = price_presentment_amount - (
            discount_allocations / original_quantity if original_quantity else 0.0
        )
    return item_amount


def build_brakex_order_rows(order_json, fo_info, risk_level, settings, today):
    """Pure. order_json: REST order dict. fo_info: {'fulfillment_order_ids',
    'line_item_map': {sku: fulfillmentOrderLineItemId}}. risk_level: already
    resolved (R4 — never None by the time this is called; defaulted here too
    as a last resort). Raises OrderBuildError if a line item's sku isn't in
    fo_info['line_item_map']."""
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

    txn_id = settings.brakex_txn_prefix + str(order_json["order_number"])
    processed_at = order_json.get("processed_at") or order_json.get("created_at")
    detail_payment = pandas.to_datetime(processed_at).date()

    fulfillment_order_id = "|".join(fo_info.get("fulfillment_order_ids") or [])
    line_item_map = fo_info.get("line_item_map") or {}
    risk_level = risk_level or "LOW"

    gateway_names = order_json.get("payment_gateway_names") or []
    payment_method = gateway_names[0] if gateway_names else None
    is_paypal = payment_method == "paypal"
    rate = settings.brakex_usd_to_cad

    # B3: Total.
    if is_paypal:
        presentment = (order_json.get("current_total_price_set") or {}).get("presentment_money")
        amount = _require_presentment_amount(presentment, "order %s current_total_price_set" % txn_id)
        if (presentment or {}).get("currency_code") == "USD":
            total = round(amount * rate, 2)
        else:
            total = amount
    else:
        total = order_json.get("total_price")

    rows = []
    for item in order_json.get("line_items", []):
        # V7/R15: Items_Quantity = current_quantity (fallback quantity); skip
        # lines with 0 remaining.
        original_quantity = item.get("quantity") or 0
        current_quantity = item.get("current_quantity", original_quantity)
        if not current_quantity:
            continue

        sku = item.get("sku")
        line_item_id = line_item_map.get(sku)
        if line_item_id is None:
            raise OrderBuildError("sku %r not found in fulfillment order line items for order %s" % (sku, txn_id))

        if is_paypal:
            item_amount = _paypal_item_amount(item, original_quantity, rate, "order %s sku %r" % (txn_id, sku))
        else:
            discount_allocations = item.get("discount_allocations") or []
            discount_amount = float(discount_allocations[0]["amount"]) if discount_allocations else 0.0
            # V7: per-unit discount uses the ORIGINAL quantity, not current_quantity.
            item_amount = float(item["price"]) - (discount_amount / original_quantity if original_quantity else 0.0)
        item_amount = round(item_amount, 2)

        product_id = item.get("product_id")
        # B3: vendor is always 'BRX' -> 2-day Promise_Date (same weekend logic).
        promise_date = _promise_date(detail_payment, "BRX", today)

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
            "StoreType": settings.brakex_store_type,
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
            "PaymentMethod": payment_method,
            "PaymentStatus": "Awaiting Fulfillment",
            "riskLevel": risk_level,
            "Promise_Date": promise_date,
        })
    return rows


def pull_brakex_open_orders(client, db, settings):
    """db: rvpartstore.db.Db instance, already bound to the right MySQL
    connection settings. client: a ShopifyClient built with
    ShopifyClient.with_static_token(settings.brakex_shop, ..., settings.brakex_access_token)."""
    conn = db.connect()
    try:
        existing_ids = _load_existing_order_ids(conn, db_mod, settings.brakex_order_table)

        inserted = 0
        skipped = 0
        errors = 0
        dry_run_rows = []
        cutoff = datetime.datetime.now() - datetime.timedelta(days=settings.order_max_age_days)
        today = datetime.date.today()

        for order in _iter_open_orders(client):
            try:
                order_id = str(order["id"])
                if order_id in existing_ids:
                    skipped += 1
                    continue
                if order_id in settings.brakex_order_id_blocklist:
                    skipped += 1
                    continue
                try:
                    created_at = datetime.datetime.strptime(order["created_at"][:10], "%Y-%m-%d")
                except (KeyError, ValueError):
                    log.exception("brakex order_id=%s: unparseable created_at; skipping", order_id)
                    errors += 1
                    continue
                if created_at < cutoff:
                    skipped += 1
                    continue

                # R10: protected customer data guard.
                shipping = order.get("shipping_address")
                if not shipping or not shipping.get("name") or not shipping.get("address1"):
                    log.error("brakex order_id=%s: possibly missing protected customer data access "
                             "(no shipping_address/name/address1); skipping, will retry next run", order_id)
                    errors += 1
                    continue

                try:
                    fo_info = _fetch_order_detail(client, order_id)
                except ShopifyError:
                    log.exception("brakex order_id=%s: failed fetching fulfillment orders/risk; skipping", order_id)
                    errors += 1
                    continue

                try:
                    rows = build_brakex_order_rows(order, fo_info, fo_info.get("risk_level"), settings, today)
                except OrderBuildError:
                    log.exception("brakex order_id=%s: failed building order rows; skipping", order_id)
                    errors += 1
                    continue

                if not rows:
                    # Item 6 (review fix): an order whose lines are all
                    # current_quantity==0 is an expected skip, not an error.
                    log.warning("brakex order_id=%s: no line items with positive current_quantity; skipping", order_id)
                    skipped += 1
                    continue

                financial_status = order.get("financial_status")
                if financial_status not in ("paid", "partially_paid"):
                    log.warning("brakex order_id=%s: financial_status=%r is not paid/partially_paid", order_id, financial_status)

                if settings.dry_run:
                    dry_run_rows.extend(rows)
                    inserted += len(rows)
                    continue

                try:
                    _insert_order_rows(conn, db_mod, settings.brakex_order_table, BRAKEX_ORDER_COLUMNS, rows)
                    inserted += len(rows)
                except Exception:
                    log.exception("brakex order_id=%s: DB insert failed", order_id)
                    errors += 1
            except Exception:
                # Code review fix (item 1): per-order isolation.
                log.exception("brakex order_id=%s: unexpected error processing order; skipping", order.get("id"))
                errors += 1
                continue

        if settings.dry_run and dry_run_rows:
            _write_dry_run_csv(settings, dry_run_rows, BRAKEX_ORDER_COLUMNS, kind="brakex_orders")

        return {"inserted": inserted, "skipped": skipped, "errors": errors}
    finally:
        conn.close()

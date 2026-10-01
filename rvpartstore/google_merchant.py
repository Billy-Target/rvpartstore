"""Ported merchant sync + delete-disapproved (design spec sec. 10), flag-gated
by GOOGLE_MERCHANT_ENABLED. All entry points return immediately if disabled."""

import logging

import pandas

from . import db as db_mod
from . import shopify_writes
from .google_sheets import get_google_content_service

log = logging.getLogger(__name__)


def update_item_price_on_google_merchant(settings, merchant_id, sku, price, compared_at_price, status):
    product_id = "online:en:CA:" + sku
    service = get_google_content_service(settings)
    product = service.products().get(merchantId=merchant_id, productId=product_id).execute()
    product.pop("source", None)
    if price >= compared_at_price:
        product.pop("salePrice", None)
        product["price"]["value"] = str(price)
    else:
        product["salePrice"] = {"value": str(price), "currency": "CAD"}
        product["price"]["value"] = str(compared_at_price)
    product["availability"] = status
    request = service.products().insert(merchantId=merchant_id, body=product)
    return request.execute()


def _update_item_price_multi_id(settings, merchant_id, sku, upc, price, compared_at_price, status):
    """Multi-id retry chain: sku, sku suffix, int suffix, upc, int upc (the
    old create_new_ad_for_rv_marines_ca fallback is dropped, per spec)."""
    candidates = [sku]
    if sku and "-" in sku:
        suffix = sku.split("-")[-1]
        candidates.append(suffix)
        try:
            candidates.append(str(int(suffix)))
        except ValueError:
            pass
    if upc:
        candidates.append(upc)
        try:
            candidates.append(str(int(upc)))
        except ValueError:
            pass

    last_exc = None
    for candidate_id in candidates:
        try:
            return update_item_price_on_google_merchant(settings, merchant_id, candidate_id, price, compared_at_price, status)
        except Exception as exc:
            last_exc = exc
    log.error("google merchant update failed for sku=%s after trying ids=%s: %s", sku, candidates, last_exc)
    return None


def sync_prices(settings, google_df):
    if not settings.google_merchant_enabled or google_df.empty:
        return {"attempted": 0, "succeeded": 0, "failed": 0}

    # Item 6 (reviewer fix): honour DRY_RUN like every other write path.
    if settings.dry_run:
        shopify_writes._write_dry_run_csv(settings, "google", google_df)
        return {"attempted": len(google_df.index), "succeeded": 0, "failed": 0}

    attempted = 0
    succeeded = 0
    failed = 0
    for row in google_df.itertuples(index=False):
        attempted += 1
        result = _update_item_price_multi_id(
            settings, row.google_merchant_id, row.sku, row.upc, row.new_price, row.compareAtPrice, row.stock
        )
        if result is None:
            failed += 1
        else:
            succeeded += 1
    return {"attempted": attempted, "succeeded": succeeded, "failed": failed}


def get_all_product_status_on_google_merchant(settings, merchant_id):
    service = get_google_content_service(settings)
    product_list = []
    request = service.productstatuses().list(merchantId=merchant_id, maxResults=50)
    while request is not None:
        result = request.execute()
        products = result.get("resources")
        if not products:
            break
        product_list.extend(products)
        request = service.products().list_next(request, result)
    return product_list


def delete_all_disapproved_items(settings, db, merchant_id, remove_all=False):
    if not settings.google_merchant_enabled:
        return

    product_list = get_all_product_status_on_google_merchant(settings, merchant_id)
    df = pandas.DataFrame(product_list)
    if df.empty:
        log.info("delete_all_disapproved_items: no products returned")
        return

    df["status"] = df["destinationStatuses"].str[0].str["status"]
    df = df.loc[df["status"] == "disapproved"].reset_index(drop=True)
    if df.empty:
        log.info("delete_all_disapproved_items: no disapproved products")
        return

    df["reason"] = df["itemLevelIssues"].apply(lambda x: [a["code"] for a in x]).str.join("<br>")
    product_ids = df["productId"].to_list()

    if len(product_ids) > 20 and not remove_all:
        # Old code emailed here; email dropped per spec -> WARNING only.
        log.warning("delete_all_disapproved_items: %d disapproved products (>20), skipping automatic delete "
                   "— needs manual review (pass remove_all=True to override)", len(product_ids))
        return

    if settings.dry_run:
        log.info("DRY_RUN: would delete %d disapproved product(s): %s (no Google call made)",
                len(product_ids), product_ids)
        return

    service = get_google_content_service(settings)
    for product_id in product_ids:
        service.products().delete(merchantId=merchant_id, productId=product_id).execute()

    conn = db.connect()
    try:
        df["SKU"] = df["productId"].str[13:]
        query = "insert ignore into inventory.bigcommerce_rv_marines_ca_ad_ban_list (SKU, reason) VALUES (%s, %s);"
        params = [tuple(x) for x in df[["SKU", "reason"]].to_numpy(na_value=None)]
        db_mod.run_write(conn, query, params, many=True)
    finally:
        conn.close()

    log.info("delete_all_disapproved_items: removed %d disapproved product(s) from google merchant center", len(product_ids))

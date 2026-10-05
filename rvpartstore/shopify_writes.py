"""Shopify write operations (design spec sec. 7). All honour DRY_RUN: log +
write a CSV to data/dry_run/<ts>/<kind>.csv, no API calls."""

import datetime
import json
import logging
import os
import uuid

import pandas

from .shopify_client import ShopifyError

log = logging.getLogger(__name__)

_RUN_TS = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def _dry_run_dir(settings):
    path = os.path.join(settings.project_root, "data", "dry_run", _RUN_TS)
    os.makedirs(path, exist_ok=True)
    return path


def _write_dry_run_csv(settings, kind, df):
    path = os.path.join(_dry_run_dir(settings), kind + ".csv")
    df.to_csv(path, index=False)
    log.info("DRY_RUN: wrote %d row(s) to %s (no Shopify call made)", len(df.index), path)


def _summary(attempted, succeeded, failed):
    return {"attempted": attempted, "succeeded": succeeded, "failed": failed}


def update_prices(client, prices_df, settings):
    if prices_df.empty:
        return _summary(0, 0, 0)
    if settings.dry_run:
        _write_dry_run_csv(settings, "prices", prices_df)
        return _summary(len(prices_df.index), 0, 0)

    mutation = """mutation productVariantsBulkUpdate($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
        productVariantsBulkUpdate(productId: $productId, variants: $variants) {
            product { id }
            userErrors { field message }
        }
    }"""
    attempted = 0
    succeeded = 0
    failed = 0
    for row in prices_df.itertuples(index=False):
        attempted += 1
        compare_at = None if pandas.isna(row.compareAtPrice) else float(row.compareAtPrice)
        variables = {
            "productId": "gid://shopify/Product/{}".format(row.productId),
            "variants": [{
                "id": "gid://shopify/ProductVariant/{}".format(row.variantId),
                "price": float(row.new_price),
                "compareAtPrice": compare_at,
            }],
        }
        try:
            result = client.graphql(mutation, variables)
        except ShopifyError:
            log.exception("update_prices: request failed for variantId=%s", row.variantId)
            failed += 1
            continue
        errors = result["productVariantsBulkUpdate"]["userErrors"]
        if errors:
            log.error("update_prices: userErrors for variantId=%s: %s", row.variantId, errors)
            failed += 1
        else:
            succeeded += 1
    return _summary(attempted, succeeded, failed)


def _safe_bool_series(series):
    """Coerce a column that's *supposed* to be boolean back to a clean bool
    dtype, treating NaN/missing as False. Defensive fix (live dry-run finding,
    2026-10-01): stocked/tracked can reach here with a corrupted object/float
    dtype carried over from an upstream merge; `~` on that is bitwise-NOT (not
    logical), which turns True/False into -2/-1 and makes `.loc[...]`
    misinterpret the result as integer labels instead of a boolean mask ->
    KeyError. pricing.compute_changes now normalizes this at the ChangeSet
    boundary too, but this is the second line of defense right where the
    mask is actually used."""
    return series.fillna(False).astype(bool)


def set_inventory(client, settings, inv_df, batch_size=100):
    if inv_df.empty:
        return _summary(0, 0, 0)

    if "stocked" in inv_df.columns:
        stocked = _safe_bool_series(inv_df["stocked"])
        unstocked_count = int((~stocked).sum())
        if unstocked_count:
            log.warning("set_inventory: skipping %d variant(s) with no inventory level at the location", unstocked_count)
        inv_df = inv_df.loc[stocked]

    if "tracked" in inv_df.columns:
        # Item 7 (reviewer fix): a variant with tracked=false can't have its
        # inventory set (Shopify has no per-location level for it); skip +
        # warn, same treatment as stocked=False. R5/_enforce_tracking already
        # autofixes tracked=false->true before this runs, but variants that
        # are trackedEditable.locked or exceed TRACKING_AUTOFIX_MAX are
        # deliberately left untouched there, so this can still trigger.
        tracked = _safe_bool_series(inv_df["tracked"])
        untracked_count = int((~tracked).sum())
        if untracked_count:
            log.warning("set_inventory: skipping %d variant(s) with tracked=false", untracked_count)
        inv_df = inv_df.loc[tracked]

    if inv_df.empty:
        return _summary(0, 0, 0)

    if settings.dry_run:
        _write_dry_run_csv(settings, "inventory", inv_df)
        return _summary(len(inv_df.index), 0, 0)

    # 2026-07: inventory mutations require the @idempotent directive. A fresh
    # key per request; the client's own retries resend the same variables, so
    # a retried request reuses its key.
    mutation = """mutation inventorySetQuantities($input: InventorySetQuantitiesInput!, $idempotencyKey: String!) {
        inventorySetQuantities(input: $input) @idempotent(key: $idempotencyKey) {
            userErrors { field message }
        }
    }"""
    location_gid = "gid://shopify/Location/{}".format(settings.shopify_location_id)

    def quantity_entry(row):
        return {
            "inventoryItemId": "gid://shopify/InventoryItem/{}".format(row.inventoryId),
            "locationId": location_gid,
            "quantity": int(row.target_quantity),
            # Item 2 (2026-07 API): InventoryQuantityInput now has an optional
            # changeFromQuantity; send it explicitly as null so this stays a
            # plain absolute set (no compare-and-swap against a prior value).
            "changeFromQuantity": None,
        }

    attempted = 0
    succeeded = 0
    failed = 0
    rows = list(inv_df.itertuples(index=False))
    for start in range(0, len(rows), batch_size):
        batch = rows[start:start + batch_size]
        variables = {"input": {
            "name": "available",
            "reason": "correction",
            "quantities": [quantity_entry(row) for row in batch],
        }, "idempotencyKey": str(uuid.uuid4())}
        attempted += len(batch)
        try:
            result = client.graphql(mutation, variables)
        except ShopifyError:
            log.exception("set_inventory: batch request failed (%d items)", len(batch))
            failed += len(batch)
            continue

        errors = result["inventorySetQuantities"]["userErrors"]
        if not errors:
            succeeded += len(batch)
            continue

        # Item 7 (reviewer fix): a batch userErrors doesn't tell us which
        # item(s) caused it; retry this batch's items one at a time so one
        # bad item doesn't block the rest.
        log.error("set_inventory: userErrors for batch of %d, retrying individually: %s", len(batch), errors)
        for row in batch:
            single_variables = {"input": {
                "name": "available",
                "reason": "correction",
                "quantities": [quantity_entry(row)],
            }, "idempotencyKey": str(uuid.uuid4())}
            try:
                single_result = client.graphql(mutation, single_variables)
            except ShopifyError:
                log.exception("set_inventory: retry failed for inventoryItemId=%s", row.inventoryId)
                failed += 1
                continue
            single_errors = single_result["inventorySetQuantities"]["userErrors"]
            if single_errors:
                log.error("set_inventory: userErrors for inventoryItemId=%s: %s", row.inventoryId, single_errors)
                failed += 1
            else:
                succeeded += 1
    return _summary(attempted, succeeded, failed)


def set_vendorname(client, vendors_df, settings, batch_size=25):
    if vendors_df.empty:
        return _summary(0, 0, 0)
    if settings.dry_run:
        _write_dry_run_csv(settings, "vendors", vendors_df)
        return _summary(len(vendors_df.index), 0, 0)

    mutation = """mutation metafieldsSet($metafields: [MetafieldsSetInput!]!) {
        metafieldsSet(metafields: $metafields) {
            metafields { id }
            userErrors { field message }
        }
    }"""

    attempted = 0
    succeeded = 0
    failed = 0
    rows = list(vendors_df.itertuples(index=False))
    for start in range(0, len(rows), batch_size):
        batch = rows[start:start + batch_size]
        metafields = [{
            "ownerId": "gid://shopify/Product/{}".format(row.productId),
            "namespace": "custom",
            "key": "vendorname",
            "type": "list.single_line_text_field",
            "value": json.dumps([row.lowest_vendor]),
        } for row in batch]
        variables = {"metafields": metafields}
        attempted += len(batch)
        try:
            result = client.graphql(mutation, variables)
        except ShopifyError:
            log.exception("set_vendorname: batch request failed (%d items)", len(batch))
            failed += len(batch)
            continue
        errors = result["metafieldsSet"]["userErrors"]
        if errors:
            log.error("set_vendorname: userErrors for batch: %s", errors)
            failed += len(batch)
        else:
            succeeded += len(batch)
    return _summary(attempted, succeeded, failed)


def set_tracked(client, settings, inventory_item_ids):
    """R5/V3(a): autofix tracked=false -> true via inventoryItemUpdate, one
    call each (throttled by the client itself). Honours DRY_RUN."""
    if not inventory_item_ids:
        return _summary(0, 0, 0)
    if settings.dry_run:
        df = pandas.DataFrame({"inventoryItemId": inventory_item_ids})
        _write_dry_run_csv(settings, "enable_tracking", df)
        return _summary(len(inventory_item_ids), 0, 0)

    mutation = """mutation inventoryItemUpdate($id: ID!, $input: InventoryItemInput!) {
        inventoryItemUpdate(id: $id, input: $input) {
            inventoryItem { id tracked }
            userErrors { field message }
        }
    }"""
    attempted = 0
    succeeded = 0
    failed = 0
    for inventory_item_id in inventory_item_ids:
        attempted += 1
        variables = {"id": "gid://shopify/InventoryItem/{}".format(inventory_item_id), "input": {"tracked": True}}
        try:
            result = client.graphql(mutation, variables)
        except ShopifyError:
            log.exception("set_tracked: request failed for inventoryItemId=%s", inventory_item_id)
            failed += 1
            continue
        errors = result["inventoryItemUpdate"]["userErrors"]
        if errors:
            log.error("set_tracked: userErrors for inventoryItemId=%s: %s", inventory_item_id, errors)
            failed += 1
        else:
            succeeded += 1
    return _summary(attempted, succeeded, failed)

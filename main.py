#!/usr/bin/env python
"""CLI: python main.py <job> [--dry-run] [--apply] [--ignore-quiet] [--force-breaker]

Jobs: upload | enable_tracking | tracking_audit | delete_disapproved | brakex_orders
(design spec sec. 11, R2/R5/R6/R7/R12/R14/V2/V3/V4/V5; Brakex: sec. B, B1-B5).
`upload` also runs brakex_orders as its final step when BRAKEX_ENABLED.
"""

import argparse
import dataclasses
import datetime
import logging

from rvpartstore import brakex_orders as brakex_orders_mod
from rvpartstore import config
from rvpartstore import db as db_mod
from rvpartstore import google_merchant
from rvpartstore import job_runner
from rvpartstore import orders as orders_mod
from rvpartstore import pricing
from rvpartstore import shopify_client
from rvpartstore import shopify_writes
from rvpartstore import snapshot as snapshot_mod
from rvpartstore import sources
from rvpartstore.shopify_client import ShopifyError
from rvpartstore.sources import GuardError
from rvpartstore.tracking import NEW_STORE_PREFIX

log = logging.getLogger(__name__)


def _load_settings(force_dry_run=False):
    settings = config.load_settings()
    config.validate_upload(settings)
    if settings.txn_prefix != NEW_STORE_PREFIX:
        raise config.MissingSettingsError(
            "TXN_PREFIX (%r) must equal tracking.NEW_STORE_PREFIX (%r)" % (settings.txn_prefix, NEW_STORE_PREFIX)
        )
    if force_dry_run and not settings.dry_run:
        settings = dataclasses.replace(settings, dry_run=True)
    return settings


def _in_quiet_window(settings):
    now = datetime.datetime.now().time()
    start = datetime.datetime.strptime(settings.quiet_start, "%H:%M").time()
    end = datetime.datetime.strptime(settings.quiet_end, "%H:%M").time()
    # Item 9 (reviewer fix): handle both a same-day window (start <= end,
    # e.g. 01:00-05:00: quiet while start <= now < end) and one that crosses
    # midnight (start > end, e.g. 23:30-06:30: quiet while now >= start or
    # now < end).
    if start <= end:
        return start <= now < end
    return now >= start or now < end


def _inserted_label(settings):
    """Order pulls count rows they insert; under DRY_RUN nothing is inserted."""
    return "would_insert" if settings.dry_run else "inserted"


def _step_failed_summary():
    """Placeholder returned for an R6 write step that raised before it could
    produce its own attempted/succeeded/failed counts (item 9)."""
    return {"attempted": 0, "succeeded": 0, "failed": 0, "step_error": True}


def _enforce_tracking(client, settings, snap):
    """R5, split by V3 into two independent checks."""
    # V3(b): inventoryPolicy != DENY -> ERROR only, never autofixed.
    bad_policy = snap.loc[snap["inventoryPolicy"] != "DENY"]
    if len(bad_policy.index):
        log.error("%d variant(s) have inventoryPolicy != DENY: %s",
                 len(bad_policy.index), bad_policy["variantId"].tolist()[:20])

    # V3(a): tracked=false -> autofix if count <= TRACKING_AUTOFIX_MAX, else ERROR.
    untracked = snap.loc[~snap["tracked"]]
    count = len(untracked.index)
    if count == 0:
        return
    if count > settings.tracking_autofix_max:
        log.error("%d variant(s) have tracked=false (> TRACKING_AUTOFIX_MAX=%d); run `python main.py "
                 "enable_tracking --apply`", count, settings.tracking_autofix_max)
        return

    locked = untracked.loc[untracked["trackedEditableLocked"]]
    if len(locked.index):
        log.error("%d variant(s) have tracked=false but inventoryItem.trackedEditable.locked=true; "
                 "cannot autofix: %s", len(locked.index), locked["variantId"].tolist()[:20])

    fixable_ids = untracked.loc[~untracked["trackedEditableLocked"], "inventoryId"].tolist()
    summary = shopify_writes.set_tracked(client, settings, fixable_ids)
    log.info("tracking autofix (tracked=false -> true): %s", summary)


def _run_brakex(settings):
    """Shared by the standalone brakex_orders job and upload's final Brakex
    step (B4). Caller is responsible for checking settings.brakex_enabled and
    for its own try/except."""
    config.validate_brakex(settings)
    client = shopify_client.ShopifyClient.with_static_token(
        settings.brakex_shop, settings.shopify_api_version, settings.brakex_access_token
    )
    db = db_mod.Db(settings)
    summary = brakex_orders_mod.pull_brakex_open_orders(client, db, settings)
    log.info("brakex orders: %s=%d skipped=%d errors=%d", _inserted_label(settings),
             summary["inserted"], summary["skipped"], summary["errors"])
    return summary


def brakex_orders(force_dry_run=False):
    """B4: standalone job — python main.py brakex_orders.

    Item 5 (review fix): deliberately does NOT use _load_settings() (that
    pulls in the full RV upload validation — Amazon/Google creds,
    TXN_PREFIX == NEW_STORE_PREFIX — none of which this job touches). It only
    validates what it actually needs: MySQL (for the DB read/write) and,
    once brakex_enabled is confirmed true, the Brakex keys (via
    _run_brakex's own config.validate_brakex call)."""
    settings = config.load_settings()
    config.validate_mysql(settings)
    if force_dry_run and not settings.dry_run:
        settings = dataclasses.replace(settings, dry_run=True)
    if not settings.brakex_enabled:
        log.info("brakex_orders: BRAKEX_ENABLED=false, no-op")
        return
    _run_brakex(settings)


def upload(ignore_quiet=False, force_breaker=False, force_dry_run=False):
    settings = _load_settings(force_dry_run)

    # R14: --ignore-quiet bypasses the quiet window only.
    if not ignore_quiet and _in_quiet_window(settings):
        log.info("skip upload: within overnight quiet window %s-%s", settings.quiet_start, settings.quiet_end)
        return

    try:
        _upload_rv(settings, force_breaker)
    finally:
        # B4: Brakex order pull is upload's FINAL step, independent of RV
        # snapshot/pricing success — runs even if _upload_rv aborted early
        # (empty snapshot, an R7 guard, the circuit breaker, ...), in its own
        # try/except so a Brakex failure never fails the whole upload job
        # (old hourly job did RV then Brakex the same way, sequentially).
        if settings.brakex_enabled:
            try:
                _run_brakex(settings)
            except Exception:
                log.exception("brakex orders step failed")


def _upload_rv(settings, force_breaker):
    client = shopify_client.ShopifyClient(settings)
    db = db_mod.Db(settings)

    snap = None
    try:
        snap = snapshot_mod.fetch_snapshot(client, settings)
        log.info("snapshot: %d variants", len(snap.index))
        if snap.empty:
            # Item 9 (reviewer fix): a snapshot that "succeeded" with 0
            # variants is as unusable as a failed fetch — treat it the same
            # way (abort before any write) rather than letting an empty
            # ChangeSet silently proceed.
            log.error("snapshot returned 0 variants; treating as a failed snapshot fetch")
    except Exception:
        log.exception("snapshot fetch failed")

    if snap is not None and not snap.empty:
        try:
            _enforce_tracking(client, settings, snap)
        except Exception:
            log.exception("tracking enforcement (R5) failed")

    # Step 2 (orders) still runs even if the snapshot failed, with an empty
    # vendor lookup, per design spec sec. 11.
    order_summary = {"inserted": 0, "skipped": 0, "errors": 0}
    try:
        order_summary = orders_mod.pull_open_orders(client, db, settings, snap)
        log.info("orders: %s=%d skipped=%d errors=%d", _inserted_label(settings),
                 order_summary["inserted"], order_summary["skipped"], order_summary["errors"])
    except Exception:
        log.exception("pull_open_orders failed")

    if snap is None or snap.empty:
        log.error("skip pricing/push: snapshot step failed or returned 0 variants")
        return

    # Step 3: pricing inputs, guarded (R7).
    try:
        feed_df = sources.load_merged_feed(db)
        amazon_df = sources.load_amazon_report(settings)
        rma_df = sources.load_rma_sheet(settings)
        restricted_df = sources.load_restricted_skus(db)
        map_df = sources.load_map_list(db)
        upc_exception_df = sources.load_upc_exceptions(settings)
    except GuardError as exc:
        log.error("abort pricing/push: %s", exc)
        return
    except Exception:
        log.exception("abort pricing/push: unexpected error loading pricing inputs")
        return

    try:
        changeset = pricing.compute_changes(
            snap, feed_df, amazon_df, rma_df, restricted_df, map_df, upc_exception_df, settings
        )
    except Exception:
        log.exception("abort pricing/push: compute_changes failed")
        return

    # R12
    log.info("store UPC variants matched in feed: %d/%d", changeset.feed_match_count, changeset.feed_match_total)
    if changeset.no_upc_count:
        log.info("UPC-less variants (option b, forced to 0): %d", changeset.no_upc_count)
    if changeset.zero_price_count:
        log.warning("$0/NaN computed price guard (R13) triggered for %d row(s)", changeset.zero_price_count)

    # V5: feed-match guard, NOT bypassed by --force-breaker.
    if changeset.feed_match_total and changeset.feed_match_count < settings.feed_match_min:
        log.error("abort pricing/push: matched-in-feed count %d < FEED_MATCH_MIN %d",
                 changeset.feed_match_count, settings.feed_match_min)
        return

    # R7 circuit breaker, evaluated on the full ChangeSet before any write.
    breaker = pricing.evaluate_breaker(snap, changeset, settings)
    if breaker.tripped:
        if force_breaker:
            log.warning("circuit breaker tripped but bypassed via --force-breaker: %s", breaker.reasons)
        else:
            log.error("abort pricing/push: circuit breaker tripped (use --force-breaker to override): %s",
                     breaker.reasons)
            return

    # R6 write order: (a) inventory target 0, (b) prices, (c) vendor
    # metafields, (d) inventory target > 0, (e) google.
    inv = changeset.inventory
    inv_zero = inv.loc[inv["target_quantity"] == 0]
    inv_positive = inv.loc[inv["target_quantity"] > 0]

    # Item 9 (reviewer fix): each write step gets its own try/except so one
    # step failing doesn't block the rest of the R6 sequence.
    zero_summary = _step_failed_summary()
    try:
        zero_summary = shopify_writes.set_inventory(client, settings, inv_zero)
    except Exception:
        log.exception("R6 step (a) set_inventory target=0 failed")

    price_summary = _step_failed_summary()
    try:
        price_summary = shopify_writes.update_prices(client, changeset.prices, settings)
    except Exception:
        log.exception("R6 step (b) update_prices failed")

    vendor_summary = _step_failed_summary()
    try:
        vendor_summary = shopify_writes.set_vendorname(client, changeset.vendors, settings)
    except Exception:
        log.exception("R6 step (c) set_vendorname failed")

    positive_summary = _step_failed_summary()
    try:
        positive_summary = shopify_writes.set_inventory(client, settings, inv_positive)
    except Exception:
        log.exception("R6 step (d) set_inventory target>0 failed")

    google_summary = _step_failed_summary()
    try:
        google_summary = google_merchant.sync_prices(settings, changeset.google)
    except Exception:
        log.exception("R6 step (e) google_merchant.sync_prices failed")

    log.info(
        "summary: variants=%d no_upc=%d zero_price_guard=%d prices=%s inventory_zero=%s inventory_positive=%s "
        "vendors=%s google=%s orders_%s=%d orders_errors=%d",
        len(snap.index), changeset.no_upc_count, changeset.zero_price_count,
        price_summary, zero_summary, positive_summary, vendor_summary, google_summary,
        _inserted_label(settings), order_summary["inserted"], order_summary["errors"],
    )


def enable_tracking(apply=False, force_dry_run=False):
    settings = _load_settings(force_dry_run)
    client = shopify_client.ShopifyClient(settings)
    snap = snapshot_mod.fetch_snapshot(client, settings)

    untracked = snap.loc[~snap["tracked"]]
    log.info("enable_tracking: %d variant(s) with tracked=false", len(untracked.index))
    if not apply:
        log.info("enable_tracking: --apply not passed; counted only, no writes")
        return

    locked = untracked.loc[untracked["trackedEditableLocked"]]
    if len(locked.index):
        log.error("enable_tracking: %d variant(s) cannot be fixed (trackedEditable.locked=true): %s",
                 len(locked.index), locked["variantId"].tolist())

    fixable_ids = untracked.loc[~untracked["trackedEditableLocked"], "inventoryId"].tolist()
    summary = shopify_writes.set_tracked(client, settings, fixable_ids)
    log.info("enable_tracking: %s", summary)


def tracking_audit(force_dry_run=False):
    """R2/V2: report-only — no automatic shipping."""
    settings = _load_settings(force_dry_run)
    client = shopify_client.ShopifyClient(settings)
    db = db_mod.Db(settings)
    conn = db.connect()
    try:
        rows, _columns = db_mod.run_query(
            conn,
            "select PO_Number, Vendor from ds_tracking where PO_Number like %s and Create_Date < curdate();",
            (settings.txn_prefix + "%",),
        )
        by_txn = {}
        for po_number, vendor in rows:
            by_txn.setdefault(po_number, []).append(vendor)

        flagged = 0
        for txn_id, vendors in by_txn.items():
            order_rows, _c = db_mod.run_query(
                conn, "select distinct shopifyOrderId from {} where TxnID = %s;".format(settings.order_table),
                (txn_id,),
            )
            if not order_rows:
                log.warning("tracking_audit: TxnID=%s in ds_tracking but not found in %s", txn_id, settings.order_table)
                continue
            shopify_order_id = str(order_rows[0][0])

            query = """query($id: ID!) {
                order(id: $id) {
                    fulfillmentOrders(first: 10) {
                        edges { node { lineItems(first: 50) { edges { node { sku remainingQuantity } } } } }
                    }
                }
            }"""
            try:
                data = client.graphql(query, {"id": "gid://shopify/Order/{}".format(shopify_order_id)})
            except ShopifyError:
                log.exception("tracking_audit: failed fetching fulfillment orders for TxnID=%s", txn_id)
                continue

            order = data.get("order")
            if not order:
                continue
            remaining_skus = []
            for edge in order["fulfillmentOrders"]["edges"]:
                for li_edge in edge["node"]["lineItems"]["edges"]:
                    node = li_edge["node"]
                    if node["remainingQuantity"] > 0:
                        remaining_skus.append(node["sku"])
            if remaining_skus:
                flagged += 1
                log.error("tracking_audit: TxnID=%s has a ds_tracking row (vendors=%s) but still has "
                         "unshipped sku(s)=%s", txn_id, vendors, remaining_skus)

        log.info("tracking_audit: checked %d TxnID(s), flagged %d", len(by_txn), flagged)
    finally:
        conn.close()


def delete_disapproved(force_dry_run=False):
    settings = _load_settings(force_dry_run)
    if not settings.google_merchant_enabled:
        log.info("delete_disapproved: GOOGLE_MERCHANT_ENABLED=false, no-op")
        return
    db = db_mod.Db(settings)
    google_merchant.delete_all_disapproved_items(settings, db, settings.google_merchant_id)


JOBS = {
    "upload": upload,
    "enable_tracking": enable_tracking,
    "tracking_audit": tracking_audit,
    "delete_disapproved": delete_disapproved,
    "brakex_orders": brakex_orders,
}


def main():
    parser = argparse.ArgumentParser(description="rvpartstore jobs")
    parser.add_argument("job", choices=sorted(JOBS))
    parser.add_argument("--dry-run", action="store_true", help="force DRY_RUN for this run")
    parser.add_argument("--apply", action="store_true", help="enable_tracking: actually write (default: count only)")
    parser.add_argument("--ignore-quiet", action="store_true", help="upload: bypass the overnight quiet window")
    parser.add_argument("--force-breaker", action="store_true", help="upload: bypass the R7 circuit breaker")
    args = parser.parse_args()

    kwargs = {"force_dry_run": args.dry_run}
    if args.job == "upload":
        kwargs["ignore_quiet"] = args.ignore_quiet
        kwargs["force_breaker"] = args.force_breaker
    elif args.job == "enable_tracking":
        kwargs["apply"] = args.apply

    settings = config.load_settings()
    job_runner.run(settings.log_name, lambda: JOBS[args.job](**kwargs), label=args.job)


if __name__ == "__main__":
    main()

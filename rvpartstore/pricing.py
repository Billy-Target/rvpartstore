"""Pricing/inventory/vendor computation — a line-by-line PORT of francis-shopify's
update_price_quantity_graphql (lines ~614-1044 of update_price_inventory.py),
NOT a rewrite. Pure: no I/O (P7), no DB/API calls; everything comes in as
DataFrame parameters and Settings.

Deviations from the old algorithm are limited to the ones the design spec
calls out explicitly (P1-P7, R8, R9, R13, V6) and are each marked with a
comment naming the spec id they implement. Everything else — every vendor
shipping/markup formula, the RMA handling, the amazon repricer filtering, the
MAP floor, the lowest-vendor selection cascade — is transcribed as-is,
including a couple of pre-existing quirks in the old code that are NOT in the
deviation list (called out below where they matter) and are therefore kept
verbatim rather than "fixed".
"""

import logging
from collections import namedtuple

import numpy
import pandas

log = logging.getLogger(__name__)

DISCOUNT_RATE = 0.978

_VENDOR_COST_COLS = [
    "keystoneCost", "motorstateCost", "meyerCost", "turnfourteenCost", "premierCost",
    "cbkCost", "rma price", "partsauthorityCost", "cwrCost",
]
_VENDOR_QTY_COLS = [
    "rma quantity", "keystoneQuantity", "motorstateQuantity", "meyerQuantity",
    "turnfourteenQuantity", "premierQuantity", "cbkQuantity", "partsauthorityQuantity", "cwrQuantity",
]
_RESTRICTED_ZERO_COLS = [
    "keystoneQuantity", "motorstateQuantity", "meyerQuantity", "turnfourteenQuantity",
    "cbkQuantity", "premierQuantity", "partsauthorityQuantity", "rma quantity", "cwrQuantity",
]

_FEED_SOURCE_COLS = [
    "keystonePartNumber", "meyerPartNumber", "motorstatePartNumber", "turnfourteenPartNumber",
    "cbkPartNumber", "premierPartNumber", "partsauthorityPartNumber", "UPC", "keystoneCost",
    "motorstateCost", "meyerCost", "turnfourteenCost", "cbkCost", "premierCost",
    "partsauthorityCost", "keystoneQuantity", "motorstateQuantity", "meyerQuantity", "turnfourteenQuantity",
    "cbkQuantity", "premierQuantity", "partsauthorityQuantity", "PurolatorGroundAssessorials",
    "motorstateLTL", "cbkLTL", "keystoneSpecialHandlingCharge",
    "motorstateSpecialHandlingCharge", "meyerSpecialHandlingCharge",
    "turnfourteenSpecialHandlingCharge", "cbkSpecialHandlingCharge",
    "premierSpecialHandlingCharge", "partsauthoritySpecialHandlingCharge", "RealWeight", "keystoneLTL",
    "rma price", "rma quantity", "cwrPartNumber", "cwrCost", "cwrQuantity",
    "cwrSpecialHandlingCharge",
]


ChangeSet = namedtuple("ChangeSet", [
    "prices",            # productId, variantId, new_price, compareAtPrice
    "inventory",         # variantId, inventoryId, current_quantity, target_quantity
    "vendors",           # productId, lowest_vendor
    "google",            # google_merchant_id, sku, upc, new_price, compareAtPrice, stock, variantId
    "no_upc_count",      # int (P2)
    "zero_price_count",  # int (R13)
    "feed_match_count",  # int (R12/V5) — store UPC variants that matched today's feed
    "feed_match_total",  # int (R12/V5) — total store UPC-bearing variants
])

BreakerResult = namedtuple("BreakerResult", ["zero_out_pct", "big_move_pct", "tripped", "reasons"])


def _bucket_target(total_quantity):
    """P3: kept old bucket mapping of total_quantity -> target (0 / 1 if >0 /
    4 if >3 / 6 if >5)."""
    if total_quantity > 5:
        return 6
    if total_quantity > 3:
        return 4
    if total_quantity > 0:
        return 1
    return 0


def _apply_map_floor(df, map_list):
    """Port of the old module-level df_change_map_price()."""
    df = df.drop(columns=["map_price"], errors="ignore")
    df["old_upc"] = df["upc"]
    df["upc"] = df["upc"].astype(numpy.int64).astype(str)

    map_list = map_list.loc[map_list["upc"].astype(str).str.isdigit()].copy()
    map_list["upc"] = map_list["upc"].astype(numpy.int64).astype(str)

    df = pandas.merge(df, map_list, how="left", on="upc")
    df["map_price"] = df["map_price"].fillna(0)
    df["new_price"] = df[["map_price", "new_price"]].max(axis=1)
    df["upc"] = df["old_upc"]
    df = df.drop(columns=["old_upc"])
    return df


def _get_changed_items(df, google_enabled):
    """Port of the old get_changed_items(), modified per P3 (exact
    target-vs-current quantity compare instead of bucket-vs-bucket) and V6
    (also emit a price change when target compareAtPrice differs from the
    store's, NULL counting as different)."""
    df = df.copy()
    df["target_quantity"] = df["total_quantity"].apply(_bucket_target)

    price_mask = (abs((df["new_price"] - df["price"]) / df["price"])) > 0.01

    # V6: compareAtPrice-driven price change too. Guarded by feed_matched (if
    # present — the RMA path doesn't carry this column and is always
    # considered matched, R8 already filtered it to matched variants): rows
    # with NO vendor-feed match at all have new_price/compareAtPrice that are
    # pure store-price pass-through (no real data), so a NULL->non-null
    # compareAtPrice there is not a genuine change. Without this guard, R9's
    # "price: UPC path wins" would let this spurious UPC-side "change" clobber
    # a real RMA-sourced price for RMA-only (not-in-vendor-feed) variants —
    # found via smoke-testing, not an explicit spec id.
    # Once filled, compareAtPrice only counts as changed beyond the same 1%
    # threshold as price (an absolute 0.01 re-wrote hundreds of variants every
    # hour on tiny Amazon-repricer drift — seen on the first live run).
    feed_matched = df["feed_matched"] if "feed_matched" in df.columns else True
    cap_old = df["store_compare_at_price"]
    cap_new = df["compareAtPrice"]
    cap_differs = feed_matched & (
        (cap_old.isna() != cap_new.isna())
        | ((~cap_old.isna()) & (~cap_new.isna()) & (((cap_old - cap_new).abs() / cap_old.abs()) > 0.01))
    )

    price_df = df.loc[price_mask | cap_differs].copy()

    # P3: exact compare (old code compared buckets, which missed same-bucket
    # quantity drift, e.g. current=2 vs target=1 — both "low stock").
    qty_changed = df["target_quantity"] != df["quantity"]
    inventory_df = df.loc[qty_changed].copy()

    metafield_df = df.loc[
        (df["metafieldValue"] != df["lowest_vendor"])
        & (df["lowest_vendor"] != "NO VENDOR")
        & (~df["lowest_vendor"].isna())
    ].copy()

    if google_enabled:
        # P6: google list only built when enabled.
        google_df = df.loc[price_mask | cap_differs | qty_changed].copy()
        google_df.loc[~price_mask, "new_price"] = google_df.loc[~price_mask, "price"]
    else:
        google_df = df.iloc[0:0].copy()

    return (
        price_df.drop_duplicates().reset_index(drop=True),
        inventory_df.drop_duplicates().reset_index(drop=True),
        metafield_df.drop_duplicates().reset_index(drop=True),
        google_df.drop_duplicates().reset_index(drop=True),
    )


def _apply_vendor_shipping(upc_items):
    """Vendor shipping/markup formulas — kept verbatim (P1)."""
    # cwr
    upc_items["cwrShipping"] = upc_items["RealWeight"] * 0.4 + 23 + upc_items["cwrSpecialHandlingCharge"]
    upc_items.loc[upc_items["RealWeight"] == 0, "cwrShipping"] = upc_items["cwrShipping"] + 50
    upc_items.loc[upc_items["cwrShipping"] > 300, "cwrShipping"] = 300
    upc_items.loc[upc_items["cwrShipping"].isna(), "cwrShipping"] = 300
    upc_items["cwrCost"] = upc_items["cwrCost"] + upc_items["cwrShipping"]
    upc_items["cwrCost1"] = upc_items["cwrCost"] * 1.15
    upc_items["cwrCost2"] = upc_items["cwrCost"] + 7
    upc_items["cwrCost"] = upc_items[["cwrCost1", "cwrCost2"]].max(axis=1)

    # keystone
    upc_items["PurolatorGroundAssessorials"] = upc_items["PurolatorGroundAssessorials"].fillna(0)
    upc_items["keystoneShipping"] = upc_items["PurolatorGroundAssessorials"] + 11.49
    upc_items.loc[upc_items["keystoneLTL"] == 1, "keystoneShipping"] = 317.96
    upc_items["keystoneCost"] = upc_items["keystoneCost"] + upc_items["keystoneShipping"]
    upc_items["keystoneCost1"] = upc_items["keystoneCost"] * 1.15
    upc_items["keystoneCost2"] = upc_items["keystoneCost"] + 7
    upc_items["keystoneCost"] = upc_items[["keystoneCost1", "keystoneCost2"]].max(axis=1)

    # motorstate
    upc_items["motorstateShipping"] = upc_items["RealWeight"] / 2 + 13
    upc_items.loc[upc_items["RealWeight"] > 10000, "motorstateShipping"] = 50
    upc_items.loc[upc_items["motorstateLTL"] == 1, "motorstateShipping"] = 100 + upc_items["motorstateShipping"]
    upc_items["motorstateCost"] = upc_items["motorstateCost"] + upc_items["motorstateShipping"]
    upc_items["motorstateCost1"] = upc_items["motorstateCost"] * 1.15
    upc_items["motorstateCost2"] = upc_items["motorstateCost"] + 7
    upc_items["motorstateCost"] = upc_items[["motorstateCost1", "motorstateCost2"]].max(axis=1)

    # meyer
    upc_items["meyerShipping"] = upc_items["RealWeight"] * 0.4 + 13 + upc_items["meyerSpecialHandlingCharge"]
    upc_items.loc[upc_items["RealWeight"] == 0, "meyerShipping"] = 60 + upc_items["meyerShipping"]
    upc_items.loc[upc_items["meyerShipping"] > 250, "meyerShipping"] = 250
    upc_items["meyerCost"] = upc_items["meyerCost"] + upc_items["meyerShipping"]
    upc_items["meyerCost1"] = upc_items["meyerCost"] * 1.15
    upc_items["meyerCost2"] = upc_items["meyerCost"] + 7
    upc_items["meyerCost"] = upc_items[["meyerCost1", "meyerCost2"]].max(axis=1)

    # premier
    upc_items["premierShipping"] = upc_items["RealWeight"] * 0.4 + 20 + upc_items["premierSpecialHandlingCharge"]
    upc_items.loc[upc_items["RealWeight"] == 0, "premierShipping"] = 80 + upc_items["premierShipping"]
    upc_items.loc[upc_items["premierShipping"] > 300, "premierShipping"] = 300
    upc_items["premierCost"] = upc_items["premierCost"] + upc_items["premierShipping"]
    upc_items["premierCost1"] = upc_items["premierCost"] * 1.15
    upc_items["premierCost2"] = upc_items["premierCost"] + 7
    upc_items["premierCost"] = upc_items[["premierCost1", "premierCost2"]].max(axis=1)

    # turnfourteen
    upc_items["turnfourteenShipping"] = upc_items["RealWeight"] * 0.4 + 15 + upc_items["turnfourteenSpecialHandlingCharge"]
    upc_items.loc[upc_items["RealWeight"] == 0, "turnfourteenShipping"] = 50 + upc_items["turnfourteenShipping"]
    upc_items.loc[upc_items["turnfourteenShipping"] > 300, "turnfourteenShipping"] = 300
    upc_items["turnfourteenCost"] = upc_items["turnfourteenCost"] + upc_items["turnfourteenShipping"]
    upc_items["turnfourteenCost1"] = upc_items["turnfourteenCost"] * 1.15
    upc_items["turnfourteenCost2"] = upc_items["turnfourteenCost"] + 7
    upc_items["turnfourteenCost"] = upc_items[["turnfourteenCost1", "turnfourteenCost2"]].max(axis=1)

    # cbk
    upc_items["cbkShipping"] = upc_items["RealWeight"] * 0.4 + 13 + upc_items["cbkSpecialHandlingCharge"]
    upc_items.loc[upc_items["RealWeight"] == 0, "cbkShipping"] = 60 + upc_items["cbkShipping"]
    upc_items.loc[upc_items["cbkShipping"] > 250, "cbkShipping"] = 250
    upc_items["cbkCost"] = upc_items["cbkCost"] + upc_items["cbkShipping"]
    upc_items["cbkCost1"] = upc_items["cbkCost"] * 1.15
    upc_items["cbkCost2"] = upc_items["cbkCost"] + 7
    upc_items["cbkCost"] = upc_items[["cbkCost1", "cbkCost2"]].max(axis=1)

    # partsauthority
    upc_items["partsauthorityShipping"] = upc_items["RealWeight"] * 0.4 + 35 + upc_items["partsauthoritySpecialHandlingCharge"]
    upc_items.loc[upc_items["RealWeight"] == 0, "partsauthorityShipping"] = 50 + upc_items["partsauthorityShipping"]
    upc_items.loc[upc_items["partsauthorityShipping"] > 300, "partsauthorityShipping"] = 300
    upc_items["partsauthorityCost"] = upc_items["partsauthorityCost"] + upc_items["partsauthorityShipping"]
    upc_items["partsauthorityCost1"] = upc_items["partsauthorityCost"] * 1.15
    upc_items["partsauthorityCost2"] = upc_items["partsauthorityCost"] + 7
    upc_items["partsauthorityCost"] = upc_items[["partsauthorityCost1", "partsauthorityCost2"]].max(axis=1)

    return upc_items


def _select_lowest_vendor(upc_items):
    """RMA stock-first + lowest-vendor selection cascade — kept verbatim,
    including the exact tie-break order (later .loc wins on ties)."""
    upc_items["rma quantity"] = upc_items["rma quantity"].fillna(0)
    upc_items["rma quantity"] = upc_items["rma quantity"].astype(int)
    upc_items.loc[upc_items["rma price"].isna(), "rma price"] = upc_items[
        ["keystoneCost", "motorstateCost", "meyerCost", "turnfourteenCost", "premierCost", "cbkCost",
         "partsauthorityCost", "cwrCost"]
    ].min(axis=1)

    upc_items["highestPrice"] = upc_items[_VENDOR_COST_COLS].max(axis=1)
    upc_items["all_vendor_total_quantity"] = upc_items[_VENDOR_QTY_COLS].sum(axis=1)

    # 0-quantity vendors are repriced to the highest vendor price so they
    # never win the lowest-price selection below.
    upc_items.loc[upc_items["keystoneQuantity"] == 0, "keystoneCost"] = upc_items["highestPrice"]
    upc_items.loc[upc_items["motorstateQuantity"] == 0, "motorstateCost"] = upc_items["highestPrice"]
    upc_items.loc[upc_items["meyerQuantity"] == 0, "meyerCost"] = upc_items["highestPrice"]
    upc_items.loc[upc_items["turnfourteenQuantity"] == 0, "turnfourteenCost"] = upc_items["highestPrice"]
    upc_items.loc[upc_items["premierQuantity"] == 0, "premierCost"] = upc_items["highestPrice"]
    upc_items.loc[upc_items["cbkQuantity"] == 0, "cbkCost"] = upc_items["highestPrice"]
    upc_items.loc[upc_items["partsauthorityQuantity"] == 0, "partsauthorityCost"] = upc_items["highestPrice"]
    upc_items.loc[upc_items["rma quantity"] == 0, "rma price"] = upc_items["highestPrice"]
    upc_items.loc[upc_items["cwrQuantity"] == 0, "cwrCost"] = upc_items["highestPrice"]

    upc_items["lowestPrice"] = upc_items[_VENDOR_COST_COLS].min(axis=1)

    # Item 5 (empty-feed guard): pre-create lowest_vendor/total_quantity as
    # real columns before the first `.loc[mask, col] = scalar` assignment
    # below. On a genuinely empty (0-row) upc_items, assigning a BRAND NEW
    # column via a boolean-mask .loc raises pandas' "cannot set a frame with
    # no defined index and a scalar" — pre-creating the column first (as here)
    # turns every subsequent assignment into an existing-column update, which
    # pandas handles fine even with 0 rows.
    upc_items["lowest_vendor"] = None
    upc_items["total_quantity"] = None

    upc_items.loc[upc_items["lowestPrice"] == upc_items["rma price"], "lowest_vendor"] = "IB"
    upc_items.loc[upc_items["lowestPrice"] == upc_items["premierCost"], "lowest_vendor"] = "PR"
    upc_items.loc[upc_items["lowestPrice"] == upc_items["turnfourteenCost"], "lowest_vendor"] = "TF"
    upc_items.loc[upc_items["lowestPrice"] == upc_items["meyerCost"], "lowest_vendor"] = "ME"
    upc_items.loc[upc_items["lowestPrice"] == upc_items["cbkCost"], "lowest_vendor"] = "CBK"
    upc_items.loc[upc_items["lowestPrice"] == upc_items["motorstateCost"], "lowest_vendor"] = "MS"
    upc_items.loc[upc_items["lowestPrice"] == upc_items["keystoneCost"], "lowest_vendor"] = "KS"
    upc_items.loc[upc_items["lowestPrice"] == upc_items["partsauthorityCost"], "lowest_vendor"] = "PA"
    upc_items.loc[upc_items["lowestPrice"] == upc_items["cwrCost"], "lowest_vendor"] = "CW"
    upc_items.loc[upc_items["all_vendor_total_quantity"] == 0, "lowest_vendor"] = "NO VENDOR"

    # if RMA has stock, always sell RMA stock first
    upc_items.loc[upc_items["rma quantity"] > 0, "lowestPrice"] = upc_items["rma price"]
    upc_items.loc[upc_items["rma quantity"] > 0, "lowest_vendor"] = "IB"

    upc_items.loc[upc_items["lowest_vendor"] == "IB", "total_quantity"] = upc_items["rma quantity"]
    upc_items.loc[upc_items["lowest_vendor"] == "KS", "total_quantity"] = upc_items["keystoneQuantity"]
    upc_items.loc[upc_items["lowest_vendor"] == "MS", "total_quantity"] = upc_items["motorstateQuantity"]
    upc_items.loc[upc_items["lowest_vendor"] == "ME", "total_quantity"] = upc_items["meyerQuantity"]
    upc_items.loc[upc_items["lowest_vendor"] == "TF", "total_quantity"] = upc_items["turnfourteenQuantity"]
    upc_items.loc[upc_items["lowest_vendor"] == "PR", "total_quantity"] = upc_items["premierQuantity"]
    upc_items.loc[upc_items["lowest_vendor"] == "CBK", "total_quantity"] = upc_items["cbkQuantity"]
    upc_items.loc[upc_items["lowest_vendor"] == "PA", "total_quantity"] = upc_items["partsauthorityQuantity"]
    upc_items.loc[upc_items["lowest_vendor"] == "CW", "total_quantity"] = upc_items["cwrQuantity"]
    upc_items.loc[upc_items["lowest_vendor"] == "NO VENDOR", "total_quantity"] = 0

    return upc_items


def _filter_amazon_repricer_pass1(amazon_df):
    """Old lines 631-633: first filter pass on the raw report."""
    repricer = amazon_df.copy()
    repricer = repricer.loc[~repricer["price"].isna()]
    repricer = repricer.loc[~repricer["quantity"].isna()]
    repricer = repricer.loc[~repricer["seller-sku"].str.contains("FBA")]
    return repricer


def _filter_amazon_repricer_pass2(repricer):
    """Old lines 920-941: further filters the ALREADY-once-filtered repricer
    frame (same object, mutated in place in the old code) down to the U-item
    lowest listed price per upc."""
    repricer = repricer.copy()
    repricer = repricer.loc[~repricer["price"].isna()]
    repricer = repricer.loc[~repricer["quantity"].isna()]
    repricer = repricer.loc[repricer["status"] == "Active"]
    repricer = repricer.loc[~repricer["seller-sku"].str.contains("FBA")]
    repricer["sku"] = repricer["seller-sku"]
    repricer["my_price"] = repricer["price"]
    repricer["first_two"] = repricer["sku"].str.slice(start=0, stop=2)
    repricer = repricer.loc[repricer["first_two"].isin(["AU", "MP", "PS"])]
    repricer["upc"] = repricer["sku"].str.split("-").str[2]

    counts = repricer.groupby(["upc"])["quantity"].agg("sum").reset_index()
    counts.columns = ["upc", "sum"]
    repricer = pandas.merge(counts, repricer, on="upc")
    repricer["remove"] = False
    repricer.loc[(repricer["quantity"] == 0) & (repricer["sum"] != 0), "remove"] = True
    repricer = repricer.loc[~repricer["remove"]]
    repricer = repricer[["upc", "my_price"]]
    repricer = repricer.sort_values(by=["upc", "my_price"])
    repricer["my_price"] = repricer["my_price"] * DISCOUNT_RATE
    repricer = repricer.sort_values(by=["my_price"])
    repricer = repricer.drop_duplicates(subset="upc", keep="first")
    return repricer


def compute_changes(in_store_df, feed_df, amazon_df, rma_sheet_df, restricted_df, map_df,
                    upc_exception_df, settings):
    # P2: UPC-less split (option b) happens before anything else. This also
    # sidesteps the old `.astype('int64')` crash on NULL UPC.
    upc_mask = in_store_df["upc"].notna() & in_store_df["upc"].astype(str).str.isdigit()
    store_df = in_store_df.loc[upc_mask].copy().reset_index(drop=True)
    no_upc_df = in_store_df.loc[~upc_mask].copy().reset_index(drop=True)
    no_upc_count = int(len(no_upc_df))

    no_upc_zero = no_upc_df.loc[(no_upc_df["quantity"] != 0) & (no_upc_df["stocked"])]
    no_upc_inventory = pandas.DataFrame({
        "variantId": no_upc_zero["variantId"].values,
        "inventoryId": no_upc_zero["inventoryId"].values,
        "current_quantity": no_upc_zero["quantity"].values,
        "target_quantity": 0,
        "stocked": True,
        "tracked": no_upc_zero["tracked"].values,
    })

    # ---- amazon report (old 626-640) ----
    repricer_pass1 = _filter_amazon_repricer_pass1(amazon_df)
    ib_file = repricer_pass1[["seller-sku", "price"]].copy()
    ib_file.columns = ["sku", "my_price"]
    ib_file["second_two"] = ib_file["sku"].str.slice(start=3, stop=5)
    ib_file = ib_file.loc[ib_file["second_two"] == "IB"]
    ib_file["int_UPC"] = ib_file["sku"].str.slice(start=6).astype("int64")

    # ---- RMA google sheet (old 643-651) ----
    sheet_df = rma_sheet_df.copy()
    sheet_df = sheet_df.loc[sheet_df["UPC"] != ""]
    sheet_df = sheet_df.loc[sheet_df["quantity on hand"] != ""]
    sheet_df["quantity on hand"] = sheet_df["quantity on hand"].astype(int)
    sheet_df = sheet_df.loc[sheet_df["UPC"].str.isdigit()]
    sheet_df["int_UPC"] = sheet_df["UPC"].astype("int64")
    sheet_df = pandas.merge(ib_file, sheet_df, how="right", on="int_UPC")
    sheet_df = sheet_df[["my_price", "int_UPC", "UPC", "quantity on hand", "Cost CAD @"]]
    sheet_df.columns = ["rma price", "int_UPC", "rma UPC", "rma quantity", "Cost CAD @"]

    # ---- separate RMA-only items from the feed (old 654-661) ----
    feed = feed_df.copy()
    feed = feed.loc[feed["UPC"].str.isdigit()]
    feed = feed.loc[feed["UPC"] != ""]
    feed["int_UPC"] = feed["UPC"].astype("int64")
    feed = pandas.merge(feed, sheet_df, how="outer", indicator=True, on="int_UPC")
    rma_only = feed.loc[feed["_merge"] == "right_only"].drop(columns=["_merge"])
    feed = feed.loc[feed["_merge"] != "right_only"].drop(columns=["_merge"])

    # ---- restricted skus -> zero quantities (old 663-685) ----
    restricted = restricted_df.copy()
    restricted["SKU"] = restricted["SKU"].str.split("-").str[-1].astype("int64")
    restricted.columns = ["int_UPC"]
    feed = pandas.merge(feed, restricted, how="left", indicator=True, on="int_UPC")
    feed.loc[feed["_merge"] == "both", _RESTRICTED_ZERO_COLS] = 0
    feed = feed.drop(columns=["_merge"])
    rma_only = pandas.merge(rma_only, restricted, how="left", indicator=True, on="int_UPC")
    rma_only.loc[rma_only["_merge"] == "both", _RESTRICTED_ZERO_COLS] = 0
    rma_only = rma_only.drop(columns=["_merge"])

    # ---- default RMA-only price/qty (old 687-691) ----
    rma_only.loc[rma_only["rma price"].isna(), "rma quantity"] = 0
    rma_only.loc[rma_only["rma price"] == 0, "rma quantity"] = 2000 * DISCOUNT_RATE
    rma_only.loc[rma_only["rma price"].isna(), "rma price"] = 3 * rma_only["Cost CAD @"].astype("float")
    rma_only["rma price"] = rma_only["rma price"].fillna(2000).astype("float") * DISCOUNT_RATE

    # ---- P1: in-store data comes from the store_df parameter, no price_modifier ----
    # (store_df already has price / compareAtPrice / quantity / metafieldValue from snapshot.py)

    # ---- assemble upc_items (old 724-745) ----
    upc_items = feed[_FEED_SOURCE_COLS].copy()
    upc_items = upc_items.rename(columns={"UPC": "upc"})
    upc_items["cbkQuantity"] = 0
    upc_items["premierQuantity"] = 0

    upc_items = _apply_vendor_shipping(upc_items)
    upc_items = _select_lowest_vendor(upc_items)

    # ---- merge into store rows (old 908-910) ----
    merged_upc_items = pandas.merge(store_df, upc_items, how="left", on="upc")
    feed_match_total = int(len(store_df))
    merged_upc_items["feed_matched"] = merged_upc_items["total_quantity"].notna()
    feed_match_count = int(merged_upc_items["feed_matched"].sum())
    merged_upc_items["total_quantity"] = merged_upc_items["total_quantity"].fillna(0)
    merged_upc_items["lowest_vendor"] = merged_upc_items["lowest_vendor"].fillna("NO VENDOR")
    # V6: keep the store's ORIGINAL compareAtPrice before it gets overwritten
    # with the new target below, so _get_changed_items can compare the two.
    merged_upc_items["store_compare_at_price"] = merged_upc_items["compareAtPrice"]

    # ---- amazon repricer 2nd pass + U-item price lookup (old 920-941) ----
    repricer2 = _filter_amazon_repricer_pass2(repricer_pass1)

    # P1: map_df / upc_exception_df come directly from parameters (no xlsx read).

    # ---- new_price / compareAtPrice targets (old 954-980) ----
    merged_upc_items = pandas.merge(merged_upc_items, repricer2, how="left", indicator=True, on="upc")
    merged_upc_items["new_price"] = merged_upc_items["lowestPrice"]
    merged_upc_items.loc[merged_upc_items["_merge"] == "both", "new_price"] = merged_upc_items["my_price"]
    merged_upc_items.loc[merged_upc_items["new_price"].isna(), "new_price"] = merged_upc_items["price"]
    merged_upc_items = _apply_map_floor(merged_upc_items, map_df)

    merged_upc_items["int_upc"] = merged_upc_items["upc"].astype("int64")
    upc_exception = upc_exception_df.copy()
    upc_exception["UPC"] = upc_exception["UPC"].astype("int64")
    excepted = set(upc_exception["UPC"].tolist())
    merged_upc_items = merged_upc_items.loc[~merged_upc_items["int_upc"].isin(excepted)]
    rma_only = rma_only.loc[~rma_only["int_UPC"].isin(excepted)]

    merged_upc_items = merged_upc_items.reset_index(drop=True)
    merged_upc_items["compareAtPrice"] = merged_upc_items["new_price"]
    merged_upc_items.loc[merged_upc_items["_merge"] == "both", "compareAtPrice"] = (
        merged_upc_items["my_price"] / DISCOUNT_RATE
    )

    # R13: $0 guard — NaN or <=0 computed price: no price change emitted
    # (achieved by snapping new_price back to the store's current price, which
    # makes _get_changed_items' price filter false) and inventory forced to 0.
    zero_price_mask = merged_upc_items["new_price"].isna() | (merged_upc_items["new_price"] <= 0)
    zero_price_count = int(zero_price_mask.sum())
    merged_upc_items.loc[zero_price_mask, "new_price"] = merged_upc_items.loc[zero_price_mask, "price"]
    merged_upc_items.loc[zero_price_mask, "compareAtPrice"] = merged_upc_items.loc[zero_price_mask, "store_compare_at_price"]
    merged_upc_items.loc[zero_price_mask, "total_quantity"] = 0

    out_upc_price_df, out_upc_inventory_df, out_upc_metafield_df, out_upc_google_df = _get_changed_items(
        merged_upc_items, settings.google_merchant_enabled
    )
    out_upc_price_df = out_upc_price_df[
        ["productId", "variantId", "new_price", "compareAtPrice"]
    ].drop_duplicates().reset_index(drop=True)
    out_upc_inventory_df = out_upc_inventory_df[
        ["variantId", "inventoryId", "quantity", "target_quantity", "stocked", "tracked"]
    ].drop_duplicates().reset_index(drop=True)
    out_upc_metafield_df = out_upc_metafield_df[
        ["productId", "lowest_vendor"]
    ].drop_duplicates().reset_index(drop=True)
    out_upc_google_df = _finish_google_df(out_upc_google_df, settings)

    # ---- RMA path (old 997-1028) ----
    store_for_rma = store_df.copy()
    store_for_rma["int_UPC"] = store_for_rma["upc"].astype("int64")
    rma_only_cols = rma_only[["int_UPC", "rma price", "rma UPC", "rma quantity"]].copy()
    repricer2 = repricer2.loc[repricer2["upc"].str.isdigit()]
    repricer2["int_UPC"] = repricer2["upc"].astype("int64")

    merged_rma = pandas.merge(rma_only_cols, store_for_rma, how="left", on="int_UPC")
    # R8: explicit "matched a store variant" test, replacing the old
    # `~compareAtPrice.isna()` (compareAtPrice is NULL on every variant on the
    # new store, which would have dropped every RMA row).
    merged_rma = merged_rma.loc[~merged_rma["variantId"].isna()]
    # Item 4 (reviewer fix): merge on BOTH upc and int_UPC, matching the old
    # line 1006's implicit merge on the common columns (upc + int_UPC) between
    # merged_rma and repricer_file at that point in the old code.
    merged_rma = pandas.merge(
        merged_rma, repricer2[["upc", "int_UPC", "my_price"]], how="left", on=["upc", "int_UPC"]
    )
    # V6: capture the store's ORIGINAL compareAtPrice before it's overwritten
    # with the new target below (same treatment as the UPC path above).
    merged_rma["store_compare_at_price"] = merged_rma["compareAtPrice"]
    merged_rma["new_price"] = merged_rma["my_price"]
    # Note (verbatim quirk, not a spec deviation): total_quantity is captured
    # from 'rma quantity' BEFORE the isna-price zeroing below, exactly as the
    # old code did — it does not reflect the zeroing.
    merged_rma["total_quantity"] = merged_rma["rma quantity"].copy()
    merged_rma.loc[merged_rma["new_price"].isna(), "rma quantity"] = 0
    merged_rma.loc[merged_rma["new_price"].isna(), "new_price"] = merged_rma["rma price"]
    merged_rma["lowest_vendor"] = "IB"
    merged_rma["compareAtPrice"] = merged_rma["new_price"] / DISCOUNT_RATE
    merged_rma["compareAtPrice"] = merged_rma["compareAtPrice"].astype("float").round(2)

    # R13 again, same rule, for the RMA path.
    zero_price_mask_rma = merged_rma["new_price"].isna() | (merged_rma["new_price"] <= 0)
    zero_price_count += int(zero_price_mask_rma.sum())
    merged_rma.loc[zero_price_mask_rma, "new_price"] = merged_rma.loc[zero_price_mask_rma, "price"]
    merged_rma.loc[zero_price_mask_rma, "compareAtPrice"] = merged_rma.loc[zero_price_mask_rma, "store_compare_at_price"]
    merged_rma.loc[zero_price_mask_rma, "total_quantity"] = 0

    out_rma_price_df, out_rma_inventory_df, out_rma_metafield_df, out_rma_google_df = _get_changed_items(
        merged_rma, settings.google_merchant_enabled
    )
    out_rma_price_df = out_rma_price_df[
        ["productId", "variantId", "new_price", "compareAtPrice"]
    ].drop_duplicates().reset_index(drop=True)
    out_rma_inventory_df = out_rma_inventory_df[
        ["variantId", "inventoryId", "quantity", "target_quantity", "stocked", "tracked"]
    ].drop_duplicates().reset_index(drop=True)
    out_rma_metafield_df = out_rma_metafield_df[
        ["productId", "lowest_vendor"]
    ].drop_duplicates().reset_index(drop=True)
    out_rma_google_df = _finish_google_df(out_rma_google_df, settings)

    # Item 3 (reviewer fix — RMA/UPC inventory oscillation): a variant that
    # exists in merged_rma at all (i.e. the RMA path matched a store variant,
    # R8) must NEVER get an inventory change from the UPC path, even for rows
    # where the RMA path itself didn't end up emitting a change. Without this,
    # a variant whose RMA-sheet quantity differs from its current Shopify
    # quantity but whose UPC-path match is "not in vendor feed -> target 0"
    # would flip 0 on one run (UPC path fires, RMA path doesn't touch
    # inventory that run) and back on the next (vice versa) — an oscillation,
    # not tied to a single R/V id, found via testing.
    rma_matched_variant_ids = set(merged_rma["variantId"].dropna().tolist())
    out_upc_inventory_df = out_upc_inventory_df.loc[
        ~out_upc_inventory_df["variantId"].isin(rma_matched_variant_ids)
    ].reset_index(drop=True)

    # R9: RMA-vs-UPC precedence via explicit dedupe (not write order).
    # price & vendor: UPC wins. inventory: RMA wins.
    prices_df = pandas.concat([out_rma_price_df, out_upc_price_df], ignore_index=True)
    prices_df = prices_df.drop_duplicates(subset="variantId", keep="last").reset_index(drop=True)

    vendors_df = pandas.concat([out_rma_metafield_df, out_upc_metafield_df], ignore_index=True)
    vendors_df = vendors_df.drop_duplicates(subset="productId", keep="last").reset_index(drop=True)

    inventory_df = pandas.concat([out_upc_inventory_df, out_rma_inventory_df], ignore_index=True)
    inventory_df = inventory_df.drop_duplicates(subset="variantId", keep="last").reset_index(drop=True)
    inventory_df = inventory_df.rename(columns={"quantity": "current_quantity"})
    inventory_df = pandas.concat([inventory_df, no_upc_inventory], ignore_index=True)

    google_df = pandas.concat([out_rma_google_df, out_upc_google_df], ignore_index=True)

    # P4: round to 2dp; compareAtPrice NaN -> None.
    prices_df["new_price"] = prices_df["new_price"].astype(float).round(2)
    prices_df["compareAtPrice"] = prices_df["compareAtPrice"].astype(float).round(2)
    prices_df["compareAtPrice"] = prices_df["compareAtPrice"].where(prices_df["compareAtPrice"].notna(), None)

    inventory_df["current_quantity"] = inventory_df["current_quantity"].astype(int)
    inventory_df["target_quantity"] = inventory_df["target_quantity"].astype(int)

    # Bug fix (live dry-run finding, 2026-10-01): a left-merge upstream (RMA
    # path, store_for_rma joined onto rma_only_cols) introduces NaN into
    # stocked/tracked for RMA-sheet rows with no matching store variant; even
    # after those rows are filtered out (R8), the column's dtype stays
    # object/float instead of reverting to bool. Concatenating that with the
    # UPC path's real bool column then upcasts the combined column to object.
    # Downstream, `~object_column_of_python_bools` is bitwise-NOT (not
    # logical), giving -1/-2 instead of True/False, which `.loc[]` then
    # misreads as integer labels -> KeyError. Normalize to a clean bool dtype
    # here, at the ChangeSet boundary, treating missing as False (never
    # matched a real store inventory level / was never tracked).
    inventory_df["stocked"] = inventory_df["stocked"].fillna(False).astype(bool)
    inventory_df["tracked"] = inventory_df["tracked"].fillna(False).astype(bool)

    return ChangeSet(
        prices=prices_df,
        inventory=inventory_df,
        vendors=vendors_df,
        google=google_df,
        no_upc_count=no_upc_count,
        zero_price_count=zero_price_count,
        feed_match_count=feed_match_count,
        feed_match_total=feed_match_total,
    )


def _finish_google_df(df, settings):
    if df.empty:
        return pandas.DataFrame(columns=["google_merchant_id", "sku", "upc", "new_price", "compareAtPrice", "stock", "variantId"])
    df = df.copy()
    df["compareAtPrice"] = df["compareAtPrice"].astype("float").round(2)
    df["stock"] = "out of stock"
    df.loc[df["total_quantity"] > 0, "stock"] = "in stock"
    df["google_merchant_id"] = settings.google_merchant_id
    return df[["google_merchant_id", "sku", "upc", "new_price", "compareAtPrice", "stock", "variantId"]]


def evaluate_breaker(in_store_df, changeset, settings):
    """R7 circuit breaker, evaluated on the FULL ChangeSet before any write.
    Pure function so it's independently testable."""
    nonzero_total = int((in_store_df["quantity"] > 0).sum())
    zero_out = changeset.inventory.loc[
        (changeset.inventory["target_quantity"] == 0) & (changeset.inventory["current_quantity"] > 0)
    ]
    zero_out_pct = (len(zero_out) / nonzero_total * 100.0) if nonzero_total else 0.0

    store_prices = in_store_df[["variantId", "price"]].rename(columns={"price": "store_price"})
    priced_total = int((in_store_df["price"] > 0).sum())
    merged = pandas.merge(changeset.prices, store_prices, how="left", on="variantId")
    merged = merged.loc[merged["store_price"] > 0]
    big_move = merged.loc[(abs((merged["new_price"] - merged["store_price"]) / merged["store_price"])) > 0.30]
    big_move_pct = (len(big_move) / priced_total * 100.0) if priced_total else 0.0

    reasons = []
    if zero_out_pct > settings.breaker_zero_pct:
        reasons.append("%.1f%% of in-stock variants would go to 0 (> %s%%)" % (zero_out_pct, settings.breaker_zero_pct))
    if big_move_pct > settings.breaker_price_pct:
        reasons.append("%.1f%% of priced variants would move > 30%% (> %s%%)" % (big_move_pct, settings.breaker_price_pct))

    return BreakerResult(zero_out_pct=zero_out_pct, big_move_pct=big_move_pct, tripped=bool(reasons), reasons=reasons)

"""Black-box, adversarial tests for rvpartstore.pricing (design spec sec. 6,
P1-P7, R8, R9, R12/V5, R13, V6; priorities list in the test-authoring brief).

Every expected number below is hand-computed in a comment next to the
assertion from the documented rules:
  - vendor shipping formulas + max(cost*1.15, cost+7)
  - zero-quantity vendors priced at the highest vendor price (excluded from
    winning on price)
  - lowest-vendor selection (incl. documented tie-break: later vendor in the
    assignment order wins ties -- a "kept verbatim" pre-existing quirk, P1)
  - RMA stock sold first
  - restricted SKUs
  - MAP floor
  - amazon repricer: new_price = amazon_price * 0.978, compareAtPrice =
    my_price / 0.978 (recovers the original amazon price)
  - 1% price-change threshold (strict >)
  - quantity buckets 0/1/4/6, P3 exact current-vs-target compare
  - R8 (RMA matched by variantId, store compareAtPrice NULL everywhere)
  - R9/V6 feed_matched guard (documented in README as a fix for a 3-way
    interaction bug between R13/V6/R9)
  - R13 $0/NaN guard
  - P2 UPC-less split
  - evaluate_breaker thresholds
"""
import math

import pandas
import pytest

from rvpartstore.pricing import DISCOUNT_RATE, compute_changes, evaluate_breaker, ChangeSet

from .conftest import (
    make_feed_row, make_feed_df, make_store_row, make_store_df,
    empty_amazon_df, make_amazon_df, empty_rma_sheet_df, make_rma_sheet_df,
    empty_restricted_df, make_restricted_df, empty_map_df, make_map_df,
    empty_upc_exception_df, make_pricing_settings,
)


def _compute(store_rows, feed_rows=None, amazon_df=None, rma_df=None,
             restricted_df=None, map_df=None, upc_exc_df=None, settings=None):
    return compute_changes(
        make_store_df(store_rows),
        make_feed_df(feed_rows or []),
        amazon_df if amazon_df is not None else empty_amazon_df(),
        rma_df if rma_df is not None else empty_rma_sheet_df(),
        restricted_df if restricted_df is not None else empty_restricted_df(),
        map_df if map_df is not None else empty_map_df(),
        upc_exc_df if upc_exc_df is not None else empty_upc_exception_df(),
        settings or make_pricing_settings(),
    )


def _price_row(cs, variant_id):
    match = cs.prices.loc[cs.prices["variantId"] == variant_id]
    assert len(match.index) == 1, "expected exactly one price row for %s, got %d" % (variant_id, len(match.index))
    return match.iloc[0]


def _inventory_row(cs, variant_id):
    match = cs.inventory.loc[cs.inventory["variantId"] == variant_id]
    assert len(match.index) == 1, "expected exactly one inventory row for %s, got %d" % (variant_id, len(match.index))
    return match.iloc[0]


def _filler_feed_row():
    # A throwaway feed row for an unrelated UPC, used only so feed_df has at
    # least one row (see test_compute_changes_crashes_on_completely_empty_feed_df
    # for the dedicated regression test covering the 0-row case itself).
    return make_feed_row("000000000116", keystoneCost=1.0, keystoneQuantity=1)


# ---------------------------------------------------------------------------
# vendor shipping formulas + max(cost*1.15, cost+7)
# ---------------------------------------------------------------------------
def test_keystone_formula_high_branch_cost_times_1_15_wins():
    # keystoneCost=40, PurolatorGroundAssessorials=5, keystoneLTL=0:
    #   keystoneShipping = 5 + 11.49 = 16.49
    #   interim = 40 + 16.49 = 56.49
    #   cost*1.15 = 64.9635 ; cost+7 = 63.49 -> max = 64.9635 -> round 64.96
    # motorstate (a would-be competitor) is given a huge cost so it can't win
    # and so highestPrice (used to reprice the zero-qty vendors) is clearly
    # above 64.9635, guaranteeing keystone strictly wins (no tie).
    feed = [make_feed_row(
        "012345678905",
        keystoneCost=40.0, PurolatorGroundAssessorials=5.0, keystoneLTL=0, keystoneQuantity=10,
        motorstateCost=1000.0, motorstateQuantity=0, RealWeight=0.0,
    )]
    store = [make_store_row("P1", "V1", "012345678905", price=50.00, quantity=2, metafield_value="MS")]
    cs = _compute(store, feed)

    price = _price_row(cs, "V1")
    assert price["new_price"] == 64.96
    vendor = cs.vendors.loc[cs.vendors["productId"] == "P1"].iloc[0]
    assert vendor["lowest_vendor"] == "KS"


def test_motorstate_formula_low_branch_cost_plus_7_wins():
    # motorstateCost=20, RealWeight=10, motorstateLTL=0:
    #   motorstateShipping = 10/2 + 13 = 18 ; interim = 20 + 18 = 38
    #   cost*1.15 = 43.7 ; cost+7 = 45 -> max = 45 (the "+7" branch wins)
    feed = [make_feed_row(
        "012345678912",
        motorstateCost=20.0, RealWeight=10.0, motorstateLTL=0, motorstateQuantity=5,
        keystoneCost=1000.0, keystoneQuantity=0,
    )]
    store = [make_store_row("P2", "V2", "012345678912", price=10.00, quantity=0)]
    cs = _compute(store, feed)

    price = _price_row(cs, "V2")
    assert price["new_price"] == 45.00
    vendor = cs.vendors.loc[cs.vendors["productId"] == "P2"].iloc[0]
    assert vendor["lowest_vendor"] == "MS"


# ---------------------------------------------------------------------------
# zero-quantity vendors priced at highest / lowest-vendor selection
# ---------------------------------------------------------------------------
def test_zero_quantity_vendor_never_wins_despite_cheaper_raw_cost():
    # Keystone has the cheapest RAW cost (10) but quantity=0 -> its cost gets
    # overwritten to highestPrice, so it can never be selected. Motorstate
    # (quantity=5, higher raw cost) must win instead.
    feed = [make_feed_row(
        "012345678929",
        keystoneCost=10.0, keystoneQuantity=0,
        motorstateCost=43.49, RealWeight=0.0, motorstateLTL=0, motorstateQuantity=5,
    )]
    store = [make_store_row("P3", "V3", "012345678929", price=1.00, quantity=0)]
    cs = _compute(store, feed)

    vendor = cs.vendors.loc[cs.vendors["productId"] == "P3"].iloc[0]
    assert vendor["lowest_vendor"] == "MS"
    price = _price_row(cs, "V3")
    # motorstate interim = 43.49 + 13 = 56.49 -> max(64.9635, 63.49) = 64.9635
    assert price["new_price"] == 64.96


def test_tie_break_later_vendor_in_assignment_order_wins():
    # Keystone and Motorstate are engineered to produce the EXACT same final
    # cost (64.9635, both via the cost*1.15 branch, interim=56.49 for both).
    # Both have real stock (qty>0), so neither is overridden to highestPrice.
    # Per the code's assignment order (IB,PR,TF,ME,CBK,MS,KS,PA,CW) KS is
    # assigned after MS, so on an exact tie KS must win.
    feed = [make_feed_row(
        "012345678936",
        keystoneCost=40.0, PurolatorGroundAssessorials=5.0, keystoneLTL=0, keystoneQuantity=10,
        motorstateCost=43.49, RealWeight=0.0, motorstateLTL=0, motorstateQuantity=10,
        turnfourteenCost=1000.0, turnfourteenQuantity=0,  # pushes highestPrice well above the tie
    )]
    store = [make_store_row("P4", "V4", "012345678936", price=1.00, quantity=0)]
    cs = _compute(store, feed)

    vendor = cs.vendors.loc[cs.vendors["productId"] == "P4"].iloc[0]
    assert vendor["lowest_vendor"] == "KS"
    assert _price_row(cs, "V4")["new_price"] == 64.96


# ---------------------------------------------------------------------------
# RMA stock sold first (in-feed RMA columns) + R8 (matched by variantId)
# ---------------------------------------------------------------------------
def test_rma_stock_sold_first_even_when_cheaper_vendor_has_stock():
    # This UPC is present BOTH in the vendor feed (Keystone, cheap, in
    # stock) AND in the RMA google sheet (matched via an Amazon "IB" price
    # listing so its quantity isn't defaulted away) -- the "RMA overlaps
    # feed" case, not the separate rma_only path. "rma price"/"rma quantity"
    # are attached to the feed row by pricing.compute_changes' own merge
    # against the RMA sheet, not supplied directly. Even though Keystone is
    # objectively cheaper and in stock, RMA quantity > 0 must force the "IB"
    # vendor to win, using the RMA price as-is (this path does NOT apply the
    # 0.978 discount -- that only happens in the separate rma_only
    # defaulting cascade for UPCs with no feed match at all).
    feed = [make_feed_row("012345678943", keystoneCost=5.0, keystoneQuantity=10)]
    amazon = make_amazon_df([
        {"price": 200.0, "quantity": 5, "seller-sku": "XX-IB-012345678943", "status": "Active"},
    ])
    rma_sheet = make_rma_sheet_df([
        {"UPC": "012345678943", "quantity on hand": "3", "Cost CAD @": 50.0},
    ])
    store = [make_store_row("P5", "V5", "012345678943", price=1.00, quantity=0)]
    cs = _compute(store, feed, amazon_df=amazon, rma_df=rma_sheet)

    vendor = cs.vendors.loc[cs.vendors["productId"] == "P5"].iloc[0]
    assert vendor["lowest_vendor"] == "IB"
    price = _price_row(cs, "V5")
    assert price["new_price"] == 200.00
    inv = _inventory_row(cs, "V5")
    # total_quantity is taken from RMA's own quantity (3) -> bucket target 1.
    assert inv["target_quantity"] == 1


def test_r8_rma_only_matches_store_variant_even_though_compareatprice_is_null():
    # R8: the store's compareAtPrice is NaN for every variant on the new
    # store; RMA matching must be based on variantId (not on
    # ~compareAtPrice.isna(), which would drop every row). This variant's UPC
    # is present ONLY in the RMA google sheet (not in the vendor feed at
    # all), so it only reaches Shopify via the rma_only path.
    # ib_file entry gives a real "rma price" (30) so the RMA quantity isn't
    # zeroed by the no-Amazon-match default (see rma_only defaulting rule).
    amazon = make_amazon_df([
        {"price": 30.0, "quantity": 5, "seller-sku": "XX-IB-012345678950", "status": "Active"},
    ])
    rma_sheet = make_rma_sheet_df([
        {"UPC": "012345678950", "quantity on hand": "7", "Cost CAD @": 20.0},
    ])
    store = [make_store_row("P6", "V6", "012345678950", price=55.00, quantity=4, metafield_value="KS")]
    cs = _compute(store, feed_rows=[_filler_feed_row()], amazon_df=amazon, rma_df=rma_sheet)

    price = _price_row(cs, "V6")
    # rma price (final) = ib price(30) * DISCOUNT_RATE = 29.34
    assert price["new_price"] == 29.34
    # compareAtPrice = new_price / DISCOUNT_RATE = 30.00 (recovers ib price)
    assert price["compareAtPrice"] == 30.00
    inv = _inventory_row(cs, "V6")
    # rma quantity = 7 (sheet's "quantity on hand", preserved because an ib
    # price WAS found) -> bucket target = 6 (7 > 5).
    assert inv["target_quantity"] == 6
    vendor = cs.vendors.loc[cs.vendors["productId"] == "P6"].iloc[0]
    assert vendor["lowest_vendor"] == "IB"


# ---------------------------------------------------------------------------
# restricted SKUs
# ---------------------------------------------------------------------------
def test_restricted_sku_zeroes_all_vendor_quantities_to_no_vendor_and_zero_stock():
    # Restricted -> ALL vendor/rma quantity columns are zeroed before vendor
    # selection. With every vendor at quantity 0, all_vendor_total_quantity
    # == 0 so lowest_vendor is forced to "NO VENDOR" (overriding whatever the
    # per-vendor tie-break loop picked) and total_quantity -> 0 -> inventory
    # target 0. "NO VENDOR" is explicitly excluded from the vendor-change
    # set, so no vendor metafield row should be emitted even though the
    # store's current vendor ("KS") differs from "NO VENDOR".
    feed = [make_feed_row(
        "012345678967",
        keystoneCost=40.0, PurolatorGroundAssessorials=5.0, keystoneLTL=0, keystoneQuantity=5,
        motorstateCost=1.0, motorstateQuantity=1,
    )]
    restricted = make_restricted_df(["RESTRICTED-SKU-012345678967"])
    store = [make_store_row("P7", "V7", "012345678967", price=10.00, quantity=3, metafield_value="KS")]
    cs = _compute(store, feed, restricted_df=restricted)

    inv = _inventory_row(cs, "V7")
    assert inv["target_quantity"] == 0
    assert inv["current_quantity"] == 3  # current store quantity, unchanged column
    assert cs.vendors.loc[cs.vendors["productId"] == "P7"].empty, (
        "NO VENDOR must never be written back as a vendor metafield change"
    )


# ---------------------------------------------------------------------------
# MAP floor
# ---------------------------------------------------------------------------
def test_map_floor_raises_price_when_map_is_higher():
    # Keystone's computed cost is 64.9635 (see the high-branch test above);
    # a MAP price of 100 must clip new_price up to 100.00.
    feed = [make_feed_row(
        "012345678974",
        keystoneCost=40.0, PurolatorGroundAssessorials=5.0, keystoneLTL=0, keystoneQuantity=10,
    )]
    store = [make_store_row("P8", "V8", "012345678974", price=10.00, quantity=0)]
    map_df = make_map_df([{"upc": "012345678974", "map_price": 100.0}])
    cs = _compute(store, feed, map_df=map_df)

    assert _price_row(cs, "V8")["new_price"] == 100.00


def test_map_floor_no_effect_when_map_is_lower():
    feed = [make_feed_row(
        "012345678981",
        keystoneCost=40.0, PurolatorGroundAssessorials=5.0, keystoneLTL=0, keystoneQuantity=10,
    )]
    store = [make_store_row("P9", "V9", "012345678981", price=10.00, quantity=0)]
    map_df = make_map_df([{"upc": "012345678981", "map_price": 1.0}])
    cs = _compute(store, feed, map_df=map_df)

    assert _price_row(cs, "V9")["new_price"] == 64.96


# ---------------------------------------------------------------------------
# amazon repricer: new_price = price * 0.978, compareAt = my_price / 0.978
# ---------------------------------------------------------------------------
def test_amazon_repricer_overrides_vendor_price_with_978_discount():
    # Amazon match takes precedence over the vendor-cost price unconditionally.
    # amazon price = 200 -> my_price = 200*0.978 = 195.60 ; compareAtPrice =
    # my_price/0.978 = 200.00 (recovers the original amazon price exactly).
    feed = [make_feed_row(
        "012345678998",
        keystoneCost=40.0, PurolatorGroundAssessorials=5.0, keystoneLTL=0, keystoneQuantity=10,
    )]
    amazon = make_amazon_df([
        {"price": 200.0, "quantity": 5, "seller-sku": "AU-XX-012345678998", "status": "Active"},
    ])
    store = [make_store_row("P10", "V10", "012345678998", price=10.00, quantity=0)]
    cs = _compute(store, feed, amazon_df=amazon)

    price = _price_row(cs, "V10")
    assert price["new_price"] == round(200.0 * DISCOUNT_RATE, 2) == 195.60
    assert price["compareAtPrice"] == round((200.0 * DISCOUNT_RATE) / DISCOUNT_RATE, 2) == 200.00


# ---------------------------------------------------------------------------
# 1% price-change threshold (strict >)
# ---------------------------------------------------------------------------
def test_price_change_threshold_boundary_exactly_1pct_is_not_emitted():
    # store price 100.00, amazon-driven my_price EXACTLY 101.00 -> diff is
    # exactly 1% -> rule requires strictly > 0.01, so NO price row.
    #
    # This UPC is deliberately NOT in the vendor feed (only an unrelated
    # filler UPC is) so feed_matched=False for it: that guards off V6's
    # compareAtPrice-diff check (store compareAtPrice is NaN and would
    # otherwise ALWAYS look "changed"), isolating the 1% price threshold as
    # the only thing under test here.
    target_my_price = 101.00
    amazon_price = target_my_price / DISCOUNT_RATE
    amazon = make_amazon_df([
        {"price": amazon_price, "quantity": 5, "seller-sku": "AU-XX-012345679001", "status": "Active"},
    ])
    store = [make_store_row("P11", "V11", "012345679001", price=100.00, quantity=0)]
    cs = _compute(store, [_filler_feed_row()], amazon_df=amazon)

    assert cs.prices.loc[cs.prices["variantId"] == "V11"].empty


def test_price_change_threshold_boundary_just_over_1pct_is_emitted():
    target_my_price = 101.01
    amazon_price = target_my_price / DISCOUNT_RATE
    amazon = make_amazon_df([
        {"price": amazon_price, "quantity": 5, "seller-sku": "AU-XX-012345679018", "status": "Active"},
    ])
    store = [make_store_row("P12", "V12", "012345679018", price=100.00, quantity=0)]
    cs = _compute(store, [_filler_feed_row()], amazon_df=amazon)

    price = _price_row(cs, "V12")
    assert price["new_price"] == 101.01


# ---------------------------------------------------------------------------
# quantity buckets 0/1/4/6 (P3 exact compare)
# ---------------------------------------------------------------------------
def test_quantity_bucket_boundaries_exact():
    # One row per bucket boundary; Keystone is the sole vendor with the given
    # quantity. Store's current quantity is always 0 so a nonzero target
    # always emits a row; qty=0 with target 0 must NOT emit (0 == 0).
    cases = [
        ("012345679100", 0, 0),
        ("012345679117", 1, 1),
        ("012345679124", 3, 1),
        ("012345679131", 4, 4),
        ("012345679148", 5, 4),
        ("012345679155", 6, 6),
        ("012345679162", 7, 6),
    ]
    feed = [make_feed_row(upc, keystoneCost=1.0, keystoneQuantity=qty) for upc, qty, _ in cases]
    store = [make_store_row("P-%s" % upc, "V-%s" % upc, upc, price=1.00, quantity=0) for upc, _, _ in cases]
    cs = _compute(store, feed)

    for upc, qty, expected_target in cases:
        variant_id = "V-%s" % upc
        rows = cs.inventory.loc[cs.inventory["variantId"] == variant_id]
        if expected_target == 0:
            assert rows.empty, "qty=%d (target 0) must not emit an inventory change" % qty
        else:
            assert len(rows.index) == 1
            assert rows.iloc[0]["target_quantity"] == expected_target


def test_p3_exact_compare_catches_same_bucket_drift():
    # Vendor quantity=2 -> bucket target=1. Store's CURRENT recorded quantity
    # is 4 (not a valid bucket value, simulating drift). P3 requires an EXACT
    # compare (target != quantity), so this must emit a change even though a
    # naive bucket(4) vs bucket(1) comparison might not have caught it.
    feed = [make_feed_row("012345679193", keystoneCost=1.0, keystoneQuantity=2)]
    store = [make_store_row("P13", "V13", "012345679193", price=1.00, quantity=4)]
    cs = _compute(store, feed)

    inv = _inventory_row(cs, "V13")
    assert inv["current_quantity"] == 4
    assert inv["target_quantity"] == 1


# ---------------------------------------------------------------------------
# R9/V6 feed_matched guard -- the adversarial regression anchor
# ---------------------------------------------------------------------------
def test_r9_v6_feed_matched_guard_prevents_rma_price_from_being_clobbered():
    # This variant's UPC is present ONLY in the RMA sheet, never in the
    # vendor feed at all (feed_matched=False for the UPC path). Store price
    # is 55.00, store compareAtPrice is NaN (R8 baseline).
    #
    # Without the "feed_matched" guard on V6's compareAtPrice-diff check, the
    # UPC path would see NaN(store) -> 55.00(new_price passthrough) as a
    # "changed" compareAtPrice (V6), producing a spurious UPC-side price row
    # carrying the untouched store price (55.00) forward. Per R9 ("price:
    # UPC wins"), that spurious row would then win the dedupe and CLOBBER the
    # real RMA-computed price -- this is exactly the bug the README documents
    # as having been fixed with the feed_matched guard.
    #
    # RMA math: ib price 40 -> rma price = 40*0.978 = 39.12; sheet quantity
    # on hand = 7 -> bucket target = 6.
    amazon = make_amazon_df([
        {"price": 40.0, "quantity": 5, "seller-sku": "XX-IB-012345679209", "status": "Active"},
    ])
    rma_sheet = make_rma_sheet_df([
        {"UPC": "012345679209", "quantity on hand": "7", "Cost CAD @": 20.0},
    ])
    store = [make_store_row("P14", "V14", "012345679209", price=55.00, quantity=4, metafield_value="KS")]
    cs = _compute(store, feed_rows=[_filler_feed_row()], amazon_df=amazon, rma_df=rma_sheet)

    price = _price_row(cs, "V14")
    assert price["new_price"] == round(40.0 * DISCOUNT_RATE, 2) == 39.12, (
        "UPC path's spurious pass-through row must not have clobbered the RMA price"
    )
    inv = _inventory_row(cs, "V14")
    # R9: inventory -> RMA wins (target 6), not the UPC path's target 0
    # (which would have zeroed a variant that is genuinely in stock via RMA).
    assert inv["target_quantity"] == 6


def test_unmatched_upc_not_in_feed_or_rma_gets_zeroed_inventory_no_price_change():
    # Baseline/control for the above: a UPC present in neither the feed nor
    # the RMA sheet should just fall through the UPC path -> feed_matched
    # False, price passthrough (no change, guarded), inventory forced to the
    # "not in feed" target of 0 (R12 "rest go to qty 0" rule).
    store = [make_store_row("P15", "V15", "999999999999", price=42.00, quantity=5)]
    cs = _compute(store, feed_rows=[_filler_feed_row()])

    assert cs.prices.loc[cs.prices["variantId"] == "V15"].empty
    inv = _inventory_row(cs, "V15")
    assert inv["target_quantity"] == 0
    assert inv["current_quantity"] == 5


# ---------------------------------------------------------------------------
# R13: $0 / NaN price guard
# ---------------------------------------------------------------------------
def test_r13_zero_amazon_price_blocks_price_change_and_zeroes_inventory():
    # Amazon repricer price of 0 -> my_price = 0 -> new_price = 0 -> R13:
    # no price change emitted (new_price snapped back to store price so the
    # price-change filter is false) and inventory target forced to 0, even
    # though the vendor feed shows real stock.
    feed = [make_feed_row(
        "012345679216",
        keystoneCost=5.0, keystoneQuantity=10,
    )]
    amazon = make_amazon_df([
        {"price": 0.0, "quantity": 5, "seller-sku": "AU-XX-012345679216", "status": "Active"},
    ])
    store = [make_store_row("P16", "V16", "012345679216", price=30.00, quantity=2)]
    cs = _compute(store, feed, amazon_df=amazon)

    assert cs.prices.loc[cs.prices["variantId"] == "V16"].empty, "R13: no price row for a $0 computed price"
    inv = _inventory_row(cs, "V16")
    assert inv["target_quantity"] == 0


# ---------------------------------------------------------------------------
# P2: UPC-less split (option b)
# ---------------------------------------------------------------------------
def test_upc_less_variants_with_stock_get_zeroed_and_excluded_from_pricing():
    store = [
        make_store_row("P17", "V17", None, price=25.00, quantity=3, metafield_value="KS", stocked=True),
        make_store_row("P18", "V18", "NOT-A-UPC", price=25.00, quantity=6, metafield_value="KS", stocked=True),
        # A valid-UPC control row that SHOULD be priced normally, to prove
        # the split doesn't also swallow real UPC variants.
        make_store_row("P19", "V19", "012345679223", price=1.00, quantity=0),
    ]
    feed = [make_feed_row("012345679223", keystoneCost=5.0, keystoneQuantity=10)]
    cs = _compute(store, feed)

    assert cs.no_upc_count == 2
    for vid in ("V17", "V18"):
        inv = _inventory_row(cs, vid)
        assert inv["target_quantity"] == 0
        assert cs.prices.loc[cs.prices["variantId"] == vid].empty, "UPC-less rows must never get a price change"
        assert cs.vendors.loc[cs.vendors["variantId"] == vid].empty if "variantId" in cs.vendors.columns else True

    # Control row: has a real UPC, matched in feed -> must still be priced.
    assert not cs.prices.loc[cs.prices["variantId"] == "V19"].empty


def test_upc_less_variant_with_zero_quantity_not_in_inventory_changeset():
    # P2: only rows with quantity != 0 (and stocked) get a zero-out change.
    store = [make_store_row("P20", "V20", None, price=25.00, quantity=0, stocked=True)]
    cs = _compute(store, feed_rows=[_filler_feed_row()])

    assert cs.no_upc_count == 1
    assert cs.inventory.loc[cs.inventory["variantId"] == "V20"].empty


def test_upc_less_split_does_not_crash_on_null_upc_astype_int64():
    # P2 explicitly calls out fixing an old astype('int64') crash on NULL
    # UPC; simply not raising here IS the regression test.
    store = [make_store_row("P21", "V21", None, price=25.00, quantity=1, stocked=True)]
    compute_changes(
        make_store_df(store), make_feed_df([_filler_feed_row()]), empty_amazon_df(), empty_rma_sheet_df(),
        empty_restricted_df(), empty_map_df(), empty_upc_exception_df(), make_pricing_settings(),
    )  # must not raise


def test_compute_changes_does_not_crash_on_completely_empty_feed_df():
    # Regression test: a feed_df with zero rows at all (e.g. every one of
    # the 9 merged_feed tables genuinely returns nothing after filtering, or
    # a transient bad pull) used to raise a raw pandas ValueError from deep
    # inside _select_lowest_vendor ("cannot set a frame with no defined
    # index and a scalar") instead of treating every store UPC as unmatched.
    # R7's "abort if any merged_feed table has 0 rows" guard lives in the
    # caller (sources.py/main.py), not in this pure function, so
    # compute_changes needs to be defensive against this input shape on its
    # own. Confirmed fixed as of this revision.
    store = [make_store_row("P26", "V26", "012345679278", price=10.00, quantity=1)]
    cs = _compute(store, feed_rows=[])
    assert cs.no_upc_count == 0


# ---------------------------------------------------------------------------
# evaluate_breaker
# ---------------------------------------------------------------------------
def test_breaker_trips_on_zero_out_percentage():
    in_store_df = make_store_df([
        make_store_row("P%d" % i, "V%d" % i, "01234567%04d" % i, price=10.00, quantity=1)
        for i in range(10)
    ])
    # 3 of 10 currently-in-stock variants going to 0 = 30% > default 20%.
    inventory = pandas.DataFrame({
        "variantId": ["V0", "V1", "V2"],
        "inventoryId": ["I0", "I1", "I2"],
        "current_quantity": [1, 1, 1],
        "target_quantity": [0, 0, 0],
    })
    changeset = ChangeSet(
        prices=pandas.DataFrame(columns=["productId", "variantId", "new_price", "compareAtPrice"]),
        inventory=inventory, vendors=pandas.DataFrame(columns=["productId", "lowest_vendor"]),
        google=pandas.DataFrame(), no_upc_count=0, zero_price_count=0,
        feed_match_count=0, feed_match_total=0,
    )
    result = evaluate_breaker(in_store_df, changeset, make_pricing_settings())
    assert result.zero_out_pct == 30.0
    assert result.tripped is True
    assert any("0" in r or "%" in r for r in result.reasons)


def test_breaker_does_not_trip_below_thresholds():
    in_store_df = make_store_df([
        make_store_row("P%d" % i, "V%d" % i, "01234567%04d" % i, price=10.00, quantity=1)
        for i in range(10)
    ])
    inventory = pandas.DataFrame({
        "variantId": ["V0"], "inventoryId": ["I0"], "current_quantity": [1], "target_quantity": [0],
    })
    changeset = ChangeSet(
        prices=pandas.DataFrame(columns=["productId", "variantId", "new_price", "compareAtPrice"]),
        inventory=inventory, vendors=pandas.DataFrame(columns=["productId", "lowest_vendor"]),
        google=pandas.DataFrame(), no_upc_count=0, zero_price_count=0,
        feed_match_count=0, feed_match_total=0,
    )
    result = evaluate_breaker(in_store_df, changeset, make_pricing_settings())
    assert result.zero_out_pct == 10.0  # 1 of 10 -> below default 20%
    assert result.tripped is False
    assert result.reasons == []


def test_breaker_trips_on_big_price_move_percentage():
    in_store_df = make_store_df([
        make_store_row("P%d" % i, "V%d" % i, "01234567%04d" % i, price=100.00, quantity=1)
        for i in range(10)
    ])
    # 3 of 10 priced variants moving > 30% = 30% > default 20%.
    prices = pandas.DataFrame({
        "productId": ["P0", "P1", "P2"], "variantId": ["V0", "V1", "V2"],
        "new_price": [140.0, 140.0, 140.0],  # +40% move
        "compareAtPrice": [None, None, None],
    })
    changeset = ChangeSet(
        prices=prices, inventory=pandas.DataFrame(columns=["variantId", "inventoryId", "current_quantity", "target_quantity"]),
        vendors=pandas.DataFrame(columns=["productId", "lowest_vendor"]),
        google=pandas.DataFrame(), no_upc_count=0, zero_price_count=0,
        feed_match_count=0, feed_match_total=0,
    )
    result = evaluate_breaker(in_store_df, changeset, make_pricing_settings())
    assert result.big_move_pct == 30.0
    assert result.tripped is True


def test_breaker_custom_thresholds_from_settings():
    in_store_df = make_store_df([
        make_store_row("P%d" % i, "V%d" % i, "01234567%04d" % i, price=10.00, quantity=1)
        for i in range(10)
    ])
    inventory = pandas.DataFrame({
        "variantId": ["V0"], "inventoryId": ["I0"], "current_quantity": [1], "target_quantity": [0],
    })
    changeset = ChangeSet(
        prices=pandas.DataFrame(columns=["productId", "variantId", "new_price", "compareAtPrice"]),
        inventory=inventory, vendors=pandas.DataFrame(columns=["productId", "lowest_vendor"]),
        google=pandas.DataFrame(), no_upc_count=0, zero_price_count=0,
        feed_match_count=0, feed_match_total=0,
    )
    # 1 of 10 = 10% zero-out; with a threshold of 5% it must now trip.
    strict_settings = make_pricing_settings(breaker_zero_pct=5.0)
    result = evaluate_breaker(in_store_df, changeset, strict_settings)
    assert result.tripped is True


# ---------------------------------------------------------------------------
# R12/V5: feed match counters
# ---------------------------------------------------------------------------
def test_feed_match_counters():
    store = [
        make_store_row("P22", "V22", "012345679230", price=1.00, quantity=0),  # matched in feed
        make_store_row("P23", "V23", "012345679247", price=1.00, quantity=0),  # NOT in feed
    ]
    feed = [make_feed_row("012345679230", keystoneCost=1.0, keystoneQuantity=1)]
    cs = _compute(store, feed)
    assert cs.feed_match_total == 2
    assert cs.feed_match_count == 1


# ---------------------------------------------------------------------------
# P6: google list only built when enabled
# ---------------------------------------------------------------------------
def test_google_changeset_empty_when_disabled():
    feed = [make_feed_row("012345679254", keystoneCost=5.0, keystoneQuantity=10)]
    store = [make_store_row("P24", "V24", "012345679254", price=1.00, quantity=0)]
    cs = _compute(store, feed, settings=make_pricing_settings(google_merchant_enabled=False))
    assert cs.google.empty


def test_google_changeset_populated_when_enabled():
    feed = [make_feed_row("012345679261", keystoneCost=5.0, keystoneQuantity=10)]
    store = [make_store_row("P25", "V25", "012345679261", price=1.00, quantity=0)]
    cs = _compute(store, feed, settings=make_pricing_settings(google_merchant_enabled=True))
    assert not cs.google.empty
    assert set(cs.google.columns) == {
        "google_merchant_id", "sku", "upc", "new_price", "compareAtPrice", "stock", "variantId"
    }


# ---------------------------------------------------------------------------
# RMA/UPC inventory oscillation fix + RMA repricer join on upc string AND
# int_UPC (leading-zero protection)
# ---------------------------------------------------------------------------
def test_rma_matched_variant_never_gets_upc_path_inventory_change_no_oscillation():
    # Store qty=4, RMA sheet qty=5 (bucket target=4 -> already equals current,
    # so the RMA path itself emits NO inventory change), and this UPC is NOT
    # in the vendor feed at all. Before the fix, the UPC path (unmatched ->
    # target 0) would have fired here (0 != 4), and on a LATER run (after
    # Shopify's quantity got set to 0 by that write), the RMA path would then
    # fire instead (target 4 != new current 0) -- oscillating forever. The
    # fix excludes ANY variant that merged_rma matched at all from the UPC
    # path's inventory output, regardless of whether RMA itself emits a
    # change that run. Expect: no inventory change on this run, and calling
    # compute_changes again with the exact same (unwritten) inputs still
    # produces no inventory change (deterministic convergence, not a flip).
    amazon = make_amazon_df([
        {"price": 50.0, "quantity": 5, "seller-sku": "XX-IB-012345680001", "status": "Active"},
    ])
    rma_sheet = make_rma_sheet_df([
        {"UPC": "012345680001", "quantity on hand": "5", "Cost CAD @": 20.0},
    ])
    store = [make_store_row("P27", "V27", "012345680001", price=20.00, quantity=4)]

    cs1 = _compute(store, feed_rows=[], amazon_df=amazon, rma_df=rma_sheet)
    assert cs1.inventory.loc[cs1.inventory["variantId"] == "V27"].empty

    # Re-run with identical (unwritten) inputs -- must still be empty, not
    # flipped to a target-0 or target-4 change.
    cs2 = _compute(store, feed_rows=[], amazon_df=amazon, rma_df=rma_sheet)
    assert cs2.inventory.loc[cs2.inventory["variantId"] == "V27"].empty


def test_rma_repricer_join_requires_matching_upc_string_not_just_int_upc():
    # A store upc "0012345678905" and an amazon-derived upc "012345678905"
    # are numerically equal (same int_UPC) but must NOT be treated as a
    # repricer match on the RMA path -- the join is on BOTH upc (string) and
    # int_UPC. If the amazon price were wrongly applied here, new_price
    # would be round(999 * 0.978, 2) = 977.22; it must NOT be.
    amazon = make_amazon_df([
        {"price": 40.0, "quantity": 5, "seller-sku": "XX-IB-0012345678905", "status": "Active"},
        {"price": 999.0, "quantity": 5, "seller-sku": "AU-XX-012345678905", "status": "Active"},
    ])
    rma_sheet = make_rma_sheet_df([
        {"UPC": "0012345678905", "quantity on hand": "3", "Cost CAD @": 20.0},
    ])
    store = [make_store_row("P28", "V28", "0012345678905", price=10.00, quantity=0)]
    cs = _compute(store, feed_rows=[], amazon_df=amazon, rma_df=rma_sheet)

    price = _price_row(cs, "V28")
    wrong_price_if_bug_present = round(999.0 * DISCOUNT_RATE, 2)
    assert price["new_price"] != wrong_price_if_bug_present
    # Correct value: rma price = ib price(40) * DISCOUNT_RATE = 39.12.
    assert price["new_price"] == round(40.0 * DISCOUNT_RATE, 2) == 39.12


# ---------------------------------------------------------------------------
# Live bug regression (2026-10-01): an RMA-sheet UPC with NO matching store
# variant at all, mixed into the same run as a genuine UPC-path match, used
# to leave ChangeSet.inventory's stocked/tracked columns as object/float
# dtype (NaN introduced by the orphaned RMA row's left-merge, then not
# cleanly reverted even after R8 filters that row out, then upcasting the
# whole concatenated column when combined with the UPC path's clean bool
# column). shopify_writes.set_inventory's `~column` boolean-mask logic
# depends on these being real bool dtype.
# ---------------------------------------------------------------------------
def test_changeset_inventory_stocked_and_tracked_dtype_is_bool_with_rma_orphan_row():
    # RMA sheet row for a UPC with NO corresponding store variant at all (not
    # just "not in the vendor feed" -- genuinely absent from in_store_df).
    amazon = make_amazon_df([
        {"price": 50.0, "quantity": 5, "seller-sku": "XX-IB-099999999999", "status": "Active"},
    ])
    rma_sheet = make_rma_sheet_df([
        {"UPC": "099999999999", "quantity on hand": "5", "Cost CAD @": 20.0},  # orphan
    ])
    feed = [make_feed_row("012345679300", keystoneCost=5.0, keystoneQuantity=10)]
    store = [make_store_row("P29", "V29", "012345679300", price=1.00, quantity=0)]  # genuine UPC-path match
    cs = _compute(store, feed, amazon_df=amazon, rma_df=rma_sheet)

    assert cs.inventory["stocked"].dtype == bool
    assert cs.inventory["tracked"].dtype == bool
    v29 = cs.inventory.loc[cs.inventory["variantId"] == "V29"]
    assert not v29.empty
    assert bool(v29.iloc[0]["stocked"]) is True

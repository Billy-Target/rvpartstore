"""Black-box tests for rvpartstore.brakex_orders (design spec sec. B, B1-B5).

Contract under test (build_brakex_order_rows):
  * exactly 28 columns, in the documented order (PaymentMethod sits between
    Details_PaymentDate and PaymentStatus).
  * TxnID = BRAKEX_TXN_PREFIX + order_number (e.g. "BRX_SP_37" + "1613" ->
    "BRX_SP_371613"); StoreType = BRAKEX_STORE_TYPE.
  * Promise_Date: vendor is always 'BRX' -> 2 days, same weekend logic as
    orders.py's _promise_date.
  * Total / Items_Amount: non-paypal uses total_price / price-minus-discount
    (ORIGINAL quantity denominator, V7); paypal converts via BRAKEX_USD_TO_CAD
    depending on presentment currency, with a shop_money/presentment_money
    discount_dict fallback cascade (B3, ported verbatim).
  * R15: lines with 0 current_quantity are dropped; an unmapped sku raises
    OrderBuildError (caller skips the whole order).
  * Empty payment_gateway_names -> PaymentMethod None.

pull_brakex_open_orders: same fetch/skip/R10/DRY_RUN contract as orders.py's
pull_open_orders, against BRAKEX_ORDER_TABLE.
"""
import datetime
import types

import pytest

from rvpartstore.brakex_orders import (
    BRAKEX_ORDER_COLUMNS,
    build_brakex_order_rows,
    pull_brakex_open_orders,
)
from rvpartstore.orders import OrderBuildError

EXPECTED_BRAKEX_COLUMNS = [
    "TxnID", "shopifyOrderId", "itemId", "fulfillmentOrderId", "fulfillmentOrderLineItemId",
    "product_id", "Items_Name", "Items_Desc", "Items_Quantity", "Items_Amount", "Items_Shipping",
    "StoreType", "company", "To_Name", "To_Street", "To_Street2", "To_State", "To_City",
    "To_ZipCode", "To_CountryCode", "To_PhoneNumber", "email", "Total_Total", "Details_PaymentDate",
    "PaymentMethod", "PaymentStatus", "riskLevel", "Promise_Date",
]


def _settings(**overrides):
    base = dict(
        brakex_txn_prefix="BRX_SP_37", brakex_store_type="shopifycom_brx", brakex_usd_to_cad=1.35,
        order_table="shopify_rvmarines_order", brakex_order_table="shopify_brakex_order",
        order_max_age_days=60, brakex_order_id_blocklist=(), dry_run=False,
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


def _order_json(**overrides):
    base = {
        "id": 700100200,
        "order_number": 1613,
        "email": "buyer@example.com",
        "total_price": "129.98",
        "processed_at": "2024-01-01T10:00:00-05:00",
        "created_at": "2024-01-01T10:00:00-05:00",
        "payment_gateway_names": ["manual"],
        "shipping_address": {
            "company": "Acme Co", "name": "Jane Doe", "address1": "123 Main St", "address2": "Unit 4",
            "province_code": "ON", "city": "Toronto", "zip": "M1M1M1", "country_code": "CA",
            "phone": "4165551234",
        },
        "line_items": [{
            "id": 1, "sku": "SKU-A", "quantity": 2, "current_quantity": 2,
            "price": "25.00", "title": "Widget", "product_id": 777,
            "discount_allocations": [],
        }],
    }
    base.update(overrides)
    return base


def _fo_info(**overrides):
    base = {"fulfillment_order_ids": ["FO1"], "line_item_map": {"SKU-A": "LI1"}}
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# shape / identity
# ---------------------------------------------------------------------------
def test_exactly_28_columns_in_documented_order():
    assert len(EXPECTED_BRAKEX_COLUMNS) == 28
    assert list(BRAKEX_ORDER_COLUMNS) == EXPECTED_BRAKEX_COLUMNS
    rows = build_brakex_order_rows(_order_json(), _fo_info(), "LOW", _settings(), datetime.date(2024, 1, 1))
    assert list(rows[0].keys()) == EXPECTED_BRAKEX_COLUMNS
    # PaymentMethod sits between Details_PaymentDate and PaymentStatus.
    idx = EXPECTED_BRAKEX_COLUMNS.index
    assert idx("Details_PaymentDate") < idx("PaymentMethod") < idx("PaymentStatus")


def test_txn_id_format_matches_spec_example():
    rows = build_brakex_order_rows(_order_json(order_number=1613), _fo_info(), "LOW",
                                    _settings(), datetime.date(2024, 1, 1))
    assert rows[0]["TxnID"] == "BRX_SP_371613"


def test_store_type_is_shopifycom_brx():
    rows = build_brakex_order_rows(_order_json(), _fo_info(), "LOW", _settings(), datetime.date(2024, 1, 1))
    assert rows[0]["StoreType"] == "shopifycom_brx"


def test_risk_level_never_null_defaults_to_low():
    rows = build_brakex_order_rows(_order_json(), _fo_info(), None, _settings(), datetime.date(2024, 1, 1))
    assert rows[0]["riskLevel"] == "LOW"


def test_payment_status_is_awaiting_fulfillment():
    rows = build_brakex_order_rows(_order_json(), _fo_info(), "LOW", _settings(), datetime.date(2024, 1, 1))
    assert rows[0]["PaymentStatus"] == "Awaiting Fulfillment"


def test_empty_payment_gateway_names_payment_method_is_none():
    rows = build_brakex_order_rows(_order_json(payment_gateway_names=[]), _fo_info(), "LOW",
                                    _settings(), datetime.date(2024, 1, 1))
    assert rows[0]["PaymentMethod"] is None


def test_payment_method_is_first_gateway_name():
    rows = build_brakex_order_rows(_order_json(payment_gateway_names=["shopify_payments", "other"]), _fo_info(),
                                    "LOW", _settings(), datetime.date(2024, 1, 1))
    assert rows[0]["PaymentMethod"] == "shopify_payments"


# ---------------------------------------------------------------------------
# Promise_Date: BRX = 2 days, same weekend logic as orders.py
# ---------------------------------------------------------------------------
def test_promise_date_brx_2_days_no_weekend_adjustment():
    # today = Monday 2024-01-01 (ISO=1): fulfillment_index of (Mon+2=Wed)=3;
    # 3<=1? no; 3>=6? no -> stays 2 days. promise = 2024-01-03.
    order = _order_json(processed_at="2024-01-01T09:00:00-05:00")
    rows = build_brakex_order_rows(order, _fo_info(), "LOW", _settings(), datetime.date(2024, 1, 1))
    assert rows[0]["Promise_Date"] == "2024-01-03"


def test_promise_date_brx_lands_on_weekend_gets_bumped():
    # today = Friday 2024-01-05 (ISO=5): fulfillment_index of (Fri+2=Sun)=7;
    # 7<=5? no; 7>=6? yes -> +2 -> 4 days. promise = 2024-01-09.
    order = _order_json(processed_at="2024-01-05T09:00:00-05:00")
    rows = build_brakex_order_rows(order, _fo_info(), "LOW", _settings(), datetime.date(2024, 1, 5))
    assert rows[0]["Promise_Date"] == "2024-01-09"


# ---------------------------------------------------------------------------
# non-paypal total / item amount
# ---------------------------------------------------------------------------
def test_non_paypal_total_is_total_price_verbatim():
    rows = build_brakex_order_rows(_order_json(total_price="129.98"), _fo_info(), "LOW",
                                    _settings(), datetime.date(2024, 1, 1))
    assert rows[0]["Total_Total"] == "129.98"


def test_non_paypal_item_amount_no_discount():
    order = _order_json(line_items=[{
        "id": 1, "sku": "SKU-A", "quantity": 2, "current_quantity": 2,
        "price": "25.00", "title": "Widget", "product_id": 777, "discount_allocations": [],
    }])
    rows = build_brakex_order_rows(order, _fo_info(), "LOW", _settings(), datetime.date(2024, 1, 1))
    assert rows[0]["Items_Amount"] == 25.00


def test_non_paypal_item_amount_with_discount_uses_original_quantity():
    # price=25.00, ORIGINAL quantity=4, current_quantity=2, discount=8.00 ->
    # item_amount = round(25.00 - 8.00/4, 2) = round(25.00 - 2.00, 2) = 23.00
    order = _order_json(line_items=[{
        "id": 1, "sku": "SKU-A", "quantity": 4, "current_quantity": 2,
        "price": "25.00", "title": "Widget", "product_id": 777,
        "discount_allocations": [{"amount": "8.00"}],
    }])
    rows = build_brakex_order_rows(order, _fo_info(), "LOW", _settings(), datetime.date(2024, 1, 1))
    assert rows[0]["Items_Amount"] == 23.00
    assert rows[0]["Items_Quantity"] == 2


# ---------------------------------------------------------------------------
# paypal total: USD presentment (*rate) vs CAD presentment (as-is)
#
# Realistic Shopify paypal orders always carry BOTH
# current_total_price_set.presentment_money (order-level) AND each line's
# price_set.presentment_money (item-level) -- the new guard (review item 1)
# requires both, raising OrderBuildError for the order if either is missing.
# Every paypal fixture below sets both; the "other" field's concrete value is
# arbitrary wherever a given test isn't asserting on it.
# ---------------------------------------------------------------------------
def _paypal_line_item(presentment_currency, presentment_amount, discount_amount_set, original_quantity=2,
                       current_quantity=2):
    return {
        "id": 1, "sku": "SKU-A", "quantity": original_quantity, "current_quantity": current_quantity,
        "price": "25.00", "title": "Widget", "product_id": 777,
        "price_set": {"presentment_money": {"amount": presentment_amount, "currency_code": presentment_currency}},
        "total_discount_set": {"presentment_money": {"amount": "0", "currency_code": presentment_currency}},
        "discount_allocations": (
            [{"amount_set": discount_amount_set}] if discount_amount_set is not None else []
        ),
    }


_ARBITRARY_ORDER_TOTAL = {"presentment_money": {"amount": "1.00", "currency_code": "USD"}}


def test_paypal_total_usd_presentment_converted_by_rate():
    item = _paypal_line_item("USD", "10.00", None)  # item amount not asserted here
    order = _order_json(
        payment_gateway_names=["paypal"], line_items=[item],
        current_total_price_set={"presentment_money": {"amount": "55.00", "currency_code": "USD"}},
    )
    rows = build_brakex_order_rows(order, _fo_info(), "LOW", _settings(), datetime.date(2024, 1, 1))
    # 55.00 * 1.35 = 74.25
    assert rows[0]["Total_Total"] == 74.25


def test_paypal_total_cad_presentment_used_as_is():
    item = _paypal_line_item("USD", "10.00", None)  # item amount not asserted here
    order = _order_json(
        payment_gateway_names=["paypal"], line_items=[item],
        current_total_price_set={"presentment_money": {"amount": "70.00", "currency_code": "CAD"}},
    )
    rows = build_brakex_order_rows(order, _fo_info(), "LOW", _settings(), datetime.date(2024, 1, 1))
    assert rows[0]["Total_Total"] == 70.00


# ---------------------------------------------------------------------------
# paypal item amount: 4 currency/discount-fallback combinations (B3)
# ---------------------------------------------------------------------------
def test_paypal_item_amount_usd_presentment_with_usd_discount():
    # price_presentment(USD)=30.00, discount shop_money USD=4.00, qty=2:
    # item_amount = 30.00*1.35 - (4.00*1.35)/2 = 40.5 - 2.7 = 37.80
    item = _paypal_line_item("USD", "30.00", {"shop_money": {"amount": "4.00", "currency_code": "USD"}})
    order = _order_json(payment_gateway_names=["paypal"], line_items=[item],
                         current_total_price_set=_ARBITRARY_ORDER_TOTAL)
    rows = build_brakex_order_rows(order, _fo_info(), "LOW", _settings(), datetime.date(2024, 1, 1))
    assert rows[0]["Items_Amount"] == 37.80


def test_paypal_item_amount_usd_presentment_falls_back_to_cad_discount():
    # No "USD" key in discount_dict (only a presentment_money CAD entry) ->
    # falls back to the CAD discount used as-is (no *rate).
    # item_amount = 30.00*1.35 - 6.00/2 = 40.5 - 3.0 = 37.50
    item = _paypal_line_item("USD", "30.00", {"presentment_money": {"amount": "6.00", "currency_code": "CAD"}})
    order = _order_json(payment_gateway_names=["paypal"], line_items=[item],
                         current_total_price_set=_ARBITRARY_ORDER_TOTAL)
    rows = build_brakex_order_rows(order, _fo_info(), "LOW", _settings(), datetime.date(2024, 1, 1))
    assert rows[0]["Items_Amount"] == 37.50


def test_paypal_item_amount_cad_presentment_with_cad_discount():
    # price_presentment(CAD)=25.00, discount presentment_money CAD=5.00, qty=2:
    # item_amount = 25.00 - 5.00/2 = 25.00 - 2.50 = 22.50
    item = _paypal_line_item("CAD", "25.00", {"presentment_money": {"amount": "5.00", "currency_code": "CAD"}})
    order = _order_json(payment_gateway_names=["paypal"], line_items=[item],
                         current_total_price_set=_ARBITRARY_ORDER_TOTAL)
    rows = build_brakex_order_rows(order, _fo_info(), "LOW", _settings(), datetime.date(2024, 1, 1))
    assert rows[0]["Items_Amount"] == 22.50


def test_paypal_item_amount_cad_presentment_falls_back_to_usd_discount_times_rate():
    # No "CAD" key in discount_dict (only a shop_money USD entry) -> falls
    # back to the USD discount * rate.
    # item_amount = 25.00 - (3.00*1.35)/2 = 25.00 - 4.05/2 = 25.00 - 2.025
    #             = 22.975 -> round(22.975, 2) = 22.98 (independently
    #             verified: python's round() on this exact float value).
    item = _paypal_line_item("CAD", "25.00", {"shop_money": {"amount": "3.00", "currency_code": "USD"}})
    order = _order_json(payment_gateway_names=["paypal"], line_items=[item],
                         current_total_price_set=_ARBITRARY_ORDER_TOTAL)
    rows = build_brakex_order_rows(order, _fo_info(), "LOW", _settings(), datetime.date(2024, 1, 1))
    assert rows[0]["Items_Amount"] == 22.98


def test_paypal_item_amount_no_discount_allocations_uses_empty_discount_dict():
    # discount_allocations=None/[] -> discount_dict={"CAD":0,"USD":0} -> no
    # discount applied regardless of branch.
    item = _paypal_line_item("USD", "30.00", None)
    order = _order_json(payment_gateway_names=["paypal"], line_items=[item],
                         current_total_price_set=_ARBITRARY_ORDER_TOTAL)
    rows = build_brakex_order_rows(order, _fo_info(), "LOW", _settings(), datetime.date(2024, 1, 1))
    assert rows[0]["Items_Amount"] == round(30.00 * 1.35, 2) == 40.50


# ---------------------------------------------------------------------------
# Review item 1: missing presentment amount (order-level OR item-level) ->
# OrderBuildError, the order is skipped (never silently written as 0.0).
# ---------------------------------------------------------------------------
def test_paypal_missing_order_level_presentment_amount_raises():
    item = _paypal_line_item("USD", "30.00", None)
    order = _order_json(
        payment_gateway_names=["paypal"], line_items=[item],
        current_total_price_set={"presentment_money": {"currency_code": "USD"}},  # no "amount" key
    )
    with pytest.raises(OrderBuildError):
        build_brakex_order_rows(order, _fo_info(), "LOW", _settings(), datetime.date(2024, 1, 1))


def test_paypal_missing_order_level_presentment_money_entirely_raises():
    item = _paypal_line_item("USD", "30.00", None)
    order = _order_json(
        payment_gateway_names=["paypal"], line_items=[item],
        current_total_price_set={},  # no presentment_money at all
    )
    with pytest.raises(OrderBuildError):
        build_brakex_order_rows(order, _fo_info(), "LOW", _settings(), datetime.date(2024, 1, 1))


def test_paypal_missing_item_level_presentment_amount_raises():
    item = _paypal_line_item("USD", "30.00", None)
    del item["price_set"]["presentment_money"]["amount"]  # amount missing, currency present
    order = _order_json(payment_gateway_names=["paypal"], line_items=[item],
                         current_total_price_set=_ARBITRARY_ORDER_TOTAL)
    with pytest.raises(OrderBuildError):
        build_brakex_order_rows(order, _fo_info(), "LOW", _settings(), datetime.date(2024, 1, 1))


def test_paypal_missing_item_level_price_set_entirely_raises():
    item = _paypal_line_item("USD", "30.00", None)
    del item["price_set"]
    order = _order_json(payment_gateway_names=["paypal"], line_items=[item],
                         current_total_price_set=_ARBITRARY_ORDER_TOTAL)
    with pytest.raises(OrderBuildError):
        build_brakex_order_rows(order, _fo_info(), "LOW", _settings(), datetime.date(2024, 1, 1))


def test_pull_brakex_paypal_order_with_missing_presentment_amount_is_skipped_logged_not_inserted(monkeypatch, caplog):
    insert_calls = []
    _patch_db_mod(monkeypatch, insert_calls)

    item = _paypal_line_item("USD", "30.00", None)
    bad_order = _order_json(
        id=9, order_number=3009, created_at=_today_iso(), processed_at=_today_iso(),
        payment_gateway_names=["paypal"], line_items=[item],
        current_total_price_set={"presentment_money": {"currency_code": "USD"}},  # missing amount
    )
    detail = {"9": {"riskLevel": "LOW", "fulfillmentOrders": {"edges": [
        {"node": {"id": "gid://shopify/FulfillmentOrder/FO1",
                  "lineItems": {"edges": [{"node": {"id": "gid://shopify/FulfillmentOrderLineItem/LI1", "sku": "SKU-A"}}]}}}
    ]}}}
    client = _FakeClient([bad_order], detail)

    with caplog.at_level("ERROR"):
        result = pull_brakex_open_orders(client, _FakeDb(), _settings())

    assert result["errors"] == 1
    assert result["inserted"] == 0
    assert len(insert_calls) == 0


# ---------------------------------------------------------------------------
# R15 / V7 / OrderBuildError
# ---------------------------------------------------------------------------
def test_zero_current_quantity_line_is_dropped():
    order = _order_json(line_items=[
        {"id": 1, "sku": "SKU-A", "quantity": 2, "current_quantity": 0,
         "price": "25.00", "title": "Widget", "product_id": 777, "discount_allocations": []},
        {"id": 2, "sku": "SKU-B", "quantity": 1, "current_quantity": 1,
         "price": "5.00", "title": "Gadget", "product_id": 778, "discount_allocations": []},
    ])
    rows = build_brakex_order_rows(order, _fo_info(line_item_map={"SKU-A": "LI1", "SKU-B": "LI2"}),
                                    "LOW", _settings(), datetime.date(2024, 1, 1))
    assert len(rows) == 1
    assert rows[0]["Items_Name"] == "SKU-B"


def test_all_zero_quantity_lines_returns_empty_list():
    order = _order_json(line_items=[
        {"id": 1, "sku": "SKU-A", "quantity": 2, "current_quantity": 0,
         "price": "25.00", "title": "Widget", "product_id": 777, "discount_allocations": []},
    ])
    rows = build_brakex_order_rows(order, _fo_info(), "LOW", _settings(), datetime.date(2024, 1, 1))
    assert rows == []


def test_unmapped_sku_raises_order_build_error():
    order = _order_json(line_items=[{
        "id": 1, "sku": "UNKNOWN-SKU", "quantity": 1, "current_quantity": 1,
        "price": "1.00", "title": "X", "product_id": 1, "discount_allocations": [],
    }])
    with pytest.raises(OrderBuildError):
        build_brakex_order_rows(order, _fo_info(), "LOW", _settings(), datetime.date(2024, 1, 1))


# ---------------------------------------------------------------------------
# pull_brakex_open_orders
# ---------------------------------------------------------------------------
class _FakeConn:
    def close(self):
        pass


class _FakeDb:
    def connect(self):
        return _FakeConn()


class _FakeClient:
    def __init__(self, pages, detail_by_order_id):
        self._pages = pages
        self._detail_by_order_id = detail_by_order_id

    def rest_get_paginated(self, path, params=None):
        yield {"orders": self._pages}

    def graphql(self, query, variables=None):
        gid = variables["id"]
        order_id = gid.rsplit("/", 1)[-1]
        if "riskLevel" in query and "fulfillmentOrders" in query:
            return {"order": self._detail_by_order_id[order_id]}
        raise AssertionError("unexpected graphql query: %s" % query)


def _patch_db_mod(monkeypatch, insert_calls, existing_ids=()):
    import rvpartstore.brakex_orders as brakex_mod

    def fake_run_query(conn, query, params=None):
        return [(i,) for i in existing_ids], ["shopifyOrderId"]

    def fake_run_write(conn, query, params=None, many=False):
        insert_calls.append(params)
        return len(params or [])

    monkeypatch.setattr(brakex_mod.db_mod, "run_query", fake_run_query)
    monkeypatch.setattr(brakex_mod.db_mod, "run_write", fake_run_write)


def _today_iso():
    return datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S-05:00")


def test_pull_brakex_skips_existing_blocklisted_and_old_orders(monkeypatch):
    insert_calls = []

    order_existing = _order_json(id=1, order_number=3001, created_at=_today_iso(), processed_at=_today_iso())
    order_blocklisted = _order_json(id=2, order_number=3002, created_at=_today_iso(), processed_at=_today_iso())
    order_too_old = _order_json(
        id=3, order_number=3003,
        created_at=(datetime.datetime.now() - datetime.timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%S-05:00"),
        processed_at=_today_iso(),
    )
    order_valid = _order_json(id=4, order_number=3004, created_at=_today_iso(), processed_at=_today_iso())
    order_valid["financial_status"] = "paid"

    _patch_db_mod(monkeypatch, insert_calls, existing_ids=("1",))

    detail = {"4": {"riskLevel": "LOW", "fulfillmentOrders": {"edges": [
        {"node": {"id": "gid://shopify/FulfillmentOrder/FO1",
                  "lineItems": {"edges": [{"node": {"id": "gid://shopify/FulfillmentOrderLineItem/LI1", "sku": "SKU-A"}}]}}}
    ]}}}
    client = _FakeClient([order_existing, order_blocklisted, order_too_old, order_valid], detail)

    result = pull_brakex_open_orders(
        client, _FakeDb(), _settings(brakex_order_id_blocklist=("2",))
    )

    assert result["inserted"] == 1
    assert len(insert_calls) == 1
    assert insert_calls[0][0][0] == "BRX_SP_373004"


def test_pull_brakex_r10_missing_shipping_address_skipped(monkeypatch, caplog):
    insert_calls = []
    _patch_db_mod(monkeypatch, insert_calls)

    order_missing_shipping = _order_json(
        id=5, order_number=3005, created_at=_today_iso(), processed_at=_today_iso(), shipping_address=None,
    )
    detail = {"5": {"riskLevel": "LOW", "fulfillmentOrders": {"edges": []}}}
    client = _FakeClient([order_missing_shipping], detail)

    with caplog.at_level("ERROR"):
        result = pull_brakex_open_orders(client, _FakeDb(), _settings())

    assert result["errors"] == 1
    assert len(insert_calls) == 0
    assert any("protected customer data" in r.message for r in caplog.records)


def test_pull_brakex_dry_run_writes_csv_no_insert(monkeypatch, tmp_path):
    insert_calls = []
    _patch_db_mod(monkeypatch, insert_calls)

    order_valid = _order_json(id=6, order_number=3006, created_at=_today_iso(), processed_at=_today_iso())
    order_valid["financial_status"] = "paid"
    detail = {"6": {"riskLevel": "LOW", "fulfillmentOrders": {"edges": [
        {"node": {"id": "gid://shopify/FulfillmentOrder/FO1",
                  "lineItems": {"edges": [{"node": {"id": "gid://shopify/FulfillmentOrderLineItem/LI1", "sku": "SKU-A"}}]}}}
    ]}}}
    client = _FakeClient([order_valid], detail)

    settings = _settings(dry_run=True, project_root=str(tmp_path))
    result = pull_brakex_open_orders(client, _FakeDb(), settings)

    assert result["inserted"] == 1
    assert len(insert_calls) == 0  # no DB insert in dry run
    import glob
    csvs = glob.glob(str(tmp_path / "data" / "dry_run" / "*" / "brakex_orders.csv"))
    assert len(csvs) == 1


# ---------------------------------------------------------------------------
# Review item 1: per-order isolation for ANY unexpected exception. Review
# item 6: an order whose every line has current_quantity==0 counts as a
# (non-error) skip.
# ---------------------------------------------------------------------------
def test_pull_brakex_isolates_unexpected_exception_and_keeps_processing(monkeypatch, caplog):
    insert_calls = []
    _patch_db_mod(monkeypatch, insert_calls)

    # Missing "order_number" entirely -> build_brakex_order_rows raises a
    # bare KeyError (NOT OrderBuildError); must still be isolated per-order.
    order_bad = _order_json(id=20, created_at=_today_iso(), processed_at=_today_iso())
    del order_bad["order_number"]
    order_bad["financial_status"] = "paid"

    order_ok = _order_json(id=21, order_number=3021, created_at=_today_iso(), processed_at=_today_iso())
    order_ok["financial_status"] = "paid"

    fo_edges = [{"node": {"id": "gid://shopify/FulfillmentOrder/FO1",
                           "lineItems": {"edges": [{"node": {"id": "gid://shopify/FulfillmentOrderLineItem/LI1", "sku": "SKU-A"}}]}}}]
    detail = {
        "20": {"riskLevel": "LOW", "fulfillmentOrders": {"edges": fo_edges}},
        "21": {"riskLevel": "LOW", "fulfillmentOrders": {"edges": fo_edges}},
    }
    client = _FakeClient([order_bad, order_ok], detail)

    with caplog.at_level("ERROR"):
        result = pull_brakex_open_orders(client, _FakeDb(), _settings())

    assert result["errors"] == 1
    assert result["inserted"] == 1  # order 21 still processed despite order 20's crash
    assert len(insert_calls) == 1
    assert insert_calls[0][0][0] == "BRX_SP_373021"
    assert any("unexpected error" in r.message for r in caplog.records)


def test_pull_brakex_all_zero_quantity_lines_counts_as_skipped_not_error(monkeypatch):
    insert_calls = []
    _patch_db_mod(monkeypatch, insert_calls)

    order_all_zero = _order_json(
        id=22, order_number=3022, created_at=_today_iso(), processed_at=_today_iso(),
        line_items=[{
            "id": 1, "sku": "SKU-A", "quantity": 2, "current_quantity": 0,
            "price": "10.00", "title": "Widget", "product_id": 777, "discount_allocations": [],
        }],
    )
    detail = {"22": {"riskLevel": "LOW", "fulfillmentOrders": {"edges": [
        {"node": {"id": "gid://shopify/FulfillmentOrder/FO1",
                  "lineItems": {"edges": [{"node": {"id": "gid://shopify/FulfillmentOrderLineItem/LI1", "sku": "SKU-A"}}]}}}
    ]}}}
    client = _FakeClient([order_all_zero], detail)

    result = pull_brakex_open_orders(client, _FakeDb(), _settings())

    assert result["skipped"] == 1
    assert result["errors"] == 0
    assert result["inserted"] == 0
    assert len(insert_calls) == 0

"""Black-box tests for rvpartstore.orders (design spec sec. 8; R4, R10, R15, V7).

Contract under test:
  * build_order_rows: TxnID = TXN_PREFIX + order_number; StoreType = the
    given store_type; 27 explicit output columns; item_amount = price -
    discount/ORIGINAL quantity (V7); Items_Quantity = current_quantity with
    fallback to quantity (V7); riskLevel defaults to 'LOW', never null;
    R15 lines with 0 current_quantity are skipped, and an order with no
    remaining lines returns [].
  * Promise_Date: vendor -> days table (IB1 CW5 ME3 KS1 MS2 TF5 PA5 CBK3 PR3
    WE5 backorder28, default 4) with the documented weekend adjustment.
  * pull_open_orders: R10 orders with missing/empty shipping name or
    address1 are skipped (logged ERROR, not inserted, retried next run).
"""
import datetime
import types

import pytest

from rvpartstore.orders import build_order_rows, pull_open_orders, OrderBuildError, _fetch_order_detail
from rvpartstore.shopify_client import ShopifyError


EXPECTED_ORDER_COLUMNS = [
    "TxnID", "shopifyOrderId", "itemId", "fulfillmentOrderId", "fulfillmentOrderLineItemId",
    "product_id", "Items_Name", "Items_Desc", "Items_Quantity", "Items_Amount", "Items_Shipping",
    "StoreType", "company", "To_Name", "To_Street", "To_Street2", "To_State", "To_City",
    "To_ZipCode", "To_CountryCode", "To_PhoneNumber", "email", "Total_Total", "Details_PaymentDate",
    "PaymentStatus", "riskLevel", "Promise_Date",
]


def _order_json(**overrides):
    base = {
        "id": 900100200,
        "order_number": 1050,
        "email": "buyer@example.com",
        "total_price": "129.98",
        "processed_at": "2024-01-01T10:00:00-05:00",
        "created_at": "2024-01-01T10:00:00-05:00",
        "shipping_address": {
            "company": "Acme RV",
            "name": "Jane Doe",
            "address1": "123 Main St",
            "address2": "Unit 4",
            "province_code": "ON",
            "city": "Toronto",
            "zip": "M1M1M1",
            "country_code": "CA",
            "phone": "4165551234",
        },
        "line_items": [{
            "id": 1, "sku": "SKU-A", "quantity": 2, "current_quantity": 2,
            "price": "19.99", "title": "Widget", "product_id": 777,
            "discount_allocations": [],
        }],
    }
    base.update(overrides)
    return base


def _fo_info(**overrides):
    base = {
        "risk_level": "LOW",
        "fulfillment_order_ids": ["FO1"],
        "line_item_map": {"SKU-A": "LI1"},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# build_order_rows
# ---------------------------------------------------------------------------
def test_txn_id_format():
    rows = build_order_rows(_order_json(order_number=1050), _fo_info(), {}, datetime.date(2024, 1, 1))
    assert rows[0]["TxnID"] == "RVM_SP_N1050"


def test_store_type_and_default():
    rows = build_order_rows(_order_json(), _fo_info(), {}, datetime.date(2024, 1, 1))
    assert rows[0]["StoreType"] == "shopifyca_rv"
    rows2 = build_order_rows(_order_json(), _fo_info(), {}, datetime.date(2024, 1, 1), store_type="custom_store")
    assert rows2[0]["StoreType"] == "custom_store"


def test_exactly_27_columns_matching_the_documented_table():
    rows = build_order_rows(_order_json(), _fo_info(), {}, datetime.date(2024, 1, 1))
    assert len(EXPECTED_ORDER_COLUMNS) == 27
    assert set(rows[0].keys()) == set(EXPECTED_ORDER_COLUMNS)


def test_risk_level_defaults_to_low_never_null():
    rows = build_order_rows(_order_json(), _fo_info(risk_level=None), {}, datetime.date(2024, 1, 1))
    assert rows[0]["riskLevel"] == "LOW"
    assert rows[0]["riskLevel"] is not None


def test_shipping_fields_mapped_from_shipping_address():
    rows = build_order_rows(_order_json(), _fo_info(), {}, datetime.date(2024, 1, 1))
    row = rows[0]
    assert row["company"] == "Acme RV"
    assert row["To_Name"] == "Jane Doe"
    assert row["To_Street"] == "123 Main St"
    assert row["To_Street2"] == "Unit 4"
    assert row["To_State"] == "ON"
    assert row["To_City"] == "Toronto"
    assert row["To_ZipCode"] == "M1M1M1"
    assert row["To_CountryCode"] == "CA"
    assert row["To_PhoneNumber"] == "4165551234"
    assert row["email"] == "buyer@example.com"
    assert row["Total_Total"] == "129.98"


def test_items_amount_discount_uses_original_quantity_v7():
    # price=19.99, ORIGINAL quantity=3, current_quantity=2 (partially
    # fulfilled/refunded), discount_allocations amount=6.00 ->
    # item_amount = round(19.99 - 6.00/3, 2) = round(19.99 - 2.00, 2) = 17.99
    # Items_Quantity must be the CURRENT quantity (2), not original (3).
    order = _order_json(line_items=[{
        "id": 1, "sku": "SKU-A", "quantity": 3, "current_quantity": 2,
        "price": "19.99", "title": "Widget", "product_id": 777,
        "discount_allocations": [{"amount": "6.00"}],
    }])
    rows = build_order_rows(order, _fo_info(), {}, datetime.date(2024, 1, 1))
    assert rows[0]["Items_Amount"] == 17.99
    assert rows[0]["Items_Quantity"] == 2


def test_items_amount_zero_when_no_discount_allocations():
    order = _order_json(line_items=[{
        "id": 1, "sku": "SKU-A", "quantity": 2, "current_quantity": 2,
        "price": "19.99", "title": "Widget", "product_id": 777,
        "discount_allocations": [],
    }])
    rows = build_order_rows(order, _fo_info(), {}, datetime.date(2024, 1, 1))
    assert rows[0]["Items_Amount"] == 19.99


def test_items_quantity_falls_back_to_original_quantity_when_current_missing():
    order = _order_json(line_items=[{
        "id": 1, "sku": "SKU-A", "quantity": 5, "price": "10.00", "title": "Widget",
        "product_id": 777, "discount_allocations": [],
        # no "current_quantity" key at all
    }])
    rows = build_order_rows(order, _fo_info(), {}, datetime.date(2024, 1, 1))
    assert rows[0]["Items_Quantity"] == 5


def test_r15_line_with_zero_current_quantity_is_skipped():
    order = _order_json(line_items=[
        {"id": 1, "sku": "SKU-A", "quantity": 2, "current_quantity": 0,
         "price": "19.99", "title": "Widget", "product_id": 777, "discount_allocations": []},
        {"id": 2, "sku": "SKU-B", "quantity": 1, "current_quantity": 1,
         "price": "5.00", "title": "Gadget", "product_id": 778, "discount_allocations": []},
    ])
    rows = build_order_rows(order, _fo_info(line_item_map={"SKU-A": "LI1", "SKU-B": "LI2"}),
                             {}, datetime.date(2024, 1, 1))
    assert len(rows) == 1
    assert rows[0]["Items_Name"] == "SKU-B"


def test_r15_order_with_all_zero_quantity_lines_returns_empty_list():
    order = _order_json(line_items=[
        {"id": 1, "sku": "SKU-A", "quantity": 2, "current_quantity": 0,
         "price": "19.99", "title": "Widget", "product_id": 777, "discount_allocations": []},
    ])
    rows = build_order_rows(order, _fo_info(), {}, datetime.date(2024, 1, 1))
    assert rows == []


def test_raises_order_build_error_when_sku_missing_from_line_item_map():
    order = _order_json(line_items=[{
        "id": 1, "sku": "UNKNOWN-SKU", "quantity": 1, "current_quantity": 1,
        "price": "1.00", "title": "X", "product_id": 1, "discount_allocations": [],
    }])
    with pytest.raises(OrderBuildError):
        build_order_rows(order, _fo_info(), {}, datetime.date(2024, 1, 1))


def test_fulfillment_order_id_joins_all_fo_ids_with_pipe():
    rows = build_order_rows(
        _order_json(), _fo_info(fulfillment_order_ids=["FO1", "FO2", "FO3"]), {}, datetime.date(2024, 1, 1)
    )
    assert rows[0]["fulfillmentOrderId"] == "FO1|FO2|FO3"


# ---------------------------------------------------------------------------
# Promise_Date
# ---------------------------------------------------------------------------
def test_promise_date_ks_vendor_1_day_monday():
    # today = Monday 2024-01-01 (ISO weekday 1), vendor KS -> fulfillment_time=1
    # (<5 branch): fulfillment_index of (Mon+1=Tue)=2; 2<=1? no; 2>=6? no ->
    # stays 1 day. order processed same day -> promise = 2024-01-02.
    order = _order_json(processed_at="2024-01-01T09:00:00-05:00", product_id_vendor_map=None)
    rows = build_order_rows(order, _fo_info(), {777: "KS"}, datetime.date(2024, 1, 1))
    assert rows[0]["Promise_Date"] == "2024-01-02"


def test_promise_date_missing_vendor_defaults_to_4_days_with_weekend_adjust():
    # today = Thursday 2024-01-04 (ISO weekday 4), vendor missing -> default
    # fulfillment_time=4 (<5 branch): fulfillment_index of (Thu+4=Mon)=1;
    # 1<=4 -> true -> +2 -> 6 days. promise = 2024-01-04 + 6 = 2024-01-10.
    order = _order_json(processed_at="2024-01-04T09:00:00-05:00")
    rows = build_order_rows(order, _fo_info(), {}, datetime.date(2024, 1, 4))
    assert rows[0]["Promise_Date"] == "2024-01-10"


def test_promise_date_cw_vendor_5_day_branch_ignores_weekday():
    # CW -> fulfillment_time=5 (>=5 branch): += (5//5)*2 = +2 -> 7 days,
    # regardless of weekday. order_date=2024-01-01 -> promise=2024-01-08.
    order = _order_json(processed_at="2024-01-01T09:00:00-05:00")
    rows = build_order_rows(order, _fo_info(), {777: "CW"}, datetime.date(2024, 1, 1))
    assert rows[0]["Promise_Date"] == "2024-01-08"


def test_promise_date_backorder_vendor_28_days():
    # backorder -> fulfillment_time=28 (>=5 branch): += (28//5)*2 = +10 -> 38
    # days. order_date=2024-01-01 -> promise = 2024-01-01 + 38 = 2024-02-08.
    order = _order_json(processed_at="2024-01-01T09:00:00-05:00")
    rows = build_order_rows(order, _fo_info(), {777: "backorder"}, datetime.date(2024, 1, 1))
    assert rows[0]["Promise_Date"] == "2024-02-08"


def test_promise_date_ms_vendor_lands_on_weekend_gets_bumped():
    # today = Friday 2024-01-05 (ISO weekday 5), vendor MS -> fulfillment_time=2
    # (<5 branch): fulfillment_index of (Fri+2=Sun)=7; 7<=5? no; 7>=6? yes ->
    # +2 -> 4 days. order_date=2024-01-05 -> promise=2024-01-09.
    order = _order_json(processed_at="2024-01-05T09:00:00-05:00")
    rows = build_order_rows(order, _fo_info(), {777: "MS"}, datetime.date(2024, 1, 5))
    assert rows[0]["Promise_Date"] == "2024-01-09"


# ---------------------------------------------------------------------------
# pull_open_orders: R10 protected-customer-data guard
# ---------------------------------------------------------------------------
class _FakeConn:
    def close(self):
        pass


class _FakeDb:
    def connect(self):
        return _FakeConn()


class _FakeClient:
    """Dispatches graphql() by inspecting the query text; rest_get_paginated()
    returns a single scripted page."""

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
        if "metafield" in query:
            return {"product": None}  # no vendor metafield -> default fulfillment days
        raise AssertionError("unexpected graphql query: %s" % query)


def _settings(**overrides):
    base = dict(
        order_table="shopify_rvmarines_order", order_max_age_days=60,
        order_id_blocklist=(), dry_run=False, txn_prefix="RVM_SP_N", store_type="shopifyca_rv",
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


def _patch_db_mod(monkeypatch, insert_calls):
    import rvpartstore.orders as orders_mod

    def fake_run_query(conn, query, params=None):
        return [], []  # no existing order ids

    def fake_run_write(conn, query, params=None, many=False):
        insert_calls.append(params)
        return len(params or [])

    monkeypatch.setattr(orders_mod.db_mod, "run_query", fake_run_query)
    monkeypatch.setattr(orders_mod.db_mod, "run_write", fake_run_write)


def _today_iso():
    return datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S-05:00")


def test_r10_order_missing_shipping_address_is_skipped_not_inserted(monkeypatch, caplog):
    insert_calls = []
    _patch_db_mod(monkeypatch, insert_calls)

    order_ok = _order_json(id=1, order_number=2001, created_at=_today_iso(), processed_at=_today_iso())
    order_ok["financial_status"] = "paid"

    order_missing_shipping = _order_json(
        id=2, order_number=2002, created_at=_today_iso(), processed_at=_today_iso(),
        shipping_address=None,
    )
    order_missing_shipping["financial_status"] = "paid"

    detail = {
        "1": {"riskLevel": "LOW", "fulfillmentOrders": {"edges": [
            {"node": {"id": "gid://shopify/FulfillmentOrder/FO1",
                      "lineItems": {"edges": [{"node": {"id": "gid://shopify/FulfillmentOrderLineItem/LI1", "sku": "SKU-A"}}]}}}
        ]}},
        "2": {"riskLevel": "LOW", "fulfillmentOrders": {"edges": []}},
    }
    client = _FakeClient([order_ok, order_missing_shipping], detail)

    with caplog.at_level("ERROR"):
        result = pull_open_orders(client, _FakeDb(), _settings(), None)

    assert result["errors"] >= 1
    assert len(insert_calls) == 1  # only the valid order was inserted
    assert len(insert_calls[0]) == 1  # one row, for order 1's single line item
    assert insert_calls[0][0][0] == "RVM_SP_N2001"  # TxnID of the inserted row
    assert any("protected customer data" in r.message for r in caplog.records)


def test_r10_order_with_empty_address1_is_skipped(monkeypatch, caplog):
    insert_calls = []
    _patch_db_mod(monkeypatch, insert_calls)

    order_bad_address = _order_json(
        id=3, order_number=2003, created_at=_today_iso(), processed_at=_today_iso(),
        shipping_address={"name": "Jane Doe", "address1": "", "company": None, "address2": None,
                           "province_code": None, "city": None, "zip": None, "country_code": None, "phone": None},
    )
    order_bad_address["financial_status"] = "paid"
    detail = {"3": {"riskLevel": "LOW", "fulfillmentOrders": {"edges": []}}}
    client = _FakeClient([order_bad_address], detail)

    with caplog.at_level("ERROR"):
        result = pull_open_orders(client, _FakeDb(), _settings(), None)

    assert result["errors"] == 1
    assert len(insert_calls) == 0


# ---------------------------------------------------------------------------
# Review item 1: per-order isolation for ANY unexpected exception (not just
# the specifically-anticipated ones). Review item 6: an order whose every
# line has current_quantity==0 counts as a (non-error) skip.
# ---------------------------------------------------------------------------
def test_pull_open_orders_isolates_unexpected_exception_and_keeps_processing(monkeypatch, caplog):
    insert_calls = []
    _patch_db_mod(monkeypatch, insert_calls)

    # Missing "order_number" entirely -> build_order_rows raises a bare
    # KeyError (NOT OrderBuildError), which must still be caught by the
    # per-order isolation wrapper, not escape and abort the whole run.
    order_bad = _order_json(id=10, created_at=_today_iso(), processed_at=_today_iso())
    del order_bad["order_number"]
    order_bad["financial_status"] = "paid"

    order_ok = _order_json(id=11, order_number=2011, created_at=_today_iso(), processed_at=_today_iso())
    order_ok["financial_status"] = "paid"

    detail = {
        "10": {"riskLevel": "LOW", "fulfillmentOrders": {"edges": [
            {"node": {"id": "gid://shopify/FulfillmentOrder/FO1",
                      "lineItems": {"edges": [{"node": {"id": "gid://shopify/FulfillmentOrderLineItem/LI1", "sku": "SKU-A"}}]}}}
        ]}},
        "11": {"riskLevel": "LOW", "fulfillmentOrders": {"edges": [
            {"node": {"id": "gid://shopify/FulfillmentOrder/FO1",
                      "lineItems": {"edges": [{"node": {"id": "gid://shopify/FulfillmentOrderLineItem/LI1", "sku": "SKU-A"}}]}}}
        ]}},
    }
    client = _FakeClient([order_bad, order_ok], detail)

    with caplog.at_level("ERROR"):
        result = pull_open_orders(client, _FakeDb(), _settings(), None)

    assert result["errors"] == 1
    assert result["inserted"] == 1  # order 11 still processed despite order 10's crash
    assert len(insert_calls) == 1
    assert insert_calls[0][0][0] == "RVM_SP_N2011"
    assert any("unexpected error" in r.message for r in caplog.records)


def test_pull_open_orders_all_zero_quantity_lines_counts_as_skipped_not_error(monkeypatch):
    insert_calls = []
    _patch_db_mod(monkeypatch, insert_calls)

    order_all_zero = _order_json(
        id=12, order_number=2012, created_at=_today_iso(), processed_at=_today_iso(),
        line_items=[{
            "id": 1, "sku": "SKU-A", "quantity": 2, "current_quantity": 0,
            "price": "10.00", "title": "Widget", "product_id": 777, "discount_allocations": [],
        }],
    )
    detail = {"12": {"riskLevel": "LOW", "fulfillmentOrders": {"edges": [
        {"node": {"id": "gid://shopify/FulfillmentOrder/FO1",
                  "lineItems": {"edges": [{"node": {"id": "gid://shopify/FulfillmentOrderLineItem/LI1", "sku": "SKU-A"}}]}}}
    ]}}}
    client = _FakeClient([order_all_zero], detail)

    result = pull_open_orders(client, _FakeDb(), _settings(), None)

    assert result["skipped"] == 1
    assert result["errors"] == 0
    assert result["inserted"] == 0
    assert len(insert_calls) == 0


# ---------------------------------------------------------------------------
# Review item 3: _fetch_order_detail (shared by orders.py and
# brakex_orders.py) — a null `order` in the GraphQL response must raise
# ShopifyError (never a raw TypeError from `.get()` on None), and a null
# riskLevel must trigger the risk.assessments fallback query.
# ---------------------------------------------------------------------------
class _ScriptedGraphqlClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def graphql(self, query, variables=None):
        self.calls.append(query)
        return self._responses.pop(0)


_FO_EDGES = [{"node": {"id": "gid://shopify/FulfillmentOrder/FO1",
                        "lineItems": {"edges": [{"node": {"id": "gid://shopify/FulfillmentOrderLineItem/LI1", "sku": "SKU-A"}}]}}}]


def test_fetch_order_detail_primary_riskLevel_present_no_fallback_call():
    client = _ScriptedGraphqlClient([
        {"order": {"id": "gid://shopify/Order/1", "riskLevel": "MEDIUM", "fulfillmentOrders": {"edges": _FO_EDGES}}},
    ])
    result = _fetch_order_detail(client, "1")
    assert result["risk_level"] == "MEDIUM"
    assert len(client.calls) == 1  # no fallback query needed


def test_fetch_order_detail_primary_null_order_falls_back_without_typeerror():
    client = _ScriptedGraphqlClient([
        {"order": None},  # primary query: order is null
        {"order": {"id": "gid://shopify/Order/1", "risk": {"assessments": [{"riskLevel": "HIGH"}]},
                    "fulfillmentOrders": {"edges": _FO_EDGES}}},
    ])
    result = _fetch_order_detail(client, "1")  # must not raise TypeError
    assert result["risk_level"] == "HIGH"
    assert len(client.calls) == 2


def test_fetch_order_detail_primary_null_risklevel_triggers_fallback():
    client = _ScriptedGraphqlClient([
        {"order": {"id": "gid://shopify/Order/1", "riskLevel": None, "fulfillmentOrders": {"edges": _FO_EDGES}}},
        {"order": {"id": "gid://shopify/Order/1", "risk": {"assessments": [{"riskLevel": "LOW"}, {"riskLevel": "MEDIUM"}]},
                    "fulfillmentOrders": {"edges": _FO_EDGES}}},
    ])
    result = _fetch_order_detail(client, "1")
    assert result["risk_level"] == "MEDIUM"  # highest of [LOW, MEDIUM]
    assert len(client.calls) == 2


def test_fetch_order_detail_both_queries_null_order_raises_shopify_error_not_typeerror():
    client = _ScriptedGraphqlClient([
        {"order": None},
        {"order": None},
    ])
    with pytest.raises(ShopifyError):
        _fetch_order_detail(client, "1")


def test_fetch_order_detail_entirely_null_data_response_raises_shopify_error():
    # data=None (e.g. a top-level "data": null) must also not crash with
    # AttributeError/TypeError -- `(data or {}).get("order")` handles it.
    client = _ScriptedGraphqlClient([None, None])
    with pytest.raises(ShopifyError):
        _fetch_order_detail(client, "1")

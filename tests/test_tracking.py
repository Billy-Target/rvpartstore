"""Black-box tests for rvpartstore.tracking (design spec sec. 9; R1, R2, V1, V2).

Contract under test:
  * Routing happens BEFORE settings are loaded, against the hard-coded
    NEW_STORE_PREFIX constant ("RVM_SP_N"): legacy order_ids delegate to the
    caller's own "shopify_tracking" module (importlib.import_module), with
    NO try/except around the delegated call itself (V1) -- only ImportError
    of the legacy module is caught (-> log ERROR, return False).
  * build_fulfillment_input: groups by the fulfillment order Shopify actually
    returned (not the stored possibly-'a|b' column), quantity = min(stored
    qty, remainingQuantity), rows with nothing remaining are skipped.
"""
import sys
import types

import pytest

import rvpartstore.tracking as tracking_mod
from rvpartstore.tracking import NEW_STORE_PREFIX, build_fulfillment_input, ship_orders


# ---------------------------------------------------------------------------
# V1: routing decided before settings load; legacy delegation
# ---------------------------------------------------------------------------
@pytest.fixture
def block_new_store_settings(monkeypatch):
    """If the legacy path accidentally touches settings/client construction,
    fail loudly instead of silently doing real (network/DB) work."""
    def _boom():
        raise AssertionError("settings/client must not be constructed on the legacy routing path")
    monkeypatch.setattr(tracking_mod, "_get_client_and_settings", _boom)


def test_legacy_order_id_delegates_without_touching_settings(monkeypatch, block_new_store_settings):
    calls = []

    def fake_ship_orders(order_id, sku, tracking, carrier):
        calls.append((order_id, sku, tracking, carrier))
        return True

    fake_module = types.SimpleNamespace(ship_orders=fake_ship_orders)
    monkeypatch.setitem(sys.modules, "shopify_tracking", fake_module)

    result = ship_orders("RVM_SP_3801", "SKU-1", "1Z999", "UPS")

    assert result is True
    assert calls == [("RVM_SP_3801", "SKU-1", "1Z999", "UPS")]


def test_legacy_order_id_propagates_exceptions_uncaught(monkeypatch, block_new_store_settings):
    # V1: the legacy path is explicitly NOT wrapped in try/except -- only
    # ImportError of the module itself is caught. A real exception from the
    # legacy ship_orders must propagate to the caller.
    def boom(order_id, sku, tracking, carrier):
        raise ValueError("legacy DB exploded")

    fake_module = types.SimpleNamespace(ship_orders=boom)
    monkeypatch.setitem(sys.modules, "shopify_tracking", fake_module)

    with pytest.raises(ValueError, match="legacy DB exploded"):
        ship_orders("RVM_SP_3801", "", "1Z999", "UPS")


def test_legacy_order_id_import_error_logged_and_returns_false(monkeypatch, block_new_store_settings, caplog):
    monkeypatch.delitem(sys.modules, "shopify_tracking", raising=False)
    # Ensure it's genuinely not importable in this environment.
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "shopify_tracking":
            raise ImportError("no module named shopify_tracking")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    with caplog.at_level("ERROR"):
        result = ship_orders("RVM_SP_3801", "", "1Z999", "UPS")

    assert result is False
    assert any("shopify_tracking" in r.message for r in caplog.records)


def test_new_store_prefix_constant_matches_txn_prefix_format():
    assert NEW_STORE_PREFIX == "RVM_SP_N"


# ---------------------------------------------------------------------------
# build_fulfillment_input (pure)
# ---------------------------------------------------------------------------
def test_groups_by_fulfillment_order_returned_by_shopify_not_stored_column():
    # The stored 'fulfillmentOrderId' column (here irrelevant/absent from
    # fo_lookup keys) is NOT what determines grouping -- only fo_lookup
    # (built from Shopify's own response) does. Two rows map to two
    # DIFFERENT FOs -> two separate fulfillment input groups.
    rows = [
        {"fulfillmentOrderLineItemId": "LI1", "Items_Name": "SKU-A", "Items_Quantity": 2},
        {"fulfillmentOrderLineItemId": "LI2", "Items_Name": "SKU-B", "Items_Quantity": 1},
    ]
    fo_lookup = {
        "LI1": ("gid://shopify/FulfillmentOrder/FO1", 5),
        "LI2": ("gid://shopify/FulfillmentOrder/FO2", 5),
    }
    inputs, shipped = build_fulfillment_input(rows, fo_lookup)
    fo_ids = sorted(i["fulfillmentOrderId"] for i in inputs)
    assert fo_ids == ["gid://shopify/FulfillmentOrder/FO1", "gid://shopify/FulfillmentOrder/FO2"]
    assert len(inputs) == 2
    assert sorted(shipped) == [("SKU-A", 2), ("SKU-B", 1)]


def test_groups_multiple_line_items_under_the_same_fulfillment_order():
    rows = [
        {"fulfillmentOrderLineItemId": "LI1", "Items_Name": "SKU-A", "Items_Quantity": 2},
        {"fulfillmentOrderLineItemId": "LI2", "Items_Name": "SKU-B", "Items_Quantity": 1},
    ]
    fo_lookup = {
        "LI1": ("gid://shopify/FulfillmentOrder/FO1", 5),
        "LI2": ("gid://shopify/FulfillmentOrder/FO1", 5),
    }
    inputs, shipped = build_fulfillment_input(rows, fo_lookup)
    assert len(inputs) == 1
    assert inputs[0]["fulfillmentOrderId"] == "gid://shopify/FulfillmentOrder/FO1"
    line_items = sorted(inputs[0]["fulfillmentOrderLineItems"], key=lambda li: li["id"])
    assert line_items == [
        {"id": "gid://shopify/FulfillmentOrderLineItem/LI1", "quantity": 2},
        {"id": "gid://shopify/FulfillmentOrderLineItem/LI2", "quantity": 1},
    ]


def test_quantity_is_min_of_stored_and_remaining():
    rows = [{"fulfillmentOrderLineItemId": "LI1", "Items_Name": "SKU-A", "Items_Quantity": 10}]
    fo_lookup = {"LI1": ("gid://shopify/FulfillmentOrder/FO1", 3)}  # remaining < stored
    inputs, shipped = build_fulfillment_input(rows, fo_lookup)
    assert inputs[0]["fulfillmentOrderLineItems"][0]["quantity"] == 3
    assert shipped == [("SKU-A", 3)]


def test_row_skipped_when_fulfillment_order_line_item_not_found():
    rows = [{"fulfillmentOrderLineItemId": "LI-UNKNOWN", "Items_Name": "SKU-A", "Items_Quantity": 1}]
    inputs, shipped = build_fulfillment_input(rows, fo_lookup={})
    assert inputs == []
    assert shipped == []


def test_row_skipped_when_nothing_remaining():
    rows = [{"fulfillmentOrderLineItemId": "LI1", "Items_Name": "SKU-A", "Items_Quantity": 5}]
    fo_lookup = {"LI1": ("gid://shopify/FulfillmentOrder/FO1", 0)}
    inputs, shipped = build_fulfillment_input(rows, fo_lookup)
    assert inputs == []
    assert shipped == []


# ---------------------------------------------------------------------------
# New-store path end-to-end wiring (settings/db/client all faked)
# ---------------------------------------------------------------------------
class _FakeConn:
    def close(self):
        pass


def test_new_store_ship_orders_success_wires_fo_grouping_and_records_fulfillment(monkeypatch):
    fake_settings = types.SimpleNamespace(
        order_table="shopify_rvmarines_order", fulfillment_table="shopify_rvmarines_fulfillment",
    )

    class FakeClient:
        def __init__(self):
            self.mutation_calls = []

        def graphql(self, query, variables=None):
            if "fulfillmentOrders" in query:
                return {"order": {"fulfillmentOrders": {"edges": [
                    {"node": {"id": "gid://shopify/FulfillmentOrder/FO1", "status": "OPEN",
                              "lineItems": {"edges": [
                                  {"node": {"id": "gid://shopify/FulfillmentOrderLineItem/LI1", "remainingQuantity": 2}},
                              ]}}},
                    {"node": {"id": "gid://shopify/FulfillmentOrder/FO2", "status": "CLOSED",
                              "lineItems": {"edges": [
                                  {"node": {"id": "gid://shopify/FulfillmentOrderLineItem/LI2", "remainingQuantity": 5}},
                              ]}}},
                ]}}}
            if "fulfillmentCreate" in query:
                self.mutation_calls.append(variables)
                return {"fulfillmentCreate": {
                    "fulfillment": {"id": "gid://shopify/Fulfillment/999", "status": "SUCCESS"},
                    "userErrors": [],
                }}
            raise AssertionError("unexpected query")

    fake_client = FakeClient()
    monkeypatch.setattr(tracking_mod, "_get_client_and_settings", lambda: (fake_client, fake_settings))

    import rvpartstore.db as db_mod

    def fake_connect(settings):
        return _FakeConn()

    def fake_run_query(conn, query, params=None):
        assert "%s" in query  # parameterized, not string-interpolated
        rows = [
            (params[0], "12345", "FO1|FO2", "LI1", "SKU-A", 3),
            (params[0], "12345", "FO1|FO2", "LI2", "SKU-B", 1),
        ]
        cols = ["TxnID", "shopifyOrderId", "fulfillmentOrderId", "fulfillmentOrderLineItemId",
                "Items_Name", "Items_Quantity"]
        return rows, cols

    write_calls = []

    def fake_run_write(conn, query, params=None, many=False):
        write_calls.append(params)
        return len(params or [])

    monkeypatch.setattr(db_mod, "connect", fake_connect)
    monkeypatch.setattr(db_mod, "run_query", fake_run_query)
    monkeypatch.setattr(db_mod, "run_write", fake_run_write)

    result = ship_orders("RVM_SP_N1050", "", "TRACK123", "UPS")

    assert result is True
    # FO2 is CLOSED -> excluded entirely; only SKU-A (via FO1) ships, at
    # min(stored=3, remaining=2) = 2.
    [variables] = fake_client.mutation_calls
    line_item_groups = variables["fulfillment"]["lineItemsByFulfillmentOrder"]
    assert len(line_item_groups) == 1
    assert line_item_groups[0]["fulfillmentOrderId"] == "gid://shopify/FulfillmentOrder/FO1"
    assert line_item_groups[0]["fulfillmentOrderLineItems"] == [
        {"id": "gid://shopify/FulfillmentOrderLineItem/LI1", "quantity": 2}
    ]
    assert variables["fulfillment"]["trackingInfo"] == {"company": "UPS", "number": "TRACK123"}
    assert variables["fulfillment"]["notifyCustomer"] is True

    [write_params] = write_calls
    assert write_params == [("RVM_SP_N1050", "999", "SKU-A", 2)]


def test_new_store_sku_semantics_empty_string_means_all_rows(monkeypatch):
    captured = {}

    def fake_run_query(conn, query, params=None):
        captured["query"] = query
        captured["params"] = params
        return [], ["TxnID", "shopifyOrderId", "fulfillmentOrderId", "fulfillmentOrderLineItemId",
                     "Items_Name", "Items_Quantity"]

    import rvpartstore.db as db_mod
    monkeypatch.setattr(db_mod, "connect", lambda settings: _FakeConn())
    monkeypatch.setattr(db_mod, "run_query", fake_run_query)
    monkeypatch.setattr(tracking_mod, "_get_client_and_settings",
                         lambda: (object(), types.SimpleNamespace(order_table="t", fulfillment_table="f")))

    ship_orders("RVM_SP_N1050", "", "T", "C")  # returns False (no rows), that's fine

    assert captured["params"] == ("RVM_SP_N1050",)
    assert "Items_Name = %s" not in captured["query"]


def test_new_store_sku_semantics_pipe_separated_list(monkeypatch):
    captured = {}

    def fake_run_query(conn, query, params=None):
        captured["query"] = query
        captured["params"] = params
        return [], ["TxnID", "shopifyOrderId", "fulfillmentOrderId", "fulfillmentOrderLineItemId",
                     "Items_Name", "Items_Quantity"]

    import rvpartstore.db as db_mod
    monkeypatch.setattr(db_mod, "connect", lambda settings: _FakeConn())
    monkeypatch.setattr(db_mod, "run_query", fake_run_query)
    monkeypatch.setattr(tracking_mod, "_get_client_and_settings",
                         lambda: (object(), types.SimpleNamespace(order_table="t", fulfillment_table="f")))

    ship_orders("RVM_SP_N1050", "SKU-A|SKU-B", "T", "C")

    assert captured["params"] == ("RVM_SP_N1050", "SKU-A", "SKU-B")
    assert captured["query"].count("Items_Name = %s") == 2
    assert "SKU-A" not in captured["query"]  # parameterized, not interpolated


def test_new_store_sku_semantics_single_sku(monkeypatch):
    captured = {}

    def fake_run_query(conn, query, params=None):
        captured["query"] = query
        captured["params"] = params
        return [], ["TxnID", "shopifyOrderId", "fulfillmentOrderId", "fulfillmentOrderLineItemId",
                     "Items_Name", "Items_Quantity"]

    import rvpartstore.db as db_mod
    monkeypatch.setattr(db_mod, "connect", lambda settings: _FakeConn())
    monkeypatch.setattr(db_mod, "run_query", fake_run_query)
    monkeypatch.setattr(tracking_mod, "_get_client_and_settings",
                         lambda: (object(), types.SimpleNamespace(order_table="t", fulfillment_table="f")))

    ship_orders("RVM_SP_N1050", "SKU-ONLY", "T", "C")

    assert captured["params"] == ("RVM_SP_N1050", "SKU-ONLY")
    assert captured["query"].count("Items_Name = %s") == 1


# ---------------------------------------------------------------------------
# New-store path never raises to the caller (the whole path is wrapped in a
# single try/except Exception): settings-load failure, an HTTP/ShopifyError
# from any graphql() call, and a null/missing fulfillmentCreate payload must
# all just log and return False. The legacy path is explicitly NOT covered
# by this -- its exceptions still propagate (re-confirmed at the bottom).
# ---------------------------------------------------------------------------
def test_new_store_settings_load_failure_returns_false_not_raises(monkeypatch, caplog):
    def boom():
        raise RuntimeError("cannot load .env: disk error")

    monkeypatch.setattr(tracking_mod, "_get_client_and_settings", boom)

    with caplog.at_level("ERROR"):
        result = ship_orders("RVM_SP_N1050", "", "T", "C")

    assert result is False
    assert any("RVM_SP_N1050" in r.message for r in caplog.records)


def test_new_store_graphql_http_error_returns_false_not_raises(monkeypatch, caplog):
    import rvpartstore.db as db_mod
    from rvpartstore.shopify_client import ShopifyError

    def fake_run_query(conn, query, params=None):
        return (
            [("RVM_SP_N1050", "12345", "FO1", "LI1", "SKU-A", 1)],
            ["TxnID", "shopifyOrderId", "fulfillmentOrderId", "fulfillmentOrderLineItemId",
             "Items_Name", "Items_Quantity"],
        )

    class FailingClient:
        def graphql(self, query, variables=None):
            raise ShopifyError("graphql HTTP 500 after 6 attempts")

    monkeypatch.setattr(db_mod, "connect", lambda settings: _FakeConn())
    monkeypatch.setattr(db_mod, "run_query", fake_run_query)
    monkeypatch.setattr(tracking_mod, "_get_client_and_settings",
                         lambda: (FailingClient(), types.SimpleNamespace(order_table="t", fulfillment_table="f")))

    with caplog.at_level("ERROR"):
        result = ship_orders("RVM_SP_N1050", "", "T", "C")

    assert result is False
    assert any("RVM_SP_N1050" in r.message for r in caplog.records)


def test_new_store_null_fulfillment_create_payload_returns_false_not_raises(monkeypatch, caplog):
    import rvpartstore.db as db_mod

    def fake_run_query(conn, query, params=None):
        return (
            [("RVM_SP_N1050", "12345", "FO1", "LI1", "SKU-A", 1)],
            ["TxnID", "shopifyOrderId", "fulfillmentOrderId", "fulfillmentOrderLineItemId",
             "Items_Name", "Items_Quantity"],
        )

    class NullPayloadClient:
        def graphql(self, query, variables=None):
            if "fulfillmentOrders" in query:
                return {"order": {"fulfillmentOrders": {"edges": [
                    {"node": {"id": "gid://shopify/FulfillmentOrder/FO1", "status": "OPEN",
                              "lineItems": {"edges": [
                                  {"node": {"id": "gid://shopify/FulfillmentOrderLineItem/LI1", "remainingQuantity": 5}},
                              ]}}},
                ]}}}
            if "fulfillmentCreate" in query:
                # Malformed/empty response body -- no "fulfillment" key, no
                # userErrors either (e.g. Shopify returned {"data": null}
                # upstream and graphql() handed back an empty dict, or the
                # mutation's own payload was null).
                return {"fulfillmentCreate": None}
            raise AssertionError("unexpected query")

    monkeypatch.setattr(db_mod, "connect", lambda settings: _FakeConn())
    monkeypatch.setattr(db_mod, "run_query", fake_run_query)
    monkeypatch.setattr(tracking_mod, "_get_client_and_settings",
                         lambda: (NullPayloadClient(), types.SimpleNamespace(order_table="t", fulfillment_table="f")))

    with caplog.at_level("ERROR"):
        result = ship_orders("RVM_SP_N1050", "", "T", "C")

    assert result is False
    assert any("RVM_SP_N1050" in r.message for r in caplog.records)


def test_new_store_entirely_null_graphql_result_returns_false_not_raises(monkeypatch, caplog):
    # Even more degenerate: graphql() itself returns None (e.g. a top-level
    # "data": null response) rather than a dict with "fulfillmentCreate".
    import rvpartstore.db as db_mod

    def fake_run_query(conn, query, params=None):
        return (
            [("RVM_SP_N1050", "12345", "FO1", "LI1", "SKU-A", 1)],
            ["TxnID", "shopifyOrderId", "fulfillmentOrderId", "fulfillmentOrderLineItemId",
             "Items_Name", "Items_Quantity"],
        )

    class NullResultClient:
        def graphql(self, query, variables=None):
            if "fulfillmentOrders" in query:
                return {"order": {"fulfillmentOrders": {"edges": [
                    {"node": {"id": "gid://shopify/FulfillmentOrder/FO1", "status": "OPEN",
                              "lineItems": {"edges": [
                                  {"node": {"id": "gid://shopify/FulfillmentOrderLineItem/LI1", "remainingQuantity": 5}},
                              ]}}},
                ]}}}
            if "fulfillmentCreate" in query:
                return None
            raise AssertionError("unexpected query")

    monkeypatch.setattr(db_mod, "connect", lambda settings: _FakeConn())
    monkeypatch.setattr(db_mod, "run_query", fake_run_query)
    monkeypatch.setattr(tracking_mod, "_get_client_and_settings",
                         lambda: (NullResultClient(), types.SimpleNamespace(order_table="t", fulfillment_table="f")))

    with caplog.at_level("ERROR"):
        result = ship_orders("RVM_SP_N1050", "", "T", "C")

    assert result is False

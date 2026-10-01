"""Black-box tests for rvpartstore.shopify_writes DRY_RUN behaviour (design
spec sec. 7; A3).

Contract under test: when settings.dry_run is True, update_prices/
set_inventory/set_vendorname/set_tracked must make ZERO calls to the Shopify
client (no API calls) and must write a CSV to
data/dry_run/<ts>/<kind>.csv instead.
"""
import os
import types

import pandas
import pytest

import rvpartstore.shopify_writes as writes


class _PoisonClient:
    """Fails loudly if graphql() is ever called -- DRY_RUN must never touch
    the network."""
    def graphql(self, query, variables=None):
        raise AssertionError("DRY_RUN must not call the Shopify API")


def _settings(tmp_path, dry_run=True, **overrides):
    base = dict(project_root=str(tmp_path), dry_run=dry_run, shopify_location_id="118096265348")
    base.update(overrides)
    return types.SimpleNamespace(**base)


def _dry_run_csv_path(settings, kind):
    return os.path.join(settings.project_root, "data", "dry_run", writes._RUN_TS, kind + ".csv")


def test_update_prices_dry_run_writes_csv_no_api_calls(tmp_path):
    settings = _settings(tmp_path)
    df = pandas.DataFrame({
        "productId": ["P1", "P2"], "variantId": ["V1", "V2"],
        "new_price": [10.0, 20.0], "compareAtPrice": [None, 25.0],
    })
    summary = writes.update_prices(_PoisonClient(), df, settings)

    assert summary == {"attempted": 2, "succeeded": 0, "failed": 0}
    csv_path = _dry_run_csv_path(settings, "prices")
    assert os.path.exists(csv_path)
    written = pandas.read_csv(csv_path)
    assert len(written.index) == 2


def test_update_prices_empty_df_no_csv_written(tmp_path):
    settings = _settings(tmp_path)
    df = pandas.DataFrame(columns=["productId", "variantId", "new_price", "compareAtPrice"])
    summary = writes.update_prices(_PoisonClient(), df, settings)
    assert summary == {"attempted": 0, "succeeded": 0, "failed": 0}
    assert not os.path.exists(_dry_run_csv_path(settings, "prices"))


def test_set_inventory_dry_run_writes_csv_no_api_calls_and_skips_unstocked(tmp_path):
    settings = _settings(tmp_path)
    df = pandas.DataFrame({
        "variantId": ["V1", "V2", "V3"], "inventoryId": ["I1", "I2", "I3"],
        "current_quantity": [1, 2, 3], "target_quantity": [0, 4, 6],
        "stocked": [True, True, False],
    })
    summary = writes.set_inventory(_PoisonClient(), settings, df)

    assert summary == {"attempted": 2, "succeeded": 0, "failed": 0}  # V3 (unstocked) excluded
    written = pandas.read_csv(_dry_run_csv_path(settings, "inventory"))
    assert len(written.index) == 2
    assert "V3" not in set(written["variantId"])


def test_set_inventory_all_unstocked_no_csv(tmp_path):
    settings = _settings(tmp_path)
    df = pandas.DataFrame({
        "variantId": ["V1"], "inventoryId": ["I1"], "current_quantity": [1],
        "target_quantity": [0], "stocked": [False],
    })
    summary = writes.set_inventory(_PoisonClient(), settings, df)
    assert summary == {"attempted": 0, "succeeded": 0, "failed": 0}
    assert not os.path.exists(_dry_run_csv_path(settings, "inventory"))


def test_set_vendorname_dry_run_writes_csv_no_api_calls(tmp_path):
    settings = _settings(tmp_path)
    df = pandas.DataFrame({"productId": ["P1"], "lowest_vendor": ["KS"]})
    summary = writes.set_vendorname(_PoisonClient(), df, settings)
    assert summary == {"attempted": 1, "succeeded": 0, "failed": 0}
    written = pandas.read_csv(_dry_run_csv_path(settings, "vendors"))
    assert list(written["lowest_vendor"]) == ["KS"]


def test_set_tracked_dry_run_writes_csv_no_api_calls(tmp_path):
    settings = _settings(tmp_path)
    summary = writes.set_tracked(_PoisonClient(), settings, ["100", "200", "300"])
    assert summary == {"attempted": 3, "succeeded": 0, "failed": 0}
    written = pandas.read_csv(_dry_run_csv_path(settings, "enable_tracking"))
    assert len(written.index) == 3


def test_set_tracked_empty_list_no_csv(tmp_path):
    settings = _settings(tmp_path)
    summary = writes.set_tracked(_PoisonClient(), settings, [])
    assert summary == {"attempted": 0, "succeeded": 0, "failed": 0}
    assert not os.path.exists(_dry_run_csv_path(settings, "enable_tracking"))


# ---------------------------------------------------------------------------
# Non-DRY_RUN sanity: real writes DO call the client (contrast case, so the
# DRY_RUN tests above are actually testing something meaningful).
# ---------------------------------------------------------------------------
def test_update_prices_non_dry_run_calls_client_and_reports_success(tmp_path):
    settings = _settings(tmp_path, dry_run=False)

    class RecordingClient:
        def __init__(self):
            self.calls = []

        def graphql(self, query, variables=None):
            self.calls.append(variables)
            return {"productVariantsBulkUpdate": {"product": {"id": variables["productId"]}, "userErrors": []}}

    client = RecordingClient()
    df = pandas.DataFrame({
        "productId": ["P1"], "variantId": ["V1"], "new_price": [10.0], "compareAtPrice": [None],
    })
    summary = writes.update_prices(client, df, settings)
    assert summary == {"attempted": 1, "succeeded": 1, "failed": 0}
    assert len(client.calls) == 1
    assert not os.path.exists(_dry_run_csv_path(settings, "prices"))


# ---------------------------------------------------------------------------
# set_inventory: 2026-07 payload shape (changeFromQuantity: null on every
# quantity), tracked==False exclusion, and per-item retry on batch userErrors.
# ---------------------------------------------------------------------------
def test_set_inventory_sends_change_from_quantity_null_on_every_entry(tmp_path):
    settings = _settings(tmp_path, dry_run=False)

    class RecordingClient:
        def __init__(self):
            self.calls = []

        def graphql(self, query, variables=None):
            self.calls.append(variables)
            return {"inventorySetQuantities": {"userErrors": []}}

    client = RecordingClient()
    df = pandas.DataFrame({
        "variantId": ["V1", "V2"], "inventoryId": ["I1", "I2"],
        "current_quantity": [1, 2], "target_quantity": [0, 4],
    })
    summary = writes.set_inventory(client, settings, df)

    assert summary == {"attempted": 2, "succeeded": 2, "failed": 0}
    [variables] = client.calls
    quantities = variables["input"]["quantities"]
    assert len(quantities) == 2
    for entry in quantities:
        assert entry["changeFromQuantity"] is None
    assert {q["quantity"] for q in quantities} == {0, 4}


def test_set_inventory_excludes_tracked_false_rows_like_unstocked(tmp_path):
    settings = _settings(tmp_path, dry_run=False)

    class RecordingClient:
        def __init__(self):
            self.calls = []

        def graphql(self, query, variables=None):
            self.calls.append(variables)
            return {"inventorySetQuantities": {"userErrors": []}}

    client = RecordingClient()
    df = pandas.DataFrame({
        "variantId": ["V1", "V2", "V3"], "inventoryId": ["I1", "I2", "I3"],
        "current_quantity": [1, 2, 3], "target_quantity": [0, 4, 6],
        "stocked": [True, True, True],
        "tracked": [True, False, True],
    })
    summary = writes.set_inventory(client, settings, df)

    assert summary == {"attempted": 2, "succeeded": 2, "failed": 0}  # V2 (tracked=False) excluded
    [variables] = client.calls
    inventory_item_ids = {q["inventoryItemId"] for q in variables["input"]["quantities"]}
    assert inventory_item_ids == {
        "gid://shopify/InventoryItem/I1", "gid://shopify/InventoryItem/I3",
    }


def test_set_inventory_all_tracked_false_no_api_call(tmp_path):
    settings = _settings(tmp_path, dry_run=False)
    df = pandas.DataFrame({
        "variantId": ["V1"], "inventoryId": ["I1"], "current_quantity": [1],
        "target_quantity": [0], "stocked": [True], "tracked": [False],
    })
    summary = writes.set_inventory(_PoisonClient(), settings, df)
    assert summary == {"attempted": 0, "succeeded": 0, "failed": 0}


def test_set_inventory_batch_user_errors_retries_individually_good_ones_succeed(tmp_path):
    settings = _settings(tmp_path, dry_run=False)

    class FlakyBatchClient:
        def __init__(self):
            self.batch_calls = 0
            self.single_calls = []

        def graphql(self, query, variables=None):
            quantities = variables["input"]["quantities"]
            if len(quantities) > 1:
                self.batch_calls += 1
                # The whole batch reports userErrors (Shopify doesn't say
                # which item caused it).
                return {"inventorySetQuantities": {"userErrors": [{"field": ["quantities"], "message": "bad item"}]}}
            # Single-item retry: only inventoryItemId I2 is actually bad.
            item_id = quantities[0]["inventoryItemId"]
            self.single_calls.append(item_id)
            if item_id == "gid://shopify/InventoryItem/I2":
                return {"inventorySetQuantities": {"userErrors": [{"field": ["quantities"], "message": "bad item"}]}}
            return {"inventorySetQuantities": {"userErrors": []}}

    client = FlakyBatchClient()
    df = pandas.DataFrame({
        "variantId": ["V1", "V2", "V3"], "inventoryId": ["I1", "I2", "I3"],
        "current_quantity": [1, 2, 3], "target_quantity": [0, 4, 6],
    })
    summary = writes.set_inventory(client, settings, df)

    assert client.batch_calls == 1
    assert sorted(client.single_calls) == [
        "gid://shopify/InventoryItem/I1", "gid://shopify/InventoryItem/I2", "gid://shopify/InventoryItem/I3",
    ]
    assert summary == {"attempted": 3, "succeeded": 2, "failed": 1}


# ---------------------------------------------------------------------------
# Live bug regression (2026-10-01): set_inventory fed object-dtype
# stocked/tracked columns (bools mixed with None, e.g. from an upstream
# merge) must not crash -- None must be treated as False (skipped, counted
# in the warning), not blow up `~column` (bitwise-NOT on an object column of
# Python bools/None raises/misbehaves instead of a clean boolean mask).
# ---------------------------------------------------------------------------
def test_set_inventory_object_dtype_stocked_and_tracked_with_none_does_not_crash(tmp_path):
    settings = _settings(tmp_path, dry_run=False)

    class RecordingClient:
        def __init__(self):
            self.calls = []

        def graphql(self, query, variables=None):
            self.calls.append(variables)
            return {"inventorySetQuantities": {"userErrors": []}}

    client = RecordingClient()
    # Built row-by-row from plain Python objects (True/False/None mixed) so
    # pandas infers an object dtype column, exactly like the live bug.
    df = pandas.DataFrame([
        {"variantId": "V1", "inventoryId": "I1", "current_quantity": 1, "target_quantity": 0,
         "stocked": True, "tracked": True},
        {"variantId": "V2", "inventoryId": "I2", "current_quantity": 2, "target_quantity": 4,
         "stocked": None, "tracked": True},
        {"variantId": "V3", "inventoryId": "I3", "current_quantity": 3, "target_quantity": 6,
         "stocked": False, "tracked": True},
        {"variantId": "V4", "inventoryId": "I4", "current_quantity": 4, "target_quantity": 1,
         "stocked": True, "tracked": None},
    ])
    assert df["stocked"].dtype == object
    assert df["tracked"].dtype == object

    summary = writes.set_inventory(client, settings, df)  # must not raise

    # Only V1 (stocked=True, tracked=True) goes through; V2/V4's None and
    # V3's False are all treated as "skip", not as truthy/crash.
    assert summary == {"attempted": 1, "succeeded": 1, "failed": 0}
    [variables] = client.calls
    assert len(variables["input"]["quantities"]) == 1
    assert variables["input"]["quantities"][0]["inventoryItemId"] == "gid://shopify/InventoryItem/I1"


def test_set_inventory_all_none_stocked_no_api_call_no_crash(tmp_path):
    settings = _settings(tmp_path, dry_run=False)
    df = pandas.DataFrame([
        {"variantId": "V1", "inventoryId": "I1", "current_quantity": 1, "target_quantity": 0,
         "stocked": None, "tracked": True},
    ])
    summary = writes.set_inventory(_PoisonClient(), settings, df)
    assert summary == {"attempted": 0, "succeeded": 0, "failed": 0}

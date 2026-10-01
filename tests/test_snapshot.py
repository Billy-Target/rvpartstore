"""Black-box tests for rvpartstore.snapshot.parse_snapshot_rows (design spec sec. 4).

Contract under test:
  * IDs are numeric strings (gid://shopify/<Type>/<id> stripped to <id>).
  * upc = barcode stripped of whitespace, or None if empty/whitespace-only.
  * price/compareAtPrice are floats; compareAtPrice missing -> NaN.
  * quantity: 0 when there is no inventoryLevel at the location ("stocked"
    tells the caller whether that 0 is real or just "not stocked here").
  * tracked is a bool.
  * vendor metafield value: a JSON list string '["KS"]' -> first element; a
    non-JSON raw string is passed through unchanged; no metafield -> all
    metafield* columns None.
"""
import math

from rvpartstore.snapshot import parse_snapshot_rows


def _node(**overrides):
    base = {
        "id": "gid://shopify/ProductVariant/111",
        "sku": "U-012345678905",
        "barcode": "012345678905",
        "price": "19.99",
        "compareAtPrice": None,
        "product": {"id": "gid://shopify/Product/222"},
        "metafield": {"id": "gid://shopify/Metafield/333", "value": '["KS"]'},
        "inventoryItem": {
            "id": "gid://shopify/InventoryItem/444",
            "tracked": False,
            "inventoryLevel": {"quantities": [{"name": "available", "quantity": 7}]},
        },
    }
    base.update(overrides)
    return base


def test_ids_are_numeric_strings_stripped_of_gid_prefix():
    df = parse_snapshot_rows([_node()])
    row = df.iloc[0]
    assert row["productId"] == "222"
    assert row["variantId"] == "111"
    assert row["inventoryId"] == "444"
    assert row["metafieldId"] == "333"


def test_upc_is_stripped_barcode_or_none_when_empty():
    df = parse_snapshot_rows([
        _node(barcode="  012345678905  "),
        _node(id="gid://shopify/ProductVariant/2", barcode=""),
        _node(id="gid://shopify/ProductVariant/3", barcode=None),
    ])
    assert df.iloc[0]["upc"] == "012345678905"
    assert df.iloc[1]["upc"] is None
    assert df.iloc[2]["upc"] is None


def test_price_and_compare_at_price_parsed_as_float_nan_when_missing():
    df = parse_snapshot_rows([_node(price="19.99", compareAtPrice=None)])
    row = df.iloc[0]
    assert row["price"] == 19.99
    assert math.isnan(row["compareAtPrice"])


def test_compare_at_price_present_is_parsed():
    df = parse_snapshot_rows([_node(compareAtPrice="24.99")])
    assert df.iloc[0]["compareAtPrice"] == 24.99


def test_quantity_zero_and_stocked_false_when_no_inventory_level():
    df = parse_snapshot_rows([_node(inventoryItem={
        "id": "gid://shopify/InventoryItem/444", "tracked": False, "inventoryLevel": None,
    })])
    row = df.iloc[0]
    assert row["quantity"] == 0
    assert bool(row["stocked"]) is False


def test_quantity_from_available_level_and_stocked_true():
    df = parse_snapshot_rows([_node(inventoryItem={
        "id": "gid://shopify/InventoryItem/444", "tracked": True,
        "inventoryLevel": {"quantities": [{"name": "available", "quantity": 12}]},
    })])
    row = df.iloc[0]
    assert row["quantity"] == 12
    assert bool(row["stocked"]) is True
    assert bool(row["tracked"]) is True


def test_vendor_metafield_json_list_takes_first_element():
    df = parse_snapshot_rows([_node(metafield={"id": "gid://shopify/Metafield/1", "value": '["KS"]'})])
    assert df.iloc[0]["metafieldValue"] == "KS"
    assert df.iloc[0]["metafieldKey"] == "vendorname"
    assert df.iloc[0]["metafieldNamespace"] == "custom"


def test_vendor_metafield_non_json_raw_string_passthrough():
    df = parse_snapshot_rows([_node(metafield={"id": "gid://shopify/Metafield/1", "value": "KS"})])
    assert df.iloc[0]["metafieldValue"] == "KS"


def test_vendor_metafield_empty_json_list_is_none():
    df = parse_snapshot_rows([_node(metafield={"id": "gid://shopify/Metafield/1", "value": "[]"})])
    assert df.iloc[0]["metafieldValue"] is None


def test_no_metafield_all_metafield_columns_none():
    df = parse_snapshot_rows([_node(metafield=None)])
    row = df.iloc[0]
    assert row["metafieldId"] is None
    assert row["metafieldKey"] is None
    assert row["metafieldNamespace"] is None
    assert row["metafieldValue"] is None


def test_empty_rows_produces_empty_dataframe_with_expected_columns():
    df = parse_snapshot_rows([])
    assert len(df.index) == 0
    for col in ("productId", "variantId", "sku", "upc", "active", "price",
                "compareAtPrice", "inventoryId", "quantity", "tracked",
                "metafieldId", "metafieldKey", "metafieldNamespace",
                "metafieldValue", "stocked"):
        assert col in df.columns


def test_active_is_always_one():
    df = parse_snapshot_rows([_node()])
    assert df.iloc[0]["active"] == 1


def test_multiple_rows_preserve_order_and_count():
    df = parse_snapshot_rows([
        _node(id="gid://shopify/ProductVariant/1", sku="A"),
        _node(id="gid://shopify/ProductVariant/2", sku="B"),
        _node(id="gid://shopify/ProductVariant/3", sku="C"),
    ])
    assert list(df["sku"]) == ["A", "B", "C"]
    assert list(df["variantId"]) == ["1", "2", "3"]

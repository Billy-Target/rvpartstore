"""Store snapshot via Shopify bulk query -> DataFrame (design spec sec. 4).

Not on the tracking import path: free to use pandas.
"""

import json
import logging

import pandas

log = logging.getLogger(__name__)

# R5: carries inventoryPolicy (tracking-enforcement check) and
# inventoryItem.trackedEditable.locked (so the enable_tracking autofix can
# skip variants Shopify won't let us flip).
BULK_QUERY = """
{
  productVariants {
    edges {
      node {
        id
        sku
        barcode
        price
        compareAtPrice
        inventoryPolicy
        product {
          id
          metafield(namespace: "custom", key: "vendorname") { id value }
        }
        inventoryItem {
          id
          tracked
          trackedEditable { locked }
          inventoryLevel(locationId: "gid://shopify/Location/%s") {
            quantities(names: ["available"]) { name quantity }
          }
        }
      }
    }
  }
}
"""


def _numeric_id(gid):
    if gid is None:
        return None
    return gid.rsplit("/", 1)[-1]


def _parse_vendor_value(value):
    """metafield value is a JSON list string '["KS"]' -> first element; if not
    JSON, the raw string."""
    if value is None:
        return None
    try:
        parsed = json.loads(value)
        if isinstance(parsed, list) and parsed:
            return parsed[0]
        if isinstance(parsed, list):
            return None
    except (ValueError, TypeError):
        pass
    return value


def parse_snapshot_rows(rows):
    """Pure: bulk-query JSONL dict rows (productVariants only, no nested
    objects need stitching since we don't select any child connections) ->
    DataFrame with the columns the pricing port expects (design spec sec. 4)."""
    records = []
    for node in rows:
        if "__parentId" in node:
            # Bulk query would emit child-connection rows with __parentId; we
            # don't request any (metafield/inventoryItem are singular), but
            # guard defensively in case Shopify ever changes that.
            continue

        inventory_item = node.get("inventoryItem") or {}
        inventory_level = inventory_item.get("inventoryLevel")
        stocked = inventory_level is not None
        quantity = 0
        if stocked:
            for q in inventory_level.get("quantities", []):
                if q.get("name") == "available":
                    quantity = int(q.get("quantity") or 0)

        # vendorname is a PRODUCT metafield (set_vendorname writes it on the
        # product); fall back to a variant-level one for older payload shapes.
        metafield = (node.get("product") or {}).get("metafield") or node.get("metafield")
        barcode = node.get("barcode")
        upc = barcode.strip() if isinstance(barcode, str) and barcode.strip() else None

        price = node.get("price")
        compare_at = node.get("compareAtPrice")

        record = {
            "productId": _numeric_id(node.get("product", {}).get("id")),
            "variantId": _numeric_id(node.get("id")),
            "sku": node.get("sku"),
            "upc": upc,
            "active": 1,
            "price": float(price) if price not in (None, "") else float("nan"),
            "compareAtPrice": float(compare_at) if compare_at not in (None, "") else float("nan"),
            "inventoryId": _numeric_id(inventory_item.get("id")),
            "quantity": quantity,
            "tracked": bool(inventory_item.get("tracked")),
            "metafieldId": _numeric_id(metafield.get("id")) if metafield else None,
            "metafieldKey": "vendorname" if metafield else None,
            "metafieldNamespace": "custom" if metafield else None,
            "metafieldValue": _parse_vendor_value(metafield.get("value")) if metafield else None,
            "stocked": stocked,
            "inventoryPolicy": node.get("inventoryPolicy"),
            "trackedEditableLocked": bool((inventory_item.get("trackedEditable") or {}).get("locked")),
        }
        records.append(record)

    columns = [
        "productId", "variantId", "sku", "upc", "active", "price", "compareAtPrice",
        "inventoryId", "quantity", "tracked", "metafieldId", "metafieldKey",
        "metafieldNamespace", "metafieldValue", "stocked", "inventoryPolicy", "trackedEditableLocked",
    ]
    df = pandas.DataFrame.from_records(records, columns=columns)
    return df


def fetch_snapshot(client, settings):
    query = BULK_QUERY % settings.shopify_location_id
    rows = client.bulk_query(query)
    return parse_snapshot_rows(rows)

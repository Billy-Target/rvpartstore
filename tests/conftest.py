"""Shared black-box test helpers.

These helpers exist only to *invoke* the modules under test (fake
requests.Session, fake DB cursors, DataFrame builders matching the documented
column contracts). They do not encode expected behaviour -- expected values
are hand-computed in each test module from the design spec.
"""
import types

import pandas
import pytest


# ---------------------------------------------------------------------------
# shopify_client fakes: a minimal stand-in for requests.Session that returns
# a scripted sequence of responses, recording every call for assertions.
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text="", headers=None, lines=None,
                 content=None, json_raises=False):
        self.status_code = status_code
        self._json = json_data
        self.text = text or ""
        self.headers = headers or {}
        self._lines = lines or []
        self._json_raises = json_raises
        if content is not None:
            self.content = content
        elif lines:
            self.content = ("\n".join(lines)).encode("utf-8")
        else:
            self.content = b""

    def json(self):
        if self._json_raises:
            raise ValueError("not valid JSON")
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError("HTTP %d" % self.status_code)

    def iter_lines(self, decode_unicode=True):
        for line in self._lines:
            yield line


class FakeSession:
    """Pops responses off a script queue in call order, regardless of URL.
    Each test builds the queue to match the exact sequence of POST/GET calls
    the code under test is expected to make."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"method": "POST", "url": url, "json": json, "headers": headers})
        return self._next()

    def get(self, url, params=None, headers=None, timeout=None, stream=None):
        self.calls.append({"method": "GET", "url": url, "params": params, "headers": headers})
        return self._next()

    def _next(self):
        if not self.script:
            raise AssertionError("FakeSession script exhausted but another request was made")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def fake_settings():
    """Minimal duck-typed Settings for shopify_client (only the attributes it
    actually reads)."""
    return types.SimpleNamespace(
        shopify_shop="thervpartstore.myshopify.com",
        shopify_client_id="cid",
        shopify_client_secret="csecret",
        shopify_api_version="2025-10",
        shopify_location_id="118096265348",
    )


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    """Every test in this suite mocks the network, so any time.sleep() the
    client does for backoff/throttling is dead time -- neutralize it in the
    one module that calls it, without changing call semantics. If a test
    wants to assert *that* sleep was called with a given duration, it can
    still inspect the spy via monkeypatch itself."""
    import rvpartstore.shopify_client as shopify_client_mod
    monkeypatch.setattr(shopify_client_mod.time, "sleep", lambda s: None)


def token_response(token="tok"):
    return FakeResponse(status_code=200, json_data={"access_token": token})


# ---------------------------------------------------------------------------
# pricing.py fixture builders. Column names/defaults mirror the documented
# contract (design spec sec. 4 for in_store rows, sec. 6/P1 vendor formulas
# for feed rows) -- only what's needed to *invoke* compute_changes validly.
# ---------------------------------------------------------------------------
FEED_DEFAULTS = {
    "keystonePartNumber": "PN", "meyerPartNumber": "PN", "motorstatePartNumber": "PN",
    "turnfourteenPartNumber": "PN", "cbkPartNumber": "PN", "premierPartNumber": "PN",
    "partsauthorityPartNumber": "PN", "cwrPartNumber": "PN",
    "keystoneCost": 0.0, "motorstateCost": 0.0, "meyerCost": 0.0, "turnfourteenCost": 0.0,
    "cbkCost": 0.0, "premierCost": 0.0, "partsauthorityCost": 0.0, "cwrCost": 0.0,
    "keystoneQuantity": 0, "motorstateQuantity": 0, "meyerQuantity": 0, "turnfourteenQuantity": 0,
    "cbkQuantity": 0, "premierQuantity": 0, "partsauthorityQuantity": 0,
    "PurolatorGroundAssessorials": 0.0, "motorstateLTL": 0, "cbkLTL": 0,
    "keystoneSpecialHandlingCharge": 0.0, "motorstateSpecialHandlingCharge": 0.0,
    "meyerSpecialHandlingCharge": 0.0, "turnfourteenSpecialHandlingCharge": 0.0,
    "cbkSpecialHandlingCharge": 0.0, "premierSpecialHandlingCharge": 0.0,
    "partsauthoritySpecialHandlingCharge": 0.0, "RealWeight": 0.0, "keystoneLTL": 0,
    "cwrQuantity": 0, "cwrSpecialHandlingCharge": 0.0,
    # NOTE: no "rma price"/"rma quantity" here -- those columns are attached
    # to the vendor feed ONLY by pricing.compute_changes' merge against the
    # RMA google sheet (rma_sheet_df param); if the raw feed_df already had
    # them, that merge would collide and pandas would auto-suffix them to
    # "rma price_x"/"rma price_y", breaking the downstream column selection.
}


def make_feed_row(upc, **overrides):
    row = dict(FEED_DEFAULTS)
    row["UPC"] = upc
    row.update(overrides)
    return row


def make_feed_df(rows):
    cols = ["UPC"] + list(FEED_DEFAULTS.keys())
    return pandas.DataFrame(rows, columns=cols)


def make_store_row(product_id, variant_id, upc, price, quantity, metafield_value=None,
                    compare_at_price=float("nan"), sku=None, stocked=True, inventory_id=None):
    return {
        "productId": product_id,
        "variantId": variant_id,
        "sku": sku or ("SKU-%s" % variant_id),
        "upc": upc,
        "active": 1,
        "price": price,
        "compareAtPrice": compare_at_price,
        "inventoryId": inventory_id or ("INV-%s" % variant_id),
        "quantity": quantity,
        "tracked": False,
        "metafieldId": "MF-%s" % product_id if metafield_value is not None else None,
        "metafieldKey": "vendorname" if metafield_value is not None else None,
        "metafieldNamespace": "custom" if metafield_value is not None else None,
        "metafieldValue": metafield_value,
        "stocked": stocked,
    }


def make_store_df(rows):
    cols = ["productId", "variantId", "sku", "upc", "active", "price", "compareAtPrice",
            "inventoryId", "quantity", "tracked", "metafieldId", "metafieldKey",
            "metafieldNamespace", "metafieldValue", "stocked"]
    return pandas.DataFrame(rows, columns=cols)


def empty_amazon_df():
    return pandas.DataFrame(columns=["price", "quantity", "seller-sku", "status"])


def make_amazon_df(rows):
    cols = ["price", "quantity", "seller-sku", "status"]
    return pandas.DataFrame(rows, columns=cols)


def empty_rma_sheet_df():
    return pandas.DataFrame(columns=["UPC", "quantity on hand", "Cost CAD @"])


def make_rma_sheet_df(rows):
    cols = ["UPC", "quantity on hand", "Cost CAD @"]
    return pandas.DataFrame(rows, columns=cols)


def empty_restricted_df():
    return pandas.DataFrame(columns=["SKU"])


def make_restricted_df(skus):
    return pandas.DataFrame({"SKU": skus})


def empty_map_df():
    return pandas.DataFrame(columns=["upc", "map_price"])


def make_map_df(rows):
    cols = ["upc", "map_price"]
    return pandas.DataFrame(rows, columns=cols)


def empty_upc_exception_df():
    return pandas.DataFrame(columns=["UPC"])


def make_pricing_settings(google_merchant_enabled=False, google_merchant_id="645493236",
                           breaker_zero_pct=20.0, breaker_price_pct=20.0):
    return types.SimpleNamespace(
        google_merchant_enabled=google_merchant_enabled,
        google_merchant_id=google_merchant_id,
        breaker_zero_pct=breaker_zero_pct,
        breaker_price_pct=breaker_price_pct,
    )

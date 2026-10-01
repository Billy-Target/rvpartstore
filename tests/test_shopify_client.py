"""Black-box tests for rvpartstore.shopify_client (design spec sec. 3, R3, A8).

Contract under test:
  * token(): lazily fetched via client-credentials grant, cached, refetched on
    token(force=True).
  * graphql(): HTTP 401 -> refetch token once and retry; 429/5xx/connection
    errors -> bounded exponential backoff, max 6 attempts, then ShopifyError
    (never loops forever); THROTTLED top-level error -> sleep and retry
    (counts toward attempts); any other top-level error -> raise immediately;
    proactive throttle sleeps based on extensions.cost.throttleStatus.
  * rest_get_paginated(): same retry/401 rules, follows Link rel="next".
  * bulk_query(): raises if a bulk op is already running; raises on
    userErrors; polls until COMPLETED/FAILED/CANCELED; COMPLETED with
    url=None yields nothing; otherwise streams JSONL lines as dicts.

FakeSession/FakeResponse (tests/conftest.py) stand in for requests.Session --
no real network is used anywhere in this file.
"""
import pytest

from rvpartstore.shopify_client import MAX_ATTEMPTS, ShopifyClient, ShopifyError
from .conftest import FakeResponse, FakeSession, token_response


# ---------------------------------------------------------------------------
# token()
# ---------------------------------------------------------------------------
def test_token_lazily_fetched_and_cached(fake_settings):
    session = FakeSession([token_response("tok1")])
    client = ShopifyClient(fake_settings, session=session)
    assert client.token() == "tok1"
    assert client.token() == "tok1"  # cached, no second call
    assert len(session.calls) == 1


def test_token_force_refetch(fake_settings):
    session = FakeSession([token_response("tok1"), token_response("tok2")])
    client = ShopifyClient(fake_settings, session=session)
    assert client.token() == "tok1"
    assert client.token(force=True) == "tok2"
    assert len(session.calls) == 2


def test_token_fetch_bounded_retries_then_raises(fake_settings):
    # Every attempt fails with a 500 -> must give up after MAX_ATTEMPTS, not
    # loop forever.
    session = FakeSession([FakeResponse(status_code=500, text="boom")] * MAX_ATTEMPTS)
    client = ShopifyClient(fake_settings, session=session)
    with pytest.raises(ShopifyError):
        client.token()
    assert len(session.calls) == MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# graphql()
# ---------------------------------------------------------------------------
def test_graphql_success_returns_data(fake_settings):
    session = FakeSession([
        token_response("tok1"),
        FakeResponse(status_code=200, json_data={"data": {"shop": {"name": "TheRVPartStore"}}}),
    ])
    client = ShopifyClient(fake_settings, session=session)
    data = client.graphql("{ shop { name } }")
    assert data == {"shop": {"name": "TheRVPartStore"}}


def test_graphql_401_refetches_token_once_and_retries(fake_settings):
    session = FakeSession([
        token_response("tok1"),
        FakeResponse(status_code=401, text="expired"),
        token_response("tok2"),
        FakeResponse(status_code=200, json_data={"data": {"ok": True}}),
    ])
    client = ShopifyClient(fake_settings, session=session)
    data = client.graphql("{ ok }")
    assert data == {"ok": True}
    # Last call must have used the refreshed token.
    last_call = session.calls[-1]
    assert last_call["headers"]["X-Shopify-Access-Token"] == "tok2"


def test_graphql_401_twice_raises_after_single_refetch(fake_settings):
    session = FakeSession([
        token_response("tok1"),
        FakeResponse(status_code=401, text="expired"),
        token_response("tok2"),
        FakeResponse(status_code=401, text="still expired"),
    ])
    client = ShopifyClient(fake_settings, session=session)
    with pytest.raises(ShopifyError, match="401"):
        client.graphql("{ ok }")


def test_graphql_429_bounded_retries_then_raises(fake_settings):
    script = [token_response("tok1")] + [
        FakeResponse(status_code=429, text="throttled", headers={}) for _ in range(MAX_ATTEMPTS)
    ]
    session = FakeSession(script)
    client = ShopifyClient(fake_settings, session=session)
    with pytest.raises(ShopifyError):
        client.graphql("{ ok }")
    # 1 token fetch + MAX_ATTEMPTS graphql attempts, never more (bounded).
    assert len(session.calls) == 1 + MAX_ATTEMPTS


def test_graphql_connection_error_bounded_retries_then_raises(fake_settings):
    import requests
    script = [token_response("tok1")] + [requests.ConnectionError("refused")] * MAX_ATTEMPTS
    session = FakeSession(script)
    client = ShopifyClient(fake_settings, session=session)
    with pytest.raises(ShopifyError):
        client.graphql("{ ok }")
    assert len(session.calls) == 1 + MAX_ATTEMPTS


def test_graphql_throttled_error_retries_then_succeeds(fake_settings):
    session = FakeSession([
        token_response("tok1"),
        FakeResponse(status_code=200, json_data={
            "errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}]
        }),
        FakeResponse(status_code=200, json_data={"data": {"ok": True}}),
    ])
    client = ShopifyClient(fake_settings, session=session)
    data = client.graphql("{ ok }")
    assert data == {"ok": True}


def test_graphql_throttled_forever_bounded_then_raises(fake_settings):
    script = [token_response("tok1")] + [
        FakeResponse(status_code=200, json_data={
            "errors": [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}]
        }) for _ in range(MAX_ATTEMPTS)
    ]
    session = FakeSession(script)
    client = ShopifyClient(fake_settings, session=session)
    with pytest.raises(ShopifyError):
        client.graphql("{ ok }")


def test_graphql_non_throttled_top_level_error_raises_immediately(fake_settings):
    session = FakeSession([
        token_response("tok1"),
        FakeResponse(status_code=200, json_data={"errors": [{"message": "Field 'x' doesn't exist"}]}),
    ])
    client = ShopifyClient(fake_settings, session=session)
    with pytest.raises(ShopifyError):
        client.graphql("{ x }")
    # No retry for a non-THROTTLED top-level error: only the one graphql call.
    assert len(session.calls) == 2  # token fetch + single graphql call


def test_graphql_errors_as_plain_string_does_not_crash_throttle_detection(fake_settings):
    # A malformed/unexpected `errors` shape (a bare string instead of the
    # usual list-of-dicts) must not raise a TypeError from _is_throttled --
    # it should just be treated as non-THROTTLED and raised as ShopifyError.
    session = FakeSession([
        token_response("tok1"),
        FakeResponse(status_code=200, json_data={"errors": "Internal error, please try again"}),
    ])
    client = ShopifyClient(fake_settings, session=session)
    with pytest.raises(ShopifyError):
        client.graphql("{ x }")
    assert len(session.calls) == 2  # not retried -- treated as a hard error


def test_graphql_non_2xx_client_error_raises_shopify_error_no_retry(fake_settings):
    session = FakeSession([
        token_response("tok1"),
        FakeResponse(status_code=403, text="forbidden"),
    ])
    client = ShopifyClient(fake_settings, session=session)
    with pytest.raises(ShopifyError, match="403"):
        client.graphql("{ x }")
    assert len(session.calls) == 2  # token fetch + single graphql call, no retry


def test_graphql_non_json_body_raises_shopify_error(fake_settings):
    session = FakeSession([
        token_response("tok1"),
        FakeResponse(status_code=200, text="<html>not json</html>", json_raises=True),
    ])
    client = ShopifyClient(fake_settings, session=session)
    with pytest.raises(ShopifyError):
        client.graphql("{ x }")


def test_rest_get_paginated_non_2xx_client_error_raises_shopify_error(fake_settings):
    session = FakeSession([
        token_response("tok1"),
        FakeResponse(status_code=403, text="forbidden"),
    ])
    client = ShopifyClient(fake_settings, session=session)
    with pytest.raises(ShopifyError, match="403"):
        list(client.rest_get_paginated("orders.json"))


def test_rest_get_paginated_non_json_body_raises_shopify_error(fake_settings):
    session = FakeSession([
        token_response("tok1"),
        FakeResponse(status_code=200, text="not json", json_raises=True),
    ])
    client = ShopifyClient(fake_settings, session=session)
    with pytest.raises(ShopifyError):
        list(client.rest_get_paginated("orders.json"))


def test_graphql_proactive_throttle_sleeps_based_on_cost(fake_settings, monkeypatch):
    import rvpartstore.shopify_client as shopify_client_mod
    sleep_calls = []
    monkeypatch.setattr(shopify_client_mod.time, "sleep", lambda s: sleep_calls.append(s))

    session = FakeSession([
        token_response("tok1"),
        FakeResponse(status_code=200, json_data={
            "data": {"ok": True},
            "extensions": {"cost": {"throttleStatus": {"currentlyAvailable": 50, "restoreRate": 10}}},
        }),
    ])
    client = ShopifyClient(fake_settings, session=session)
    client.graphql("{ ok }")
    # MIN_AVAILABLE_COST=200; (200-50)/10 = 15.0s
    assert 15.0 in sleep_calls


def test_graphql_no_proactive_throttle_when_cost_available(fake_settings, monkeypatch):
    import rvpartstore.shopify_client as shopify_client_mod
    sleep_calls = []
    monkeypatch.setattr(shopify_client_mod.time, "sleep", lambda s: sleep_calls.append(s))

    session = FakeSession([
        token_response("tok1"),
        FakeResponse(status_code=200, json_data={
            "data": {"ok": True},
            "extensions": {"cost": {"throttleStatus": {"currentlyAvailable": 900, "restoreRate": 50}}},
        }),
    ])
    client = ShopifyClient(fake_settings, session=session)
    client.graphql("{ ok }")
    assert sleep_calls == []


# ---------------------------------------------------------------------------
# rest_get_paginated()
# ---------------------------------------------------------------------------
def test_rest_get_paginated_follows_link_header_and_stops(fake_settings):
    page1 = FakeResponse(
        status_code=200, json_data={"orders": [{"id": 1}]},
        headers={"Link": '<https://thervpartstore.myshopify.com/admin/api/2025-10/orders.json?page_info=abc>; rel="next"'},
    )
    page2 = FakeResponse(status_code=200, json_data={"orders": [{"id": 2}]}, headers={})
    session = FakeSession([token_response("tok1"), page1, page2])
    client = ShopifyClient(fake_settings, session=session)
    pages = list(client.rest_get_paginated("orders.json", {"status": "open", "limit": 250}))
    assert [p["orders"][0]["id"] for p in pages] == [1, 2]
    assert len(session.calls) == 3  # token + 2 GETs
    # Second GET must use the exact Link URL, with no extra params re-applied.
    second_get = session.calls[2]
    assert second_get["url"] == "https://thervpartstore.myshopify.com/admin/api/2025-10/orders.json?page_info=abc"
    assert second_get["params"] is None


def test_rest_get_paginated_single_page_no_link_header(fake_settings):
    page1 = FakeResponse(status_code=200, json_data={"orders": []}, headers={})
    session = FakeSession([token_response("tok1"), page1])
    client = ShopifyClient(fake_settings, session=session)
    pages = list(client.rest_get_paginated("orders.json"))
    assert len(pages) == 1
    assert len(session.calls) == 2


def test_rest_get_paginated_429_then_success(fake_settings):
    session = FakeSession([
        token_response("tok1"),
        FakeResponse(status_code=429, text="slow down", headers={"Retry-After": "0"}),
        FakeResponse(status_code=200, json_data={"orders": []}, headers={}),
    ])
    client = ShopifyClient(fake_settings, session=session)
    pages = list(client.rest_get_paginated("orders.json"))
    assert len(pages) == 1
    assert len(session.calls) == 3


def test_rest_get_paginated_401_refetches_token_once(fake_settings):
    session = FakeSession([
        token_response("tok1"),
        FakeResponse(status_code=401, text="expired"),
        token_response("tok2"),
        FakeResponse(status_code=200, json_data={"orders": []}, headers={}),
    ])
    client = ShopifyClient(fake_settings, session=session)
    pages = list(client.rest_get_paginated("orders.json"))
    assert len(pages) == 1
    assert session.calls[-1]["headers"]["X-Shopify-Access-Token"] == "tok2"


def test_rest_get_paginated_bounded_retries_then_raises(fake_settings):
    script = [token_response("tok1")] + [
        FakeResponse(status_code=503, text="down", headers={}) for _ in range(MAX_ATTEMPTS)
    ]
    session = FakeSession(script)
    client = ShopifyClient(fake_settings, session=session)
    with pytest.raises(ShopifyError):
        list(client.rest_get_paginated("orders.json"))
    assert len(session.calls) == 1 + MAX_ATTEMPTS


# ---------------------------------------------------------------------------
# bulk_query()
# ---------------------------------------------------------------------------
def _running_ops_response(nodes):
    return FakeResponse(status_code=200, json_data={"data": {"bulkOperations": {"nodes": nodes}}})


def test_bulk_query_raises_if_a_running_query_type_op_exists(fake_settings):
    # 2026-07 API: currentBulkOperation no longer exists; the "already
    # running" check is bulkOperations(query:"status:RUNNING"), and only a
    # running QUERY-type op conflicts with bulkOperationRunQuery.
    session = FakeSession([
        token_response("tok1"),
        _running_ops_response([{"id": "gid://shopify/BulkOperation/1", "status": "RUNNING", "type": "QUERY"}]),
    ])
    client = ShopifyClient(fake_settings, session=session)
    with pytest.raises(ShopifyError, match="already running"):
        list(client.bulk_query("{ productVariants { edges { node { id } } } }"))


def test_bulk_query_does_not_raise_for_a_running_mutation_type_op(fake_settings):
    # A running bulk MUTATION doesn't block a bulk QUERY.
    session = FakeSession([
        token_response("tok1"),
        _running_ops_response([{"id": "gid://shopify/BulkOperation/2", "status": "RUNNING", "type": "MUTATION"}]),
        FakeResponse(status_code=200, json_data={
            "data": {"bulkOperationRunQuery": {
                "bulkOperation": {"id": "gid://shopify/BulkOperation/9", "status": "CREATED"},
                "userErrors": [],
            }}
        }),
        FakeResponse(status_code=200, json_data={
            "data": {"bulkOperation": {"id": "gid://shopify/BulkOperation/9", "status": "COMPLETED",
                                        "errorCode": None, "url": None, "objectCount": "0"}}
        }),
    ])
    client = ShopifyClient(fake_settings, session=session)
    rows = list(client.bulk_query("{ productVariants { edges { node { id } } } }", poll_s=0))
    assert rows == []


def test_bulk_query_raises_on_user_errors(fake_settings):
    session = FakeSession([
        token_response("tok1"),
        _running_ops_response([]),
        FakeResponse(status_code=200, json_data={
            "data": {"bulkOperationRunQuery": {
                "bulkOperation": None,
                "userErrors": [{"field": ["query"], "message": "bad query"}],
            }}
        }),
    ])
    client = ShopifyClient(fake_settings, session=session)
    with pytest.raises(ShopifyError):
        list(client.bulk_query("{ bad }"))


def test_bulk_query_completed_with_url_streams_jsonl_rows(fake_settings):
    session = FakeSession([
        token_response("tok1"),
        _running_ops_response([]),
        FakeResponse(status_code=200, json_data={
            "data": {"bulkOperationRunQuery": {
                "bulkOperation": {"id": "gid://shopify/BulkOperation/9", "status": "CREATED"},
                "userErrors": [],
            }}
        }),
        FakeResponse(status_code=200, json_data={
            "data": {"bulkOperation": {"id": "gid://shopify/BulkOperation/9", "status": "COMPLETED",
                                        "errorCode": None, "url": "https://example.com/result.jsonl",
                                        "objectCount": "2"}}
        }),
        FakeResponse(status_code=200, lines=['{"a": 1}', '{"b": 2}']),
    ])
    client = ShopifyClient(fake_settings, session=session)
    rows = list(client.bulk_query("{ productVariants { edges { node { id } } } }", poll_s=0))
    assert rows == [{"a": 1}, {"b": 2}]


def test_bulk_query_completed_with_no_url_yields_nothing(fake_settings):
    session = FakeSession([
        token_response("tok1"),
        _running_ops_response([]),
        FakeResponse(status_code=200, json_data={
            "data": {"bulkOperationRunQuery": {
                "bulkOperation": {"id": "gid://shopify/BulkOperation/9", "status": "CREATED"},
                "userErrors": [],
            }}
        }),
        FakeResponse(status_code=200, json_data={
            "data": {"bulkOperation": {"id": "gid://shopify/BulkOperation/9", "status": "COMPLETED",
                                        "errorCode": None, "url": None, "objectCount": "0"}}
        }),
    ])
    client = ShopifyClient(fake_settings, session=session)
    rows = list(client.bulk_query("{ productVariants { edges { node { id } } } }", poll_s=0))
    assert rows == []
    # No extra GET for a result file since url was None (token fetch +
    # bulkOperations running-check + bulkOperationRunQuery + one poll = 4
    # POSTs, no GET).
    assert len(session.calls) == 4
    assert all(c["method"] == "POST" for c in session.calls)


def test_bulk_query_failed_status_raises(fake_settings):
    session = FakeSession([
        token_response("tok1"),
        _running_ops_response([]),
        FakeResponse(status_code=200, json_data={
            "data": {"bulkOperationRunQuery": {
                "bulkOperation": {"id": "gid://shopify/BulkOperation/9", "status": "CREATED"},
                "userErrors": [],
            }}
        }),
        FakeResponse(status_code=200, json_data={
            "data": {"bulkOperation": {"id": "gid://shopify/BulkOperation/9", "status": "FAILED",
                                        "errorCode": "INTERNAL_SERVER_ERROR", "url": None, "objectCount": "0"}}
        }),
    ])
    client = ShopifyClient(fake_settings, session=session)
    with pytest.raises(ShopifyError):
        list(client.bulk_query("{ productVariants { edges { node { id } } } }", poll_s=0))


def test_bulk_query_downloads_fully_before_yielding_any_row(fake_settings, monkeypatch):
    # Item 8 (reviewer fix): a transient error on the download GET must not
    # yield any rows at all for that attempt (full-download-before-yield);
    # only after a successful full download do rows get yielded, and a
    # retried download must not duplicate rows.
    import requests
    session = FakeSession([
        token_response("tok1"),
        _running_ops_response([]),
        FakeResponse(status_code=200, json_data={
            "data": {"bulkOperationRunQuery": {
                "bulkOperation": {"id": "gid://shopify/BulkOperation/9", "status": "CREATED"},
                "userErrors": [],
            }}
        }),
        FakeResponse(status_code=200, json_data={
            "data": {"bulkOperation": {"id": "gid://shopify/BulkOperation/9", "status": "COMPLETED",
                                        "errorCode": None, "url": "https://example.com/result.jsonl",
                                        "objectCount": "2"}}
        }),
        requests.ConnectionError("blip"),
        FakeResponse(status_code=200, lines=['{"a": 1}', '{"b": 2}']),
    ])
    client = ShopifyClient(fake_settings, session=session)
    rows = list(client.bulk_query("{ productVariants { edges { node { id } } } }", poll_s=0))
    assert rows == [{"a": 1}, {"b": 2}]  # not duplicated despite the retry


# ---------------------------------------------------------------------------
# B2: with_static_token() mode (Brakex) -- uses the given token directly, no
# client-credentials call at all; a 401 raises ShopifyError after exactly
# one HTTP call (nothing to refetch with).
# ---------------------------------------------------------------------------
def test_with_static_token_uses_token_directly_no_oauth_call():
    session = FakeSession([
        FakeResponse(status_code=200, json_data={"data": {"ok": True}}),
    ])
    client = ShopifyClient.with_static_token("brakex.myshopify.com", "2026-07", "shpat_static123", session=session)
    data = client.graphql("{ ok }")

    assert data == {"ok": True}
    assert len(session.calls) == 1  # no separate oauth/token POST
    assert session.calls[0]["headers"]["X-Shopify-Access-Token"] == "shpat_static123"
    assert "brakex.myshopify.com" in session.calls[0]["url"]
    assert "2026-07" in session.calls[0]["url"]


def test_with_static_token_401_raises_after_exactly_one_http_call():
    session = FakeSession([
        FakeResponse(status_code=401, text="invalid token"),
    ])
    client = ShopifyClient.with_static_token("brakex.myshopify.com", "2026-07", "bad_token", session=session)
    with pytest.raises(ShopifyError) as exc_info:
        client.graphql("{ ok }")
    assert len(session.calls) == 1  # no refetch-and-retry (nothing to refetch with)
    # Review item 5: the message must describe a static token (not imply a
    # token refresh, which never happened in this mode).
    message = str(exc_info.value).lower()
    assert "static" in message
    assert "refresh" not in message


def test_with_static_token_rest_get_paginated_401_raises_after_one_call():
    session = FakeSession([
        FakeResponse(status_code=401, text="invalid token"),
    ])
    client = ShopifyClient.with_static_token("brakex.myshopify.com", "2026-07", "bad_token", session=session)
    with pytest.raises(ShopifyError) as exc_info:
        list(client.rest_get_paginated("orders.json"))
    assert len(session.calls) == 1
    message = str(exc_info.value).lower()
    assert "static" in message
    assert "refresh" not in message


def test_client_credentials_mode_401_message_mentions_refresh_not_static(fake_settings):
    # Contrast case: the normal (non-static) mode's exhausted-401 message
    # should still describe a refresh attempt, not claim a static token.
    session = FakeSession([
        token_response("tok1"),
        FakeResponse(status_code=401, text="expired"),
        token_response("tok2"),
        FakeResponse(status_code=401, text="still expired"),
    ])
    client = ShopifyClient(fake_settings, session=session)
    with pytest.raises(ShopifyError) as exc_info:
        client.graphql("{ ok }")
    message = str(exc_info.value).lower()
    assert "refresh" in message
    assert "static" not in message


def test_with_static_token_retries_and_throttle_still_work():
    # Everything else (bounded retries, throttle) is identical to the normal
    # client-credentials mode.
    session = FakeSession([
        FakeResponse(status_code=429, text="slow down", headers={}),
        FakeResponse(status_code=200, json_data={"data": {"ok": True}}),
    ])
    client = ShopifyClient.with_static_token("brakex.myshopify.com", "2026-07", "shpat_static123", session=session)
    data = client.graphql("{ ok }")
    assert data == {"ok": True}
    assert len(session.calls) == 2

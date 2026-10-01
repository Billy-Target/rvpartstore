"""Shopify Admin API client (GraphQL + REST + bulk operations).

R3: on the tracking import path (imported by tracking.py) — stdlib + requests
only, no pandas/numpy/dotenv.

Auth: Dev-Dashboard app, client-credentials grant (see design spec sec. 3) via
the normal constructor, OR a static-token mode (B2, e.g. Brakex) via the
with_static_token() classmethod — no client-credentials fetch, a 401 raises
ShopifyError directly (nothing to refetch with). Everything else (retries,
throttle, pagination) is identical between the two modes.
Bounded retries everywhere; the old code's `while True: try/except: pass` is
explicitly disallowed.
"""

import json
import logging
import time

import requests

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 6
TOKEN_MAX_AGE_S = 23 * 60 * 60  # refetch if older than 23h (valid 24h)
MIN_AVAILABLE_COST = 200


class ShopifyError(Exception):
    pass


class ShopifyClient:
    def __init__(self, settings, session=None):
        self._settings = settings
        self._session = session or requests.Session()
        self._shop = settings.shopify_shop
        self._api_version = settings.shopify_api_version
        self._static_token = None
        self._token = None
        self._token_fetched_at = 0.0

    @classmethod
    def with_static_token(cls, shop, api_version, access_token, session=None):
        """B2: static-token mode (Brakex) — no client-credentials fetch, the
        given access_token is used as-is forever; a 401 raises ShopifyError
        directly (there's no client_id/secret to refetch a new token with).
        Everything else (retries, throttle, pagination) is identical to the
        normal client-credentials mode."""
        self = cls.__new__(cls)
        self._settings = None
        self._session = session or requests.Session()
        self._shop = shop
        self._api_version = api_version
        self._static_token = access_token
        self._token = access_token
        self._token_fetched_at = time.time()
        return self

    # ------------------------------------------------------------------ token
    def token(self, force=False):
        if self._static_token is not None:
            return self._static_token
        if force or self._token is None or (time.time() - self._token_fetched_at) > TOKEN_MAX_AGE_S:
            self._token = self._fetch_token()
            self._token_fetched_at = time.time()
        return self._token

    def _fetch_token(self):
        url = "https://{}/admin/oauth/access_token".format(self._shop)
        body = {
            "grant_type": "client_credentials",
            "client_id": self._settings.shopify_client_id,
            "client_secret": self._settings.shopify_client_secret,
        }
        last_exc = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                resp = self._session.post(url, json=body, timeout=30)
                if resp.status_code >= 500 or resp.status_code == 429:
                    raise ShopifyError("token fetch HTTP %d: %s" % (resp.status_code, resp.text[:500]))
                resp.raise_for_status()
                return resp.json()["access_token"]
            except (requests.RequestException, ShopifyError, KeyError, ValueError) as exc:
                last_exc = exc
                log.warning("token fetch attempt %d/%d failed: %s", attempt, MAX_ATTEMPTS, exc)
                if attempt < MAX_ATTEMPTS:
                    time.sleep(_backoff_delay(attempt))
        raise ShopifyError("could not fetch Shopify access token after %d attempts: %s" % (MAX_ATTEMPTS, last_exc))

    # ---------------------------------------------------------------- graphql
    def graphql(self, query, variables=None):
        url = "https://{}/admin/api/{}/graphql.json".format(self._shop, self._api_version)
        payload = {"query": query, "variables": variables or {}}
        refetched_after_401 = False
        attempt = 0
        while True:
            attempt += 1
            headers = {"X-Shopify-Access-Token": self.token(), "Content-Type": "application/json"}
            try:
                resp = self._session.post(url, json=payload, headers=headers, timeout=60)
            except requests.RequestException as exc:
                if attempt >= MAX_ATTEMPTS:
                    raise ShopifyError("graphql request failed after %d attempts: %s" % (attempt, exc))
                log.warning("graphql connection error attempt %d/%d: %s", attempt, MAX_ATTEMPTS, exc)
                time.sleep(_backoff_delay(attempt))
                continue

            # B2: static-token mode (self._static_token set) has nothing to
            # refetch with, so a 401 there falls straight through to raise
            # below instead of retrying once like client-credentials mode.
            if resp.status_code == 401 and not refetched_after_401 and self._static_token is None:
                refetched_after_401 = True
                self.token(force=True)
                attempt -= 1  # doesn't count toward the bounded-retry budget
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt >= MAX_ATTEMPTS:
                    raise ShopifyError("graphql HTTP %d after %d attempts: %s" % (resp.status_code, attempt, resp.text[:500]))
                delay = _retry_after(resp) or _backoff_delay(attempt)
                log.warning("graphql HTTP %d attempt %d/%d, sleeping %.1fs", resp.status_code, attempt, MAX_ATTEMPTS, delay)
                time.sleep(delay)
                continue

            if resp.status_code == 401:
                raise ShopifyError(_unauthorized_message("graphql", self._static_token is not None, resp))

            # Item 8 (reviewer fix): any remaining non-2xx (400/403/404/422/...)
            # or a body that isn't valid JSON becomes a ShopifyError — these
            # are not retried (retrying a client error won't help).
            try:
                resp.raise_for_status()
            except requests.HTTPError as exc:
                raise ShopifyError("graphql HTTP %d: %s" % (resp.status_code, resp.text[:500])) from exc
            try:
                result = resp.json()
            except ValueError as exc:
                raise ShopifyError("graphql response was not valid JSON (HTTP %d): %s" % (
                    resp.status_code, resp.text[:500])) from exc

            errors = result.get("errors")
            if errors:
                if _is_throttled(errors):
                    if attempt >= MAX_ATTEMPTS:
                        raise ShopifyError("graphql THROTTLED after %d attempts: %s" % (attempt, errors))
                    log.info("graphql THROTTLED, sleeping 2s (attempt %d/%d)", attempt, MAX_ATTEMPTS)
                    time.sleep(2)
                    continue
                raise ShopifyError(errors)

            _proactive_throttle(result)
            return result.get("data")

    # ---------------------------------------------------------- rest (paged)
    def rest_get_paginated(self, path, params=None):
        url = "https://{}/admin/api/{}/{}".format(self._shop, self._api_version, path)
        next_url = url
        next_params = params or {}
        refetched_after_401 = False
        attempt = 0
        while next_url:
            attempt += 1
            headers = {"X-Shopify-Access-Token": self.token()}
            try:
                resp = self._session.get(next_url, params=next_params, headers=headers, timeout=60)
            except requests.RequestException as exc:
                if attempt >= MAX_ATTEMPTS:
                    raise ShopifyError("rest request failed after %d attempts: %s" % (attempt, exc))
                log.warning("rest connection error attempt %d/%d: %s", attempt, MAX_ATTEMPTS, exc)
                time.sleep(_backoff_delay(attempt))
                continue

            # B2: see the matching comment in graphql().
            if resp.status_code == 401 and not refetched_after_401 and self._static_token is None:
                refetched_after_401 = True
                self.token(force=True)
                attempt -= 1
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt >= MAX_ATTEMPTS:
                    raise ShopifyError("rest HTTP %d after %d attempts: %s" % (resp.status_code, attempt, resp.text[:500]))
                delay = _retry_after(resp) or _backoff_delay(attempt)
                log.warning("rest HTTP %d attempt %d/%d, sleeping %.1fs", resp.status_code, attempt, MAX_ATTEMPTS, delay)
                time.sleep(delay)
                continue

            if resp.status_code == 401:
                raise ShopifyError(_unauthorized_message("rest", self._static_token is not None, resp))

            # Item 8 (reviewer fix): same non-2xx / JSON-decode -> ShopifyError
            # treatment as graphql().
            try:
                resp.raise_for_status()
            except requests.HTTPError as exc:
                raise ShopifyError("rest HTTP %d: %s" % (resp.status_code, resp.text[:500])) from exc
            try:
                page = resp.json()
            except ValueError as exc:
                raise ShopifyError("rest response was not valid JSON (HTTP %d): %s" % (
                    resp.status_code, resp.text[:500])) from exc
            yield page

            attempt = 0
            refetched_after_401 = False
            next_url = _next_link(resp.headers.get("Link"))
            next_params = None  # the Link url already carries its own query string

    # ---------------------------------------------------------------- bulk
    def bulk_query(self, inner_query, poll_s=5, timeout_s=1800):
        # Item 2 (2026-07 API): `currentBulkOperation` no longer exists; use
        # `bulkOperations(query:"status:RUNNING")` instead. Only a running
        # QUERY-type op conflicts with us (a running MUTATION bulk op is a
        # different thing and doesn't block bulkOperationRunQuery).
        running = self.graphql(
            '{ bulkOperations(first: 5, query: "status:RUNNING") { nodes { id status type } } }'
        )
        running_nodes = (running or {}).get("bulkOperations", {}).get("nodes", [])
        running_queries = [n for n in running_nodes if n.get("type") == "QUERY"]
        if running_queries:
            raise ShopifyError("a bulk query operation is already running: %s" % running_queries)

        mutation = (
            'mutation { bulkOperationRunQuery(query: """%s""") '
            "{ bulkOperation { id status } userErrors { field message } } }" % inner_query
        )
        result = self.graphql(mutation)
        run = result["bulkOperationRunQuery"]
        if run["userErrors"]:
            raise ShopifyError(run["userErrors"])
        op_id = run["bulkOperation"]["id"]

        # Item 2 (2026-07 API): poll via `bulkOperation(id:)` (verified
        # working on 2026-07), not `node(id:) { ... on BulkOperation }`.
        poll_query = "query($id: ID!) { bulkOperation(id: $id) { id status errorCode url objectCount } }"
        t0 = time.time()
        status = None
        url = None
        while True:
            if time.time() - t0 > timeout_s:
                raise ShopifyError("bulk query timed out after %ds (last status=%s)" % (timeout_s, status))
            data = self.graphql(poll_query, {"id": op_id})
            node = data["bulkOperation"]
            status = node["status"]
            if status == "COMPLETED":
                url = node.get("url")
                break
            if status in ("FAILED", "CANCELED"):
                raise ShopifyError("bulk query %s: errorCode=%s" % (status, node.get("errorCode")))
            time.sleep(poll_s)

        if not url:
            return

        # Item 8 (reviewer fix): download the JSONL fully before yielding
        # anything, so a mid-stream retry on a transient network error can't
        # duplicate already-yielded rows.
        last_exc = None
        content = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                resp = self._session.get(url, timeout=120)
                resp.raise_for_status()
                content = resp.content
                break
            except requests.RequestException as exc:
                last_exc = exc
                log.warning("bulk result download attempt %d/%d failed: %s", attempt, MAX_ATTEMPTS, exc)
                if attempt < MAX_ATTEMPTS:
                    time.sleep(_backoff_delay(attempt))
        if content is None:
            raise ShopifyError("could not download bulk query result after %d attempts: %s" % (MAX_ATTEMPTS, last_exc))

        for line in content.decode("utf-8").splitlines():
            if line:
                yield json.loads(line)


def _unauthorized_message(label, static_mode, resp):
    """Code review fix (item 4): in static-token mode (B2) no refresh is ever
    attempted, so the error message must not claim one was."""
    if static_mode:
        return "%s HTTP 401 with static access token (token revoked/invalid?): %s" % (label, resp.text[:500])
    return "%s HTTP 401 even after token refresh: %s" % (label, resp.text[:500])


def _backoff_delay(attempt):
    return min(2 ** attempt, 30)


def _retry_after(resp):
    value = resp.headers.get("Retry-After")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _is_throttled(errors):
    # Item 8 (reviewer fix): tolerate a top-level `errors` that isn't the
    # usual list-of-dicts shape (e.g. a bare string) instead of crashing.
    if not isinstance(errors, list):
        return False
    for err in errors:
        if not isinstance(err, dict):
            continue
        if (err.get("extensions") or {}).get("code") == "THROTTLED":
            return True
    return False


def _proactive_throttle(result):
    try:
        throttle = result["extensions"]["cost"]["throttleStatus"]
        available = throttle["currentlyAvailable"]
        restore_rate = throttle["restoreRate"]
    except (KeyError, TypeError):
        return
    if available < MIN_AVAILABLE_COST and restore_rate:
        time.sleep((MIN_AVAILABLE_COST - available) / restore_rate)


def _next_link(link_header):
    if not link_header:
        return None
    for part in link_header.split(","):
        section = part.split(";")
        if len(section) < 2:
            continue
        url_part = section[0].strip()
        rel_part = section[1].strip()
        if rel_part == 'rel="next"' and url_part.startswith("<") and url_part.endswith(">"):
            return url_part[1:-1]
    return None

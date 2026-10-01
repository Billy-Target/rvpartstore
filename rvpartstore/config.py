"""Settings loaded from ``<project_root>/.env``.

R3: this module is on the tracking import path (imported by ``tracking.py``,
directly or transitively, whenever another project does
``from rvpartstore.tracking import ship_orders``). It therefore imports ONLY
the stdlib — no pandas, no numpy, no python-dotenv. ``.env`` is hand-parsed
below (``KEY=VALUE``, ``#`` comments, optional matching quotes).

Validation is split in two (R3):
  * ``load_settings()`` always builds a full ``Settings`` object; missing keys
    just become ``""`` / defaults, it never raises.
  * ``validate_tracking(settings)`` checks only the keys tracking.py needs.
  * ``validate_upload(settings)`` checks everything else, for the upload job.

Nothing here touches logging handlers or the network at import time.
"""

import os
import dataclasses


class MissingSettingsError(Exception):
    """Raised by validate_tracking()/validate_upload() when required keys are absent."""


# rvpartstore/config.py -> project root is two levels up (rvpartstore/rvpartstore/config.py -> rvpartstore/)
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ENV_PATH = os.path.join(_PROJECT_ROOT, ".env")


def _strip_inline_comment(value):
    """Strip a trailing ``# ...`` comment that isn't inside quotes."""
    in_quote = None
    for i, ch in enumerate(value):
        if in_quote:
            if ch == in_quote:
                in_quote = None
        elif ch in ("'", '"'):
            in_quote = ch
        elif ch == "#":
            return value[:i]
    return value


def _parse_env_text(text):
    values = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = _strip_inline_comment(value.strip()).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _read_env_file(path):
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return _parse_env_text(f.read())


def _resolve_path(value, default):
    value = value if value not in (None, "") else default
    if not value:
        return value
    if os.path.isabs(value):
        return value
    return os.path.join(_PROJECT_ROOT, value)


def _parse_bool(value, default=False):
    if value in (None, ""):
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _parse_int(value, default):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _parse_float(value, default):
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _parse_list(value):
    if not value:
        return []
    return [v.strip() for v in str(value).split(",") if v.strip()]


@dataclasses.dataclass(frozen=True)
class Settings:
    project_root: str

    # Shopify
    shopify_shop: str
    shopify_client_id: str
    shopify_client_secret: str
    shopify_api_version: str
    shopify_location_id: str

    # MySQL
    mysql_host: str
    mysql_user: str
    mysql_password: str
    mysql_database: str
    order_table: str
    fulfillment_table: str
    txn_prefix: str
    store_type: str
    order_max_age_days: int
    order_id_blocklist: tuple

    # Amazon SP-API
    amazon_refresh_token: str
    amazon_lwa_app_id: str
    amazon_lwa_client_secret: str
    amazon_aws_access_key: str
    amazon_aws_secret_key: str
    amazon_role_arn: str
    amazon_report_path: str

    # Google
    google_token_path: str
    google_credentials_path: str
    rma_sheet_id: str
    rma_sheet_range: str
    upc_exception_path: str
    google_merchant_enabled: bool
    google_merchant_id: str

    # Runtime
    dry_run: bool
    log_name: str
    quiet_start: str
    quiet_end: str

    # Guards (R5/R7/V5)
    tracking_autofix_max: int
    breaker_zero_pct: float
    breaker_price_pct: float
    feed_match_min: int

    # Brakex (B1) — same SHOPIFY_API_VERSION as RV, no separate key.
    brakex_enabled: bool
    brakex_shop: str
    brakex_access_token: str
    brakex_order_table: str
    brakex_txn_prefix: str
    brakex_store_type: str
    brakex_order_id_blocklist: tuple
    brakex_usd_to_cad: float


# Keys required for the tracking import path only (R3).
TRACKING_REQUIRED_KEYS = (
    "SHOPIFY_SHOP",
    "SHOPIFY_CLIENT_ID",
    "SHOPIFY_CLIENT_SECRET",
    "SHOPIFY_API_VERSION",
    "SHOPIFY_LOCATION_ID",
    "MYSQL_HOST",
    "MYSQL_USER",
    "MYSQL_PASSWORD",
    "MYSQL_DATABASE",
    "ORDER_TABLE",
    "FULFILLMENT_TABLE",
    "TXN_PREFIX",
)

# Additional keys required to run the upload job and friends.
UPLOAD_REQUIRED_KEYS = (
    "STORE_TYPE",
    "AMAZON_REFRESH_TOKEN",
    "AMAZON_LWA_APP_ID",
    "AMAZON_LWA_CLIENT_SECRET",
    "AMAZON_AWS_ACCESS_KEY",
    "AMAZON_AWS_SECRET_KEY",
    "AMAZON_ROLE_ARN",
    "GOOGLE_TOKEN_PATH",
    "GOOGLE_CREDENTIALS_PATH",
    "RMA_SHEET_ID",
    "RMA_SHEET_RANGE",
    "UPC_EXCEPTION_PATH",
)

# Just the MySQL connection keys — for jobs (e.g. the standalone brakex_orders
# job, item 5 review fix) that need DB access but not the full RV
# tracking/upload credential set.
MYSQL_REQUIRED_KEYS = (
    "MYSQL_HOST",
    "MYSQL_USER",
    "MYSQL_PASSWORD",
    "MYSQL_DATABASE",
)

# B1: only required when BRAKEX_ENABLED (checked by validate_brakex()).
BRAKEX_REQUIRED_KEYS = (
    "BRAKEX_SHOP",
    "BRAKEX_ACCESS_TOKEN",
    "BRAKEX_ORDER_TABLE",
    "BRAKEX_TXN_PREFIX",
    "BRAKEX_STORE_TYPE",
)


def load_settings(env_path=None):
    """Build a Settings object from <project_root>/.env. Never raises on missing
    keys (use validate_tracking()/validate_upload() for that) so the same loader
    works for both import paths."""
    env_path = env_path or _ENV_PATH
    raw = dict(_read_env_file(env_path))
    # Real OS environment variables (e.g. set by Task Scheduler) win over .env.
    for key in list(raw) + list(os.environ):
        if key in os.environ:
            raw[key] = os.environ[key]

    def g(key, default=""):
        # Code review fix (item 3): a key that's PRESENT but empty (e.g.
        # `BRAKEX_ENABLED=` with nothing after the `=`) must fall back to
        # `default`, same as an ABSENT key — not silently resolve to "".
        # Confirmed this doesn't change any existing (pre-Brakex) test's
        # expectations: none of them rely on an explicit empty value
        # overriding a non-empty default, so this is applied generally
        # rather than being special-cased to just the Brakex keys.
        value = raw.get(key, default)
        return default if value == "" else value

    return Settings(
        project_root=_PROJECT_ROOT,
        shopify_shop=g("SHOPIFY_SHOP"),
        shopify_client_id=g("SHOPIFY_CLIENT_ID"),
        shopify_client_secret=g("SHOPIFY_CLIENT_SECRET"),
        shopify_api_version=g("SHOPIFY_API_VERSION", "2026-07"),
        shopify_location_id=g("SHOPIFY_LOCATION_ID"),
        mysql_host=g("MYSQL_HOST"),
        mysql_user=g("MYSQL_USER"),
        mysql_password=g("MYSQL_PASSWORD"),
        mysql_database=g("MYSQL_DATABASE", "inventory"),
        order_table=g("ORDER_TABLE", "shopify_rvmarines_order"),
        fulfillment_table=g("FULFILLMENT_TABLE", "shopify_rvmarines_fulfillment"),
        txn_prefix=g("TXN_PREFIX", "RVM_SP_N"),
        store_type=g("STORE_TYPE", "shopifyca_rv"),
        order_max_age_days=_parse_int(g("ORDER_MAX_AGE_DAYS", "60"), 60),
        order_id_blocklist=tuple(_parse_list(g("ORDER_ID_BLOCKLIST", ""))),
        amazon_refresh_token=g("AMAZON_REFRESH_TOKEN"),
        amazon_lwa_app_id=g("AMAZON_LWA_APP_ID"),
        amazon_lwa_client_secret=g("AMAZON_LWA_CLIENT_SECRET"),
        amazon_aws_access_key=g("AMAZON_AWS_ACCESS_KEY"),
        amazon_aws_secret_key=g("AMAZON_AWS_SECRET_KEY"),
        amazon_role_arn=g("AMAZON_ROLE_ARN"),
        amazon_report_path=_resolve_path(g("AMAZON_REPORT_PATH"), os.path.join(_PROJECT_ROOT, "data", "amazon_report.txt")),
        google_token_path=_resolve_path(g("GOOGLE_TOKEN_PATH"), os.path.join(_PROJECT_ROOT, "token.json")),
        google_credentials_path=_resolve_path(g("GOOGLE_CREDENTIALS_PATH"), os.path.join(_PROJECT_ROOT, "credentials.json")),
        rma_sheet_id=g("RMA_SHEET_ID", "1Yk_2rIDUFpaX32n5Zgw39duUe3-VkqB-qAbdZheU_CE"),
        rma_sheet_range=g("RMA_SHEET_RANGE", "abaaba!A:D"),
        upc_exception_path=_resolve_path(g("UPC_EXCEPTION_PATH"), ""),
        google_merchant_enabled=_parse_bool(g("GOOGLE_MERCHANT_ENABLED", "false")),
        google_merchant_id=g("GOOGLE_MERCHANT_ID", "645493236"),
        dry_run=_parse_bool(g("DRY_RUN", "false")),
        log_name=g("LOG_NAME", "rvpartstore"),
        quiet_start=g("QUIET_START", "23:30"),
        quiet_end=g("QUIET_END", "06:30"),
        tracking_autofix_max=_parse_int(g("TRACKING_AUTOFIX_MAX", "500"), 500),
        breaker_zero_pct=_parse_float(g("BREAKER_ZERO_PCT", "20"), 20.0),
        breaker_price_pct=_parse_float(g("BREAKER_PRICE_PCT", "20"), 20.0),
        feed_match_min=_parse_int(g("FEED_MATCH_MIN", "19000"), 19000),
        brakex_enabled=_parse_bool(g("BRAKEX_ENABLED", "true")),
        brakex_shop=g("BRAKEX_SHOP", "brakex.myshopify.com"),
        brakex_access_token=g("BRAKEX_ACCESS_TOKEN"),
        brakex_order_table=g("BRAKEX_ORDER_TABLE", "shopify_brakex_order"),
        brakex_txn_prefix=g("BRAKEX_TXN_PREFIX", "BRX_SP_37"),
        brakex_store_type=g("BRAKEX_STORE_TYPE", "shopifycom_brx"),
        brakex_order_id_blocklist=tuple(_parse_list(g("BRAKEX_ORDER_ID_BLOCKLIST", ""))),
        brakex_usd_to_cad=_parse_float(g("BRAKEX_USD_TO_CAD", "1.35"), 1.35),
    )


def _missing(settings, keys):
    missing = []
    for key in keys:
        attr = key.lower()
        value = getattr(settings, attr, "")
        if value in (None, ""):
            missing.append(key)
    return missing


def validate_tracking(settings):
    missing = _missing(settings, TRACKING_REQUIRED_KEYS)
    if missing:
        raise MissingSettingsError("missing required .env keys for tracking: " + ", ".join(missing))
    if settings.txn_prefix != "RVM_SP_N":
        raise MissingSettingsError(
            "TXN_PREFIX must equal 'RVM_SP_N' (NEW_STORE_PREFIX); got %r" % (settings.txn_prefix,)
        )


def validate_upload(settings):
    validate_tracking(settings)
    missing = _missing(settings, UPLOAD_REQUIRED_KEYS)
    if missing:
        raise MissingSettingsError("missing required .env keys for upload: " + ", ".join(missing))


def validate_mysql(settings):
    """Just the MySQL connection keys (item 5 review fix) — used by jobs that
    need DB access but not the full RV tracking/upload credential set."""
    missing = _missing(settings, MYSQL_REQUIRED_KEYS)
    if missing:
        raise MissingSettingsError("missing required .env keys for MySQL: " + ", ".join(missing))


def validate_brakex(settings):
    """B1: Brakex keys are only required when BRAKEX_ENABLED — kept as its own
    validator (not folded into validate_upload()) so the RV upload path's
    validation contract is unchanged; callers (main.py) call this explicitly
    before running the brakex_orders job/step, and only when
    settings.brakex_enabled."""
    missing = _missing(settings, BRAKEX_REQUIRED_KEYS)
    if missing:
        raise MissingSettingsError("missing required .env keys for brakex (BRAKEX_ENABLED=true): " + ", ".join(missing))
    # Code review fix (item 3): BRAKEX_USD_TO_CAD must be a positive number —
    # a zero/negative rate would silently zero out or flip the sign of every
    # USD-presentment paypal order's Total/Items_Amount.
    if settings.brakex_usd_to_cad <= 0:
        raise MissingSettingsError(
            "BRAKEX_USD_TO_CAD must be > 0, got %r" % (settings.brakex_usd_to_cad,)
        )

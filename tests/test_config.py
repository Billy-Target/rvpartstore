"""Black-box tests for rvpartstore.config (design spec sec. 2, R3).

Contract under test:
  * hand-parsed .env: KEY=VALUE, '#' comments (including '#' that appears
    inside quotes must NOT start a comment), optional matching quotes.
  * booleans parse true/false/1/0 case-insensitively.
  * relative paths resolve against the project root (not CWD).
  * load_settings() never raises; validate_tracking()/validate_upload() do,
    listing the missing keys, and validation is split (R3): tracking needs
    only SHOPIFY_*/MYSQL_*/ORDER_TABLE/FULFILLMENT_TABLE/TXN_PREFIX; upload
    needs the rest on top.
  * TXN_PREFIX must equal 'RVM_SP_N' (V1's NEW_STORE_PREFIX) or
    validate_tracking raises.
"""
import os

import pytest

from rvpartstore import config

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(config.__file__)))

# The minimal set of keys required for validate_tracking() to pass.
TRACKING_ENV = """
SHOPIFY_SHOP=thervpartstore.myshopify.com
SHOPIFY_CLIENT_ID=cid123
SHOPIFY_CLIENT_SECRET=csecret456
SHOPIFY_API_VERSION=2025-10
SHOPIFY_LOCATION_ID=118096265348
MYSQL_HOST=192.168.20.12
MYSQL_USER=dbuser
MYSQL_PASSWORD=dbpass
MYSQL_DATABASE=inventory
ORDER_TABLE=shopify_rvmarines_order
FULFILLMENT_TABLE=shopify_rvmarines_fulfillment
TXN_PREFIX=RVM_SP_N
"""

# Everything validate_upload additionally requires that has NO built-in
# default in load_settings (so omitting it is a real failure, not masked by
# a fallback value).
UPLOAD_EXTRA_ENV = """
AMAZON_REFRESH_TOKEN=amztok
AMAZON_LWA_APP_ID=appid
AMAZON_LWA_CLIENT_SECRET=lwasecret
AMAZON_AWS_ACCESS_KEY=awskey
AMAZON_AWS_SECRET_KEY=awssecret
AMAZON_ROLE_ARN=arn:aws:iam::123:role/x
UPC_EXCEPTION_PATH=data/upc_exception.xlsx
"""


def _write_env(tmp_path, text):
    p = tmp_path / ".env"
    p.write_text(text, encoding="utf-8")
    return str(p)


# ---------------------------------------------------------------------------
# hand-parsing
# ---------------------------------------------------------------------------
def test_parses_key_value_strips_inline_comment_and_whitespace(tmp_path):
    env_path = _write_env(tmp_path, "SHOPIFY_SHOP=thervpartstore.myshopify.com   # the new store\n")
    settings = config.load_settings(env_path=env_path)
    assert settings.shopify_shop == "thervpartstore.myshopify.com"


def test_full_line_comment_and_blank_lines_ignored(tmp_path):
    env_path = _write_env(tmp_path, "\n# a full comment line\n\nSHOPIFY_SHOP=shop.example\n")
    settings = config.load_settings(env_path=env_path)
    assert settings.shopify_shop == "shop.example"


def test_matching_quotes_are_stripped(tmp_path):
    env_path = _write_env(
        tmp_path,
        'SHOPIFY_CLIENT_ID="abc123"\n'
        "SHOPIFY_CLIENT_SECRET='secret with spaces'\n",
    )
    settings = config.load_settings(env_path=env_path)
    assert settings.shopify_client_id == "abc123"
    assert settings.shopify_client_secret == "secret with spaces"


def test_hash_inside_quotes_is_not_treated_as_comment_start(tmp_path):
    env_path = _write_env(tmp_path, 'LOG_NAME="my#name"\n')
    settings = config.load_settings(env_path=env_path)
    assert settings.log_name == "my#name"


def test_missing_env_file_does_not_raise_load_settings_just_uses_defaults(tmp_path):
    missing_path = str(tmp_path / "does_not_exist.env")
    settings = config.load_settings(env_path=missing_path)
    assert settings.shopify_shop == ""
    assert settings.mysql_database == "inventory"  # documented default


# ---------------------------------------------------------------------------
# booleans
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("true", True), ("True", True), ("TRUE", True),
    ("false", False), ("False", False), ("FALSE", False),
    ("1", True), ("0", False),
])
def test_boolean_parsing_case_insensitive(tmp_path, raw, expected):
    env_path = _write_env(tmp_path, TRACKING_ENV + "\nDRY_RUN=%s\n" % raw)
    settings = config.load_settings(env_path=env_path)
    assert settings.dry_run is expected


def test_boolean_default_false_when_absent(tmp_path):
    env_path = _write_env(tmp_path, TRACKING_ENV)
    settings = config.load_settings(env_path=env_path)
    assert settings.dry_run is False
    assert settings.google_merchant_enabled is False


# ---------------------------------------------------------------------------
# relative path resolution (against project root, not CWD)
# ---------------------------------------------------------------------------
def test_relative_path_resolves_against_project_root_not_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # prove CWD is NOT used for resolution
    env_path = _write_env(tmp_path, TRACKING_ENV + "\nAMAZON_REPORT_PATH=data/custom_report.txt\n")
    settings = config.load_settings(env_path=env_path)
    expected = os.path.join(PROJECT_ROOT, "data/custom_report.txt")
    assert settings.amazon_report_path == expected
    assert not settings.amazon_report_path.startswith(str(tmp_path))


def test_absolute_path_left_unchanged(tmp_path):
    abs_path = str(tmp_path / "somewhere" / "report.txt")
    env_path = _write_env(tmp_path, TRACKING_ENV + "\nAMAZON_REPORT_PATH=%s\n" % abs_path)
    settings = config.load_settings(env_path=env_path)
    assert settings.amazon_report_path == abs_path


def test_relative_path_default_when_key_absent(tmp_path):
    env_path = _write_env(tmp_path, TRACKING_ENV)
    settings = config.load_settings(env_path=env_path)
    assert settings.amazon_report_path == os.path.join(PROJECT_ROOT, "data", "amazon_report.txt")
    assert settings.google_token_path == os.path.join(PROJECT_ROOT, "token.json")


# ---------------------------------------------------------------------------
# int / list parsing
# ---------------------------------------------------------------------------
def test_order_max_age_days_parsed_as_int(tmp_path):
    env_path = _write_env(tmp_path, TRACKING_ENV + "\nORDER_MAX_AGE_DAYS=45\n")
    settings = config.load_settings(env_path=env_path)
    assert settings.order_max_age_days == 45
    assert isinstance(settings.order_max_age_days, int)


def test_order_id_blocklist_parsed_as_tuple(tmp_path):
    env_path = _write_env(tmp_path, TRACKING_ENV + "\nORDER_ID_BLOCKLIST=111, 222,333\n")
    settings = config.load_settings(env_path=env_path)
    assert settings.order_id_blocklist == ("111", "222", "333")


def test_order_id_blocklist_empty_by_default(tmp_path):
    env_path = _write_env(tmp_path, TRACKING_ENV)
    settings = config.load_settings(env_path=env_path)
    assert settings.order_id_blocklist == ()


# ---------------------------------------------------------------------------
# validate_tracking / validate_upload split (R3) + missing-key errors
# ---------------------------------------------------------------------------
def test_validate_tracking_passes_with_only_tracking_keys(tmp_path):
    env_path = _write_env(tmp_path, TRACKING_ENV)
    settings = config.load_settings(env_path=env_path)
    config.validate_tracking(settings)  # must not raise


def test_validate_tracking_raises_and_lists_missing_keys(tmp_path):
    # Omit MYSQL_PASSWORD and MYSQL_USER (neither has a built-in default in
    # load_settings, unlike e.g. FULFILLMENT_TABLE/ORDER_TABLE/TXN_PREFIX --
    # see the finding about those in the test report).
    partial = TRACKING_ENV.replace("MYSQL_PASSWORD=dbpass\n", "").replace(
        "MYSQL_USER=dbuser\n", ""
    )
    env_path = _write_env(tmp_path, partial)
    settings = config.load_settings(env_path=env_path)
    with pytest.raises(config.MissingSettingsError) as exc_info:
        config.validate_tracking(settings)
    message = str(exc_info.value)
    assert "MYSQL_PASSWORD" in message
    assert "MYSQL_USER" in message


def test_validate_tracking_rejects_wrong_txn_prefix(tmp_path):
    bad = TRACKING_ENV.replace("TXN_PREFIX=RVM_SP_N\n", "TXN_PREFIX=SOMETHING_ELSE\n")
    env_path = _write_env(tmp_path, bad)
    settings = config.load_settings(env_path=env_path)
    with pytest.raises(config.MissingSettingsError):
        config.validate_tracking(settings)


def test_validate_upload_requires_tracking_keys_too(tmp_path):
    # Missing tracking-required MYSQL_HOST -> validate_upload must also fail,
    # even though all the upload-only keys are present.
    partial = TRACKING_ENV.replace("MYSQL_HOST=192.168.20.12\n", "")
    env_path = _write_env(tmp_path, partial + UPLOAD_EXTRA_ENV)
    settings = config.load_settings(env_path=env_path)
    with pytest.raises(config.MissingSettingsError) as exc_info:
        config.validate_upload(settings)
    assert "MYSQL_HOST" in str(exc_info.value)


def test_validate_upload_fails_when_amazon_creds_missing(tmp_path):
    # Tracking keys present; upload-only AMAZON_* (no default) absent.
    env_path = _write_env(tmp_path, TRACKING_ENV)
    settings = config.load_settings(env_path=env_path)
    with pytest.raises(config.MissingSettingsError) as exc_info:
        config.validate_upload(settings)
    assert "AMAZON_REFRESH_TOKEN" in str(exc_info.value)


def test_validate_upload_passes_with_tracking_plus_upload_extra_keys(tmp_path):
    env_path = _write_env(tmp_path, TRACKING_ENV + UPLOAD_EXTRA_ENV)
    settings = config.load_settings(env_path=env_path)
    config.validate_upload(settings)  # must not raise


# ---------------------------------------------------------------------------
# Review item 3: a key that's PRESENT but EMPTY falls back to its default,
# same as an absent key (applies generally, not just to Brakex keys).
# ---------------------------------------------------------------------------
def test_empty_value_falls_back_to_default_brakex_enabled(tmp_path):
    env_path = _write_env(tmp_path, TRACKING_ENV + "\nBRAKEX_ENABLED=\n")
    settings = config.load_settings(env_path=env_path)
    assert settings.brakex_enabled is True  # default


def test_empty_value_falls_back_to_default_brakex_store_type(tmp_path):
    env_path = _write_env(tmp_path, TRACKING_ENV + "\nBRAKEX_STORE_TYPE=\n")
    settings = config.load_settings(env_path=env_path)
    assert settings.brakex_store_type == "shopifycom_brx"


def test_empty_value_falls_back_to_default_brakex_usd_to_cad(tmp_path):
    env_path = _write_env(tmp_path, TRACKING_ENV + "\nBRAKEX_USD_TO_CAD=\n")
    settings = config.load_settings(env_path=env_path)
    assert settings.brakex_usd_to_cad == 1.35


def test_empty_value_falls_back_to_default_non_brakex_key(tmp_path):
    # Applied generally, not special-cased to Brakex keys.
    env_path = _write_env(tmp_path, TRACKING_ENV.replace("MYSQL_DATABASE=inventory\n", "MYSQL_DATABASE=\n"))
    settings = config.load_settings(env_path=env_path)
    assert settings.mysql_database == "inventory"


def test_present_non_empty_value_still_overrides_default(tmp_path):
    env_path = _write_env(tmp_path, TRACKING_ENV + "\nBRAKEX_STORE_TYPE=custom_brx_type\n")
    settings = config.load_settings(env_path=env_path)
    assert settings.brakex_store_type == "custom_brx_type"


# ---------------------------------------------------------------------------
# validate_mysql: MySQL connection keys only.
# ---------------------------------------------------------------------------
def test_validate_mysql_passes_with_mysql_keys_present(tmp_path):
    env_path = _write_env(tmp_path, "MYSQL_HOST=h\nMYSQL_USER=u\nMYSQL_PASSWORD=p\nMYSQL_DATABASE=inventory\n")
    settings = config.load_settings(env_path=env_path)
    config.validate_mysql(settings)  # must not raise


def test_validate_mysql_raises_when_missing(tmp_path):
    env_path = _write_env(tmp_path, "MYSQL_HOST=h\nMYSQL_USER=u\n")
    settings = config.load_settings(env_path=env_path)
    with pytest.raises(config.MissingSettingsError) as exc_info:
        config.validate_mysql(settings)
    assert "MYSQL_PASSWORD" in str(exc_info.value)


# ---------------------------------------------------------------------------
# validate_brakex: required keys + BRAKEX_USD_TO_CAD > 0.
# ---------------------------------------------------------------------------
BRAKEX_FULL_ENV = (
    "BRAKEX_SHOP=brakex.myshopify.com\n"
    "BRAKEX_ACCESS_TOKEN=shpat_abc\n"
    "BRAKEX_ORDER_TABLE=shopify_brakex_order\n"
    "BRAKEX_TXN_PREFIX=BRX_SP_37\n"
    "BRAKEX_STORE_TYPE=shopifycom_brx\n"
)


def test_validate_brakex_passes_with_full_keys(tmp_path):
    env_path = _write_env(tmp_path, BRAKEX_FULL_ENV)
    settings = config.load_settings(env_path=env_path)
    config.validate_brakex(settings)  # must not raise


def test_validate_brakex_raises_when_access_token_missing(tmp_path):
    env_path = _write_env(tmp_path, BRAKEX_FULL_ENV.replace("BRAKEX_ACCESS_TOKEN=shpat_abc\n", ""))
    settings = config.load_settings(env_path=env_path)
    with pytest.raises(config.MissingSettingsError) as exc_info:
        config.validate_brakex(settings)
    assert "BRAKEX_ACCESS_TOKEN" in str(exc_info.value)


def test_validate_brakex_raises_when_usd_to_cad_zero(tmp_path):
    env_path = _write_env(tmp_path, BRAKEX_FULL_ENV + "\nBRAKEX_USD_TO_CAD=0\n")
    settings = config.load_settings(env_path=env_path)
    assert settings.brakex_usd_to_cad == 0.0
    with pytest.raises(config.MissingSettingsError, match="BRAKEX_USD_TO_CAD"):
        config.validate_brakex(settings)


def test_validate_brakex_raises_when_usd_to_cad_negative(tmp_path):
    env_path = _write_env(tmp_path, BRAKEX_FULL_ENV + "\nBRAKEX_USD_TO_CAD=-1.35\n")
    settings = config.load_settings(env_path=env_path)
    with pytest.raises(config.MissingSettingsError, match="BRAKEX_USD_TO_CAD"):
        config.validate_brakex(settings)

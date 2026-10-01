"""R3 import hygiene: modules on the tracking import path (rvpartstore's
__init__, config, shopify_client, db, tracking) may import ONLY stdlib +
requests + mysql.connector. Importing rvpartstore.tracking in a subprocess
must not load pandas/numpy/dotenv, and must not configure logging or touch
the network/filesystem as a side effect of import alone.

Run as a real subprocess (not just checking sys.modules in-process) so this
reflects exactly what tracking_automation would experience on cut-over day:
a fresh interpreter that does `from rvpartstore.tracking import ship_orders`
with no other rvpartstore module already imported.
"""
import subprocess
import sys

PROJECT_ROOT = None


def _project_root():
    global PROJECT_ROOT
    if PROJECT_ROOT is None:
        import os
        PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return PROJECT_ROOT


def _run(code):
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=_project_root(),
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_importing_tracking_does_not_load_pandas_numpy_or_dotenv():
    code = (
        "import sys\n"
        "from rvpartstore.tracking import ship_orders\n"
        "loaded = sorted(m for m in sys.modules if m.split('.')[0] in "
        "('pandas', 'numpy', 'dotenv'))\n"
        "assert not loaded, 'unexpected heavy modules loaded: %r' % (loaded,)\n"
        "print('OK')\n"
    )
    result = _run(code)
    assert result.returncode == 0, "stdout=%r stderr=%r" % (result.stdout, result.stderr)
    assert "OK" in result.stdout


def test_importing_tracking_does_not_configure_root_logging_handlers():
    code = (
        "import logging\n"
        "from rvpartstore.tracking import ship_orders\n"
        "assert logging.getLogger().handlers == [], 'import must not add root logging handlers'\n"
        "print('OK')\n"
    )
    result = _run(code)
    assert result.returncode == 0, "stdout=%r stderr=%r" % (result.stdout, result.stderr)
    assert "OK" in result.stdout


def test_tracking_module_exposes_the_documented_signature():
    code = (
        "import inspect\n"
        "from rvpartstore.tracking import ship_orders\n"
        "params = list(inspect.signature(ship_orders).parameters)\n"
        "assert params == ['order_id', 'sku', 'tracking', 'carrier'], params\n"
        "print('OK')\n"
    )
    result = _run(code)
    assert result.returncode == 0, "stdout=%r stderr=%r" % (result.stdout, result.stderr)
    assert "OK" in result.stdout

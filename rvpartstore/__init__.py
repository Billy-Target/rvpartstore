# rvpartstore package.
#
# R3: this file must be empty of side effects (no logging setup, no network,
# no eager .env read). It is imported whenever any other project does
# `from rvpartstore.tracking import ship_orders`, so it — and config.py,
# shopify_client.py, db.py, tracking.py — may only import stdlib + requests +
# mysql.connector. Do not add pandas/numpy/dotenv imports to this module.

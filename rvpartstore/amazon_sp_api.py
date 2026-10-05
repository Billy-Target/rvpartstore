"""Ported from francis-shopify's amazon_sp_api.py; credentials come from
Settings instead of being hard-coded (design spec sec. 5/0)."""

import time

from sp_api.base import Marketplaces, ReportType
from sp_api.api import Reports


def _credentials(settings):
    return dict(
        refresh_token=settings.amazon_refresh_token,
        lwa_app_id=settings.amazon_lwa_app_id,
        lwa_client_secret=settings.amazon_lwa_client_secret,
        aws_access_key=settings.amazon_aws_access_key,
        aws_secret_key=settings.amazon_aws_secret_key,
        role_arn=settings.amazon_role_arn,
    )


def create_amazon_report(settings, out_path, poll_s=5, max_polls=360):
    """Request GET_MERCHANT_LISTINGS_ALL_DATA and download it to out_path.
    Bounded polling (max_polls * poll_s seconds, 30 min) instead of the old
    `while ...: continue` with no cap; the report can take >5 min to build."""
    reports = Reports(credentials=_credentials(settings), marketplace=Marketplaces.CA)
    created = reports.create_report(
        reportType=ReportType.GET_MERCHANT_LISTINGS_ALL_DATA, reportOptions={"custom": "true"})
    # Use our own report id (the old code took the first IN_PROGRESS report of
    # this type, which can be another job's request for the same report).
    report_id = created.payload.get("reportId")
    if not report_id:
        raise RuntimeError("amazon report: create_report returned no reportId: %s" % created.payload)

    status = None
    for _ in range(max_polls):
        status = reports.get_report(report_id).payload.get("processingStatus")
        if status not in ("IN_QUEUE", "IN_PROGRESS"):
            break
        time.sleep(poll_s)
    else:
        raise RuntimeError("amazon report %s still %s after %ds" % (report_id, status, max_polls * poll_s))

    get_report = reports.get_report(report_id)
    if get_report.payload.get("processingStatus") != "DONE":
        raise RuntimeError("amazon report %s finished with status %s" % (
            report_id, get_report.payload.get("processingStatus")))

    document_id = get_report.payload.get("reportDocumentId")
    reports.get_report_document(document_id, download=True, file=out_path)
    return out_path

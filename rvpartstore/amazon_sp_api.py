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


def create_amazon_report(settings, out_path, poll_s=2, max_polls=150):
    """Request GET_MERCHANT_LISTINGS_ALL_DATA and download it to out_path.
    Bounded polling (max_polls * poll_s seconds) instead of the old
    `while ...: continue` with no cap."""
    reports = Reports(credentials=_credentials(settings), marketplace=Marketplaces.CA)
    reports.create_report(reportType=ReportType.GET_MERCHANT_LISTINGS_ALL_DATA, reportOptions={"custom": "true"})
    time.sleep(15)

    get_reports = reports.get_reports(
        reportTypes=["GET_MERCHANT_LISTINGS_ALL_DATA"], processingStatuses=["IN_PROGRESS"]
    )
    pending = get_reports.payload.get("reports")
    if not pending:
        raise RuntimeError("amazon report: no IN_PROGRESS report found after create_report")
    report_id = pending[0]["reportId"]

    for _ in range(max_polls):
        status = reports.get_report(report_id).payload.get("processingStatus")
        if status != "IN_PROGRESS":
            break
        time.sleep(poll_s)
    else:
        raise RuntimeError("amazon report %s still IN_PROGRESS after %ds" % (report_id, max_polls * poll_s))

    get_report = reports.get_report(report_id)
    if get_report.payload.get("processingStatus") != "DONE":
        raise RuntimeError("amazon report %s finished with status %s" % (
            report_id, get_report.payload.get("processingStatus")))

    document_id = get_report.payload.get("reportDocumentId")
    reports.get_report_document(document_id, download=True, file=out_path)
    return out_path

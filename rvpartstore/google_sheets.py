"""Ported from francis-shopify's google_sheets.py — only what's used (design
spec sec. 5/10). token/credentials paths come from Settings instead of being
hard-coded relative paths."""

import pandas
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/content"]


def _get_credentials(settings):
    creds = None
    token_path = settings.google_token_path
    import os
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(settings.google_credentials_path, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w") as token:
            token.write(creds.to_json())
    return creds


def get_google_spreadsheet_service(settings):
    creds = _get_credentials(settings)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def get_google_content_service(settings):
    creds = _get_credentials(settings)
    return build("content", "v2.1", credentials=creds, cache_discovery=False)


def get_info_from_google_docs(settings, sheet_range, sheet_id):
    """Grab a range from a Google spreadsheet as a DataFrame (first row = header)."""
    service = get_google_spreadsheet_service(settings)
    sheet = service.spreadsheets()
    result = sheet.values().get(spreadsheetId=sheet_id, range=sheet_range).execute()
    values = result.get("values", [])
    if not values:
        return pandas.DataFrame()
    dataframe = pandas.DataFrame(values)
    dataframe = dataframe[1:]
    dataframe = dataframe.reset_index(drop=True)
    dataframe.columns = values[0]
    return dataframe


def overwrite_sheet(settings, df, sheet_range_from, sheet_range_to, ind_from, ind_to, sheet_name,
                    overwrite_start_line, sheet_id):
    service = get_google_spreadsheet_service(settings)
    body = {"values": df.values.tolist()}
    sheet_range = sheet_name + "!" + sheet_range_from + str(ind_from + overwrite_start_line) + ":" + \
        sheet_range_to + str(ind_to + overwrite_start_line)
    return service.spreadsheets().values().update(
        spreadsheetId=sheet_id, range=sheet_range, valueInputOption="USER_ENTERED", body=body
    ).execute()

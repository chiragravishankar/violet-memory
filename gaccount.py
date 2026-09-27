"""Google account access (OAuth desktop flow) for Calendar and Gmail actions. No channel code here.

credentials.json (the OAuth client) and token.json (the user's grant) live in the project root,
readable only by the owner, and are git-ignored. Neither is ever logged or printed.
"""

import asyncio
import logging
import os
from pathlib import Path

log = logging.getLogger("gaccount")

ROOT = Path(__file__).resolve().parent
CLIENT_FILE = ROOT / "credentials.json"
TOKEN_FILE = ROOT / "token.json"
SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",  # inbox awareness: read only, never modify
]
READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
LOGIN_TIMEOUT = 300  # seconds to finish the browser login


class NotConnected(Exception):
    pass


def _save(creds) -> None:
    TOKEN_FILE.write_text(creds.to_json())
    os.chmod(TOKEN_FILE, 0o600)


def credentials():
    """Valid credentials, refreshed if needed. Raises NotConnected if there is no usable grant."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not TOKEN_FILE.exists():
        raise NotConnected("no token")
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), granted_scopes() or SCOPES)
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception as e:
            raise NotConnected(f"refresh failed ({type(e).__name__})") from None
        _save(creds)
        return creds
    raise NotConnected("token not usable")


def granted_scopes() -> list[str]:
    """The scopes the saved grant actually has (older grants may lack newer scopes)."""
    import json

    try:
        return list(json.loads(TOKEN_FILE.read_text()).get("scopes") or [])
    except Exception:
        return []


def can_read_mail() -> bool:
    return READ_SCOPE in granted_scopes() and connected()


def connected() -> bool:
    try:
        credentials()
        return True
    except NotConnected:
        return False


def _login_blocking() -> str:
    """Run the desktop OAuth flow in the local browser; returns the connected account's address."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_FILE), SCOPES)
    creds = flow.run_local_server(
        port=0, open_browser=True, timeout_seconds=LOGIN_TIMEOUT,
        authorization_prompt_message="", success_message="Violet is connected. You can close this tab.",
    )
    _save(creds)
    return account_email_blocking()


async def login() -> str:
    if not CLIENT_FILE.exists():
        raise NotConnected("credentials.json is missing")
    return await asyncio.to_thread(_login_blocking)


def service(name: str, version: str):
    from googleapiclient.discovery import build

    return build(name, version, credentials=credentials(), cache_discovery=False)


def account_email_blocking() -> str:
    return service("gmail", "v1").users().getProfile(userId="me").execute().get("emailAddress", "")


async def account_email() -> str:
    return await asyncio.to_thread(account_email_blocking)

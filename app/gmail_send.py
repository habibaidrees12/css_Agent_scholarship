"""Send saved CSC drafts through the Gmail API.

This module does not discover professors or rewrite drafts. A message is sent
only when a caller passes a Gmail client and the draft passes the stored
verification checks. Tests pass a mock client. The default automatic-sending
switch is off.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from app.config import PROJECT_ROOT, get_data_dir
from app.outreach import drafts_path, shortlist_path, tracker_path, write_json
from app.university_sources import OFFICIAL_SOURCE_KINDS
from app.verification import published_email

GMAIL_SCOPES = (
    "https://www.googleapis.com/auth/gmail.send",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
)
AUTOMATIC_BATCH_LIMIT = 5


class GmailSendError(RuntimeError):
    """Gmail did not accept a message. The draft stays unsent."""


@dataclass
class GmailStatus:
    connected: bool
    account: str | None = None
    error: str | None = None


@dataclass
class SendOutcome:
    ok: bool
    error: str | None = None
    message_id: str | None = None
    sent: bool = False


@dataclass
class BatchResult:
    sent_count: int = 0
    errors: list[str] = field(default_factory=list)
    preview: list[dict[str, str]] = field(default_factory=list)
    blocked_reason: str | None = None


class MockGmailClient:
    """Stand-in for the Gmail API. It records calls and does not use the network."""

    def __init__(
        self,
        *,
        message_id: str = "mock-gmail-id",
        fail_with: str | None = None,
        account: str = "student@example.com",
    ) -> None:
        self.message_id = message_id
        self.fail_with = fail_with
        self.account = account
        self.calls: list[dict[str, str]] = []

    def send_message(self, *, to: str, subject: str, body: str) -> str:
        self.calls.append({"to": to, "subject": subject, "body": body})
        if self.fail_with:
            raise GmailSendError(self.fail_with)
        if not self.message_id:
            raise GmailSendError("Gmail did not return a message id.")
        return self.message_id


def secrets_dir() -> Path:
    return PROJECT_ROOT / "secrets"


def client_secret_path() -> Path:
    override = os.environ.get("GMAIL_CLIENT_SECRET_PATH", "").strip()
    return Path(override) if override else secrets_dir() / "gmail_client_secret.json"


def token_path() -> Path:
    override = os.environ.get("GMAIL_TOKEN_PATH", "").strip()
    return Path(override) if override else secrets_dir() / "gmail_token.json"


def account_path() -> Path:
    override = os.environ.get("GMAIL_ACCOUNT_PATH", "").strip()
    return Path(override) if override else secrets_dir() / "gmail_account.json"


def settings_path() -> Path:
    return get_data_dir() / "gmail_settings.json"


def gmail_connection_status() -> GmailStatus:
    """Report whether a local token exists. The token itself is not returned."""

    if not token_path().is_file():
        return GmailStatus(connected=False, account=None)
    return GmailStatus(connected=True, account=_stored_account())


def automatic_sending_enabled(path: Path | None = None) -> bool:
    file_path = path or settings_path()
    if not file_path.is_file():
        return False
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("automatic_sending") is True


def set_automatic_sending(enabled: bool, path: Path | None = None) -> None:
    write_json(path or settings_path(), {"automatic_sending": bool(enabled)})


def connect_gmail() -> GmailStatus:
    """Open Google's OAuth consent screen and store the token locally.

    This asks Google for permission. It does not ask for a Gmail password.
    """

    secret = client_secret_path()
    if not secret.is_file():
        return GmailStatus(
            connected=False,
            error=(
                "The OAuth client file is missing. Save it as "
                f"{secret} and try again. Do not enter a Gmail password."
            ),
        )
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        return GmailStatus(
            connected=False,
            error="Install google-api-python-client and google-auth-oauthlib to connect Gmail.",
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(secret), list(GMAIL_SCOPES))
    credentials = flow.run_local_server(port=0, open_browser=True)
    _write_private(token_path(), credentials.to_json())
    account = _email_from_id_token(getattr(credentials, "id_token", None))
    if account:
        _write_private(account_path(), json.dumps({"account": account}))
    return GmailStatus(connected=True, account=account)


def disconnect_gmail() -> GmailStatus:
    """Remove the local token. The OAuth client file is left in place."""

    for path in (token_path(), account_path()):
        if path.is_file():
            path.unlink()
    return GmailStatus(connected=False, account=None)


def client_for_user_action() -> tuple[object | None, str | None]:
    """Return a sender only after a person starts a send action.

    GMAIL_MOCK=1 uses the local mock and does not contact Gmail.
    """

    if os.environ.get("GMAIL_MOCK") == "1":
        return MockGmailClient(), None
    if not gmail_connection_status().connected:
        return None, "Gmail is not connected."
    try:
        return build_live_client(), None
    except GmailSendError as exc:
        return None, str(exc)


def build_live_client():
    """Build a Gmail API client from the stored token. Returns None if disconnected."""

    if not token_path().is_file():
        return None
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise GmailSendError(
            "Install google-api-python-client and google-auth-oauthlib to send through Gmail."
        ) from exc
    credentials = Credentials.from_authorized_user_file(str(token_path()), list(GMAIL_SCOPES))
    service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
    return LiveGmailClient(service, account=_stored_account())


class LiveGmailClient:
    """Official Gmail API sender. A returned message id means the API accepted it."""

    def __init__(self, service: object, account: str | None) -> None:
        self._service = service
        self.account = account

    def send_message(self, *, to: str, subject: str, body: str) -> str:
        raw = _raw_message(to=to, subject=subject, body=body, sender=self.account)
        try:
            result = (
                self._service.users()
                .messages()
                .send(userId="me", body={"raw": raw})
                .execute()
            )
        except Exception as exc:
            raise GmailSendError("Gmail did not accept the message.") from exc
        message_id = result.get("id") if isinstance(result, dict) else None
        if not isinstance(message_id, str) or not message_id.strip():
            raise GmailSendError("Gmail did not return a message id.")
        return message_id


def approve_and_send(
    draft_index: int,
    *,
    client: object,
    drafts_file: Path | None = None,
    shortlist_file: Path | None = None,
    tracker_file: Path | None = None,
    now: datetime | None = None,
) -> SendOutcome:
    """Send one saved draft. A failure leaves the files unchanged."""

    drafts_target = drafts_file or drafts_path()
    tracker_target = tracker_file or tracker_path()
    drafts = _read_list(drafts_target)
    shortlist = _read_list(shortlist_file or shortlist_path())
    tracker = _read_list(tracker_target)
    if draft_index < 0 or draft_index >= len(drafts) or not isinstance(drafts[draft_index], dict):
        return SendOutcome(ok=False, error="That draft could not be found.")
    draft = drafts[draft_index]
    outcome = _attempt_send(draft, client, shortlist)
    if not outcome.ok or not outcome.message_id:
        return outcome
    sent_at = _stamp(now)
    _mark_draft_sent(draft, sent_at=sent_at, message_id=outcome.message_id)
    _mark_tracker(tracker, draft, sent_at=sent_at, message_id=outcome.message_id)
    write_json(drafts_target, drafts)
    write_json(tracker_target, tracker)
    outcome.sent = True
    return outcome


def batch_preview(
    *,
    limit: int = AUTOMATIC_BATCH_LIMIT,
    drafts_file: Path | None = None,
    shortlist_file: Path | None = None,
) -> list[dict[str, str]]:
    """List drafts a confirmed batch would send. This does not send them."""

    drafts = _read_list(drafts_file or drafts_path())
    shortlist = _read_list(shortlist_file or shortlist_path())
    chosen = _eligible(drafts, shortlist)[: max(0, limit)]
    return [
        {
            "professor_name": str(draft.get("professor_name") or ""),
            "recipient_email": str(draft.get("recipient_email") or ""),
            "subject": str(draft.get("subject") or ""),
        }
        for _index, draft in chosen
    ]


def send_automatic_batch(
    *,
    confirmed: bool,
    client: object | None,
    limit: int = AUTOMATIC_BATCH_LIMIT,
    drafts_file: Path | None = None,
    shortlist_file: Path | None = None,
    tracker_file: Path | None = None,
    settings_file: Path | None = None,
    now: datetime | None = None,
) -> BatchResult:
    """Send a small confirmed batch. Automatic sending is off unless the setting says otherwise."""

    drafts_target = drafts_file or drafts_path()
    tracker_target = tracker_file or tracker_path()
    drafts = _read_list(drafts_target)
    shortlist = _read_list(shortlist_file or shortlist_path())
    preview = batch_preview(limit=limit, drafts_file=drafts_target, shortlist_file=shortlist_file or shortlist_path())
    if not automatic_sending_enabled(settings_file):
        return BatchResult(preview=preview, blocked_reason="Automatic sending is off.")
    if not confirmed:
        return BatchResult(preview=preview, blocked_reason="Confirmation is required before the batch can send.")
    if client is None:
        return BatchResult(preview=preview, blocked_reason="Gmail is not connected.")
    tracker = _read_list(tracker_target)
    sent_count = 0
    errors: list[str] = []
    changed = False
    for index, draft in _eligible(drafts, shortlist)[: max(0, limit)]:
        outcome = _attempt_send(draft, client, shortlist)
        if not outcome.ok or not outcome.message_id:
            name = str(draft.get("professor_name") or "Professor")
            errors.append(f"{name}: {outcome.error or 'Gmail did not accept the message.'}")
            continue
        sent_at = _stamp(now)
        _mark_draft_sent(draft, sent_at=sent_at, message_id=outcome.message_id)
        _mark_tracker(tracker, draft, sent_at=sent_at, message_id=outcome.message_id)
        sent_count += 1
        changed = True
    if changed:
        write_json(drafts_target, drafts)
        write_json(tracker_target, tracker)
    return BatchResult(sent_count=sent_count, errors=errors, preview=preview)


def _attempt_send(draft: dict, client: object, shortlist: list) -> SendOutcome:
    reason = _refusal_reason(draft, shortlist)
    if reason:
        already = _already_sent(draft)
        return SendOutcome(ok=False, error=reason, sent=already)
    recipient = published_email(draft.get("recipient_email"))
    subject = str(draft.get("subject") or "")
    body = str(draft.get("body") or "")
    try:
        message_id = client.send_message(to=recipient, subject=subject, body=body)
    except GmailSendError as exc:
        return SendOutcome(ok=False, error=str(exc) or "Gmail did not accept the message.")
    except Exception:
        return SendOutcome(ok=False, error="Gmail did not accept the message.")
    if not isinstance(message_id, str) or not message_id.strip():
        return SendOutcome(ok=False, error="Gmail did not return a message id.")
    return SendOutcome(ok=True, message_id=message_id)


def _refusal_reason(draft: dict, shortlist: list) -> str | None:
    if _already_sent(draft):
        return "This draft was already sent."
    if draft.get("draft_status") != "draft":
        return "This draft is not ready to send."
    recipient = published_email(draft.get("recipient_email") if isinstance(draft.get("recipient_email"), str) else None)
    if not recipient:
        if not draft.get("recipient_email"):
            return "Recipient email is missing."
        return "Recipient email is not a published academic address."
    row = _shortlist_row(draft, shortlist)
    if not _passes_verification(row):
        return "This professor is not officially verified."
    official = published_email(row.get("public_email") if isinstance(row.get("public_email"), str) else None)
    if official != recipient:
        return "Recipient email does not match the verified public academic email."
    if row.get("research_confidence") not in {"medium", "high"}:
        return "Research confidence is not medium or high."
    if not _has_research_evidence(draft, row):
        return "This draft has no stored research evidence."
    if not str(draft.get("subject") or "").strip() or not str(draft.get("body") or "").strip():
        return "The saved subject and body are required."
    return None


def _passes_verification(row: dict | None) -> bool:
    if not isinstance(row, dict):
        return False
    if row.get("verification_status") != "officially_verified":
        return False
    if row.get("official_source") not in OFFICIAL_SOURCE_KINDS:
        return False
    return bool(row.get("official_profile_url") or row.get("official_department_url"))


def _has_research_evidence(draft: dict, row: dict) -> bool:
    areas = [
        area
        for area in draft.get("matched_research_areas") or []
        if isinstance(area, str) and area.strip() and area.casefold() != "computer science"
    ]
    if not areas:
        return False
    for paper in draft.get("paper_evidence") or []:
        if isinstance(paper, dict) and str(paper.get("title") or "").strip():
            return True
    for url in draft.get("evidence_urls") or []:
        if isinstance(url, str) and url.strip():
            return True
    for item in row.get("evidence") or []:
        if isinstance(item, str) and item.strip():
            return True
    return False


def _eligible(drafts: list, shortlist: list) -> list[tuple[int, dict]]:
    chosen: list[tuple[int, dict]] = []
    for index, draft in enumerate(drafts):
        if isinstance(draft, dict) and _refusal_reason(draft, shortlist) is None:
            chosen.append((index, draft))
    return chosen


def _already_sent(draft: dict) -> bool:
    if draft.get("draft_status") == "sent":
        return True
    prep = draft.get("gmail_preparation")
    return isinstance(prep, dict) and prep.get("sent") is True


def _shortlist_row(draft: dict, shortlist: list) -> dict | None:
    name = draft.get("professor_name")
    university = draft.get("university")
    for row in shortlist:
        if isinstance(row, dict) and row.get("name") == name and row.get("university") == university:
            return row
    return None


def _mark_draft_sent(draft: dict, *, sent_at: str, message_id: str) -> None:
    recipient = published_email(draft.get("recipient_email"))
    prep = draft.get("gmail_preparation")
    if not isinstance(prep, dict):
        prep = {}
    prep["provider"] = "gmail"
    prep["to"] = recipient
    prep["subject"] = draft.get("subject")
    prep["sent"] = True
    prep["sent_at"] = sent_at
    prep["gmail_message_id"] = message_id
    draft["gmail_preparation"] = prep
    draft["draft_status"] = "sent"


def _mark_tracker(rows: list, draft: dict, *, sent_at: str, message_id: str) -> None:
    recipient = published_email(draft.get("recipient_email"))
    name = draft.get("professor_name")
    university = draft.get("university")
    for row in rows:
        if isinstance(row, dict) and row.get("professor") == name and row.get("university") == university:
            row["email_status"] = "sent"
            row["date_contacted"] = sent_at
            row["sent_at"] = sent_at
            row["gmail_message_id"] = message_id
            row["email"] = recipient
            row["notes"] = f"Sent through Gmail. Message id: {message_id}"
            return
    rows.append(
        {
            "professor": name,
            "university": university,
            "email": recipient,
            "match_score": None,
            "email_status": "sent",
            "date_contacted": sent_at,
            "follow_up_date": None,
            "response_status": None,
            "notes": f"Sent through Gmail. Message id: {message_id}",
            "sent_at": sent_at,
            "gmail_message_id": message_id,
        }
    )


def _stamp(now: datetime | None) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.isoformat()


def _read_list(path: Path) -> list:
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return payload if isinstance(payload, list) else []


def _stored_account() -> str | None:
    path = account_path()
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    account = payload.get("account") if isinstance(payload, dict) else None
    return account if isinstance(account, str) and account.strip() else None


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")


def _email_from_id_token(token: object) -> str | None:
    if not isinstance(token, str) or token.count(".") < 2:
        return None
    payload = token.split(".")[1]
    padding = "=" * (-len(payload) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + padding))
    except (json.JSONDecodeError, ValueError):
        return None
    email = data.get("email") if isinstance(data, dict) else None
    return email if isinstance(email, str) and email.strip() else None


def _raw_message(*, to: str, subject: str, body: str, sender: str | None) -> str:
    message = EmailMessage()
    message["To"] = to
    if sender:
        message["From"] = sender
    message["Subject"] = subject
    message.set_content(body)
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")

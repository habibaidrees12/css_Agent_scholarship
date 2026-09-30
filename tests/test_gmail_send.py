"""Gmail send tests. The mock client never contacts Gmail."""

from __future__ import annotations

import ast
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.gmail_send import (
    AUTOMATIC_BATCH_LIMIT,
    MockGmailClient,
    approve_and_send,
    automatic_sending_enabled,
    send_automatic_batch,
    set_automatic_sending,
)
from app.outreach import write_json

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
OFFICIAL = "https://cs.example.edu.cn/people/test"


def _draft(**overrides: object) -> dict:
    data = {
        "professor_name": "Ada Example",
        "university": "Test University",
        "recipient_email": "ada@example.edu.cn",
        "subject": "CSC scholarship inquiry",
        "body": "Dear Professor Ada Example,\n\nI am writing about a CSC application.",
        "matched_research_areas": ["Database Systems"],
        "evidence_urls": [OFFICIAL],
        "paper_evidence": [{"title": "Database Systems for Records", "year": 2026, "abstract": None}],
        "draft_status": "draft",
        "gmail_preparation": {"provider": "gmail", "to": "ada@example.edu.cn", "subject": "CSC scholarship inquiry", "sent": False},
    }
    data.update(overrides)
    return data


def _shortlist(**overrides: object) -> dict:
    data = {
        "name": "Ada Example",
        "university": "Test University",
        "public_email": "ada@example.edu.cn",
        "official_profile_url": OFFICIAL,
        "official_source": "official_professor_or_lab_page",
        "verification_status": "officially_verified",
        "research_confidence": "high",
        "evidence": ['"Database Systems" found in research interests'],
        "matched_areas": ["Database Systems"],
    }
    data.update(overrides)
    return data


def _tracker() -> dict:
    return {
        "professor": "Ada Example",
        "university": "Test University",
        "email": "ada@example.edu.cn",
        "match_score": 50,
        "email_status": "draft_ready",
        "date_contacted": None,
        "follow_up_date": None,
        "response_status": None,
        "notes": "Draft is waiting for explicit approval. It has not been sent.",
    }


class GmailSendTests(unittest.TestCase):
    def _files(self, folder: Path, drafts: list, shortlist: list | None = None, tracker: list | None = None) -> dict:
        drafts_file = folder / "email_drafts.json"
        shortlist_file = folder / "professor_shortlist.json"
        tracker_file = folder / "application_tracker.json"
        settings_file = folder / "gmail_settings.json"
        write_json(drafts_file, drafts)
        write_json(shortlist_file, shortlist if shortlist is not None else [_shortlist()])
        write_json(tracker_file, tracker if tracker is not None else [_tracker()])
        return {
            "drafts_file": drafts_file,
            "shortlist_file": shortlist_file,
            "tracker_file": tracker_file,
            "settings_file": settings_file,
        }

    def _send_args(self, paths: dict) -> dict:
        return {key: paths[key] for key in ("drafts_file", "shortlist_file", "tracker_file")}

    def test_successful_send_marks_the_draft_and_tracker(self) -> None:
        client = MockGmailClient(message_id="gmail-msg-1")
        with tempfile.TemporaryDirectory() as folder:
            paths = self._files(Path(folder), [_draft()])
            outcome = approve_and_send(0, client=client, now=NOW, **self._send_args(paths))
            saved = json.loads(paths["drafts_file"].read_text(encoding="utf-8"))
            tracker = json.loads(paths["tracker_file"].read_text(encoding="utf-8"))

        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.message_id, "gmail-msg-1")
        self.assertEqual(client.calls, [
            {
                "to": "ada@example.edu.cn",
                "subject": "CSC scholarship inquiry",
                "body": "Dear Professor Ada Example,\n\nI am writing about a CSC application.",
            }
        ])
        self.assertEqual(saved[0]["draft_status"], "sent")
        self.assertTrue(saved[0]["gmail_preparation"]["sent"])
        self.assertEqual(saved[0]["gmail_preparation"]["sent_at"], NOW.isoformat())
        self.assertEqual(saved[0]["gmail_preparation"]["to"], "ada@example.edu.cn")
        self.assertEqual(saved[0]["gmail_preparation"]["gmail_message_id"], "gmail-msg-1")
        self.assertEqual(tracker[0]["email_status"], "sent")
        self.assertEqual(tracker[0]["gmail_message_id"], "gmail-msg-1")
        self.assertEqual(tracker[0]["sent_at"], NOW.isoformat())

    def test_failed_send_keeps_the_draft(self) -> None:
        client = MockGmailClient(fail_with="Gmail rejected the message.")
        with tempfile.TemporaryDirectory() as folder:
            original = [_draft()]
            paths = self._files(Path(folder), original)
            before = paths["drafts_file"].read_text(encoding="utf-8")
            outcome = approve_and_send(0, client=client, now=NOW, **self._send_args(paths))
            after = paths["drafts_file"].read_text(encoding="utf-8")
            tracker = json.loads(paths["tracker_file"].read_text(encoding="utf-8"))

        self.assertFalse(outcome.ok)
        self.assertFalse(outcome.sent)
        self.assertEqual(outcome.error, "Gmail rejected the message.")
        self.assertEqual(before, after)
        self.assertEqual(tracker[0]["email_status"], "draft_ready")
        self.assertEqual(len(client.calls), 1)

    def test_missing_email_is_not_sent(self) -> None:
        client = MockGmailClient()
        with tempfile.TemporaryDirectory() as folder:
            paths = self._files(
                Path(folder),
                [_draft(recipient_email=None)],
                [_shortlist(public_email=None)],
            )
            outcome = approve_and_send(0, client=client, **self._send_args(paths))

        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error, "Recipient email is missing.")
        self.assertEqual(client.calls, [])

    def test_guessed_email_is_not_sent(self) -> None:
        client = MockGmailClient()
        with tempfile.TemporaryDirectory() as folder:
            paths = self._files(Path(folder), [_draft(recipient_email="bin.cuipku.edu.cn")])
            outcome = approve_and_send(0, client=client, **self._send_args(paths))

        self.assertFalse(outcome.ok)
        self.assertIn("not a published", outcome.error or "")
        self.assertEqual(client.calls, [])

    def test_already_sent_draft_is_not_sent_again(self) -> None:
        client = MockGmailClient()
        sent = _draft(draft_status="sent", gmail_preparation={"provider": "gmail", "sent": True, "gmail_message_id": "old"})
        with tempfile.TemporaryDirectory() as folder:
            paths = self._files(Path(folder), [sent])
            outcome = approve_and_send(0, client=client, **self._send_args(paths))

        self.assertFalse(outcome.ok)
        self.assertTrue(outcome.sent)
        self.assertEqual(outcome.error, "This draft was already sent.")
        self.assertEqual(client.calls, [])

    def test_unverified_professor_is_not_sent(self) -> None:
        client = MockGmailClient()
        with tempfile.TemporaryDirectory() as folder:
            paths = self._files(Path(folder), [_draft()], [_shortlist(verification_status="unverified")])
            outcome = approve_and_send(0, client=client, **self._send_args(paths))

        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error, "This professor is not officially verified.")
        self.assertEqual(client.calls, [])

    def test_automatic_sending_off_sends_nothing(self) -> None:
        client = MockGmailClient()
        with tempfile.TemporaryDirectory() as folder:
            paths = self._files(Path(folder), [_draft()])
            self.assertFalse(automatic_sending_enabled(paths["settings_file"]))
            result = send_automatic_batch(confirmed=True, client=client, now=NOW, **paths)

        self.assertEqual(result.sent_count, 0)
        self.assertEqual(result.blocked_reason, "Automatic sending is off.")
        self.assertEqual(client.calls, [])

    def test_automatic_batch_stops_at_five_and_needs_confirmation(self) -> None:
        drafts = []
        shortlist = []
        for index in range(7):
            name = f"Professor {index}"
            email = f"p{index}@example.edu.cn"
            drafts.append(_draft(professor_name=name, recipient_email=email, university=f"University {index}"))
            shortlist.append(_shortlist(name=name, public_email=email, university=f"University {index}"))
        client = MockGmailClient(message_id="batch-id")
        with tempfile.TemporaryDirectory() as folder:
            paths = self._files(Path(folder), drafts, shortlist, tracker=[])
            set_automatic_sending(True, paths["settings_file"])
            unconfirmed = send_automatic_batch(confirmed=False, client=client, **paths)
            self.assertEqual(unconfirmed.sent_count, 0)
            self.assertEqual(len(unconfirmed.preview), AUTOMATIC_BATCH_LIMIT)
            self.assertEqual(client.calls, [])
            confirmed = send_automatic_batch(confirmed=True, client=client, now=NOW, **paths)
            saved = json.loads(paths["drafts_file"].read_text(encoding="utf-8"))

        self.assertEqual(confirmed.sent_count, 5)
        self.assertEqual(len(client.calls), 5)
        self.assertEqual([item["draft_status"] for item in saved], ["sent"] * 5 + ["draft", "draft"])
        self.assertEqual(client.calls[0]["to"], "p0@example.edu.cn")
        self.assertEqual(client.calls[4]["to"], "p4@example.edu.cn")

    def test_module_does_not_import_smtplib(self) -> None:
        tree = ast.parse((ROOT / "app" / "gmail_send.py").read_text(encoding="utf-8"))
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        self.assertNotIn("smtplib", imported)


if __name__ == "__main__":
    unittest.main()

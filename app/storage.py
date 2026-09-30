"""JSON storage and safe deduplication for discovered professors.

data/professors.json starts as an empty list. Records are added only when a
caller passes them in. This module does not invent professors and does not
send email.
"""

from __future__ import annotations

import hashlib
import json
import urllib.parse
from pathlib import Path

from app.config import get_professors_path
from app.matching import match_professor
from app.models import VERIFICATION_STATUSES, PaperRecord, ProfessorRecord, ResearchInterest, UserProfile


def normalize_label(value: str | None) -> str:
    """Compare names and universities by case and spacing, not by similarity."""

    if not value:
        return ""
    return " ".join(value.casefold().split())


def normalize_email(value: str | None) -> str:
    if not value:
        return ""
    return value.strip().casefold()


def normalize_url(value: str | None) -> str:
    """Normalize an official profile URL. A blank URL stays blank."""

    if not value or not value.strip():
        return ""
    parts = urllib.parse.urlsplit(value.strip())
    if not parts.scheme or not parts.netloc:
        return ""
    return urllib.parse.urlunsplit(
        (
            parts.scheme.casefold(),
            parts.netloc.casefold(),
            parts.path.rstrip("/"),
            parts.query,
            "",
        )
    )


def is_same_professor(left: ProfessorRecord, right: ProfessorRecord) -> bool:
    """Decide whether two records are the same person.

    A match requires an official profile URL, or the same email and exact
    name, or the same exact name and university. Similar names are not enough.
    """

    left_url = normalize_url(left.professor_profile_url)
    right_url = normalize_url(right.professor_profile_url)
    if left_url and left_url == right_url:
        return True

    left_name = normalize_label(left.name)
    right_name = normalize_label(right.name)
    left_email = normalize_email(left.public_email)
    right_email = normalize_email(right.public_email)
    if left_email and left_email == right_email and left_name and left_name == right_name:
        return True

    left_university = normalize_label(left.university)
    right_university = normalize_label(right.university)
    if left_name and left_name == right_name and left_university and left_university == right_university:
        return True
    return False


def merge_professors(existing: ProfessorRecord, incoming: ProfessorRecord) -> ProfessorRecord:
    """Fill gaps from a second source. Conflicting facts are kept, not guessed."""

    notes: list[str] = []
    merged = ProfessorRecord.from_dict(existing.to_dict())
    merged.title = _prefer(merged.title, incoming.title, "title", notes)
    merged.department = _prefer(merged.department, incoming.department, "department", notes)
    merged.country = _prefer(merged.country, incoming.country, "country", notes)
    merged.city = _prefer(merged.city, incoming.city, "city", notes)
    merged.university_website = _prefer(
        merged.university_website, incoming.university_website, "university website", notes
    )
    merged.professor_profile_url = _prefer(
        merged.professor_profile_url, incoming.professor_profile_url, "profile URL", notes
    )
    merged.public_email = _prefer(merged.public_email, incoming.public_email, "email", notes)
    for field_name in (
        "official_profile_url",
        "official_department_url",
        "official_source",
        "official_email",
        "official_department",
        "official_city",
        "official_university",
    ):
        current = getattr(merged, field_name)
        incoming_value = getattr(incoming, field_name)
        setattr(merged, field_name, _prefer(current, incoming_value, field_name, notes))
    for field_name, source_url in incoming.official_field_sources.items():
        merged.official_field_sources.setdefault(field_name, source_url)
    _keep_stronger_verification(merged, incoming)
    merged.research_interests = _union(merged.research_interests, incoming.research_interests)
    merged.papers = _merge_papers(merged.papers, incoming.papers)
    merged.source_urls = _union(merged.source_urls, incoming.source_urls)
    if incoming.verified and not merged.verified:
        merged.verified = True
        merged.last_verified = incoming.last_verified
        merged.source_kind = incoming.source_kind or merged.source_kind
    if incoming.match_result is not None:
        merged.match_result = incoming.match_result
    extra_notes = [item for item in (merged.notes, incoming.notes, *notes) if item]
    merged.notes = " ".join(extra_notes).strip()
    return merged


def deduplicate_records(records: list[ProfessorRecord]) -> list[ProfessorRecord]:
    """Collapse records that pass is_same_professor. Order follows the first sighting."""

    merged: list[ProfessorRecord] = []
    for record in records:
        for index, existing in enumerate(merged):
            if is_same_professor(existing, record):
                merged[index] = merge_professors(existing, record)
                break
        else:
            merged.append(record)
    return merged


def record_errors(record: ProfessorRecord) -> list[str]:
    """Check a record before it is stored. A missing email or city is allowed."""

    errors = []
    if not record.name.strip():
        errors.append("A professor record needs a name from the source.")
    if not record.university.strip():
        errors.append("A professor record needs a university from the source.")
    if record.public_email is not None and "@" not in record.public_email:
        errors.append("public_email must be null or an email address from the source.")
    if record.official_email is not None and "@" not in record.official_email:
        errors.append("official_email must be null or an email published by the source.")
    if record.verification_status not in VERIFICATION_STATUSES:
        errors.append(
            "verification_status must be unverified, partially_verified, officially_verified, or verification_failed."
        )
    return errors


def make_professor_id(record: ProfessorRecord) -> str:
    """Build a stable local id from the name, university, and official profile URL."""

    raw = "|".join(
        [
            normalize_label(record.name),
            normalize_label(record.university),
            normalize_url(record.professor_profile_url),
        ]
    )
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"prof_{digest}"


class ProfessorStore:
    """Read and write the professor list in one JSON file."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or get_professors_path()

    def load(self) -> list[ProfessorRecord]:
        if not self.path.exists():
            return []
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("The professor file must contain a JSON list.")
        return [ProfessorRecord.from_dict(item) for item in raw]

    def save(self, records: list[ProfessorRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = [record.to_dict() for record in records]
        with self.path.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")

    def add(self, record: ProfessorRecord) -> ProfessorRecord:
        """Add a professor, or merge it when the same person is already stored."""

        errors = record_errors(record)
        if errors:
            raise ValueError(" ".join(errors))
        records = self.load()
        for index, existing in enumerate(records):
            if is_same_professor(existing, record):
                merged = merge_professors(existing, record)
                records[index] = merged
                self.save(records)
                return merged
        if not record.professor_id:
            record.professor_id = make_professor_id(record)
        records.append(record)
        self.save(records)
        return record

    def update(self, professor_id: str, changes: dict) -> ProfessorRecord:
        """Update fields that were supplied. Blank email or city is stored as null."""

        records = self.load()
        for index, existing in enumerate(records):
            if existing.professor_id != professor_id:
                continue
            updated = ProfessorRecord.from_dict(existing.to_dict())
            _apply_changes(updated, changes)
            errors = record_errors(updated)
            if errors:
                raise ValueError(" ".join(errors))
            records[index] = updated
            self.save(records)
            return updated
        raise KeyError(f"No stored professor has id {professor_id}.")

    def all(self) -> list[ProfessorRecord]:
        return self.load()

    def filtered(
        self,
        *,
        country: str | None = None,
        city: str | None = None,
        university: str | None = None,
        research_area: str | None = None,
    ) -> list[ProfessorRecord]:
        """Return stored professors that match the requested facts.

        Research-area matching uses the existing matcher, so a department does
        not have to be named exactly Software Engineering.
        """

        records = self.load()
        if country:
            records = [item for item in records if normalize_label(item.country) == normalize_label(country)]
        if city:
            records = [item for item in records if normalize_label(item.city) == normalize_label(city)]
        if university:
            records = [
                item for item in records if normalize_label(item.university) == normalize_label(university)
            ]
        if research_area:
            records = [item for item in records if area_matches(item, research_area)]
        return records


def area_matches(record: ProfessorRecord, research_area: str) -> bool:
    """True when the area appears in the department, interests, or papers."""

    probe = UserProfile(
        research_interests=[ResearchInterest(research_area)],
        preferred_fields=[research_area],
    )
    result = match_professor(probe, record.to_match_professor())
    return result.research_score > 0


_STATUS_RANK = {
    "unverified": 0,
    "verification_failed": 1,
    "partially_verified": 2,
    "officially_verified": 3,
}


def _keep_stronger_verification(merged: ProfessorRecord, incoming: ProfessorRecord) -> None:
    """Keep a successful verification when a later attempt fails."""

    current_rank = _STATUS_RANK.get(merged.verification_status, 0)
    incoming_rank = _STATUS_RANK.get(incoming.verification_status, 0)
    if incoming_rank > current_rank:
        merged.verification_status = incoming.verification_status
        if incoming.verified_at:
            merged.verified_at = incoming.verified_at
    if incoming.verification_notes and incoming.verification_notes not in merged.verification_notes:
        merged.verification_notes = " ".join(
            part for part in (merged.verification_notes, incoming.verification_notes) if part
        ).strip()


def _prefer(current: str | None, incoming: str | None, label: str, notes: list[str]) -> str | None:
    if not current:
        return incoming
    if not incoming or normalize_label(current) == normalize_label(incoming):
        return current
    notes.append(f"Kept the existing {label} because another source reported a different value.")
    return current


def _union(left: list[str], right: list[str]) -> list[str]:
    values = list(left)
    seen = {normalize_label(item) for item in values}
    for item in right:
        key = normalize_label(item)
        if key and key not in seen:
            values.append(item)
            seen.add(key)
    return values


def _merge_papers(left: list[PaperRecord], right: list[PaperRecord]) -> list[PaperRecord]:
    merged = list(left)
    seen = {_paper_key(paper) for paper in merged}
    for paper in right:
        key = _paper_key(paper)
        if key in seen:
            continue
        merged.append(paper)
        seen.add(key)
    return merged


def _paper_key(paper: PaperRecord) -> tuple[str, int | None]:
    return (normalize_label(paper.title), paper.year)


def _apply_changes(record: ProfessorRecord, changes: dict) -> None:
    allowed = {
        "name",
        "title",
        "university",
        "department",
        "country",
        "city",
        "university_website",
        "professor_profile_url",
        "public_email",
        "research_interests",
        "papers",
        "source_urls",
        "last_verified",
        "verified",
        "source_kind",
        "verification_status",
        "verification_notes",
        "verified_at",
        "official_profile_url",
        "official_department_url",
        "official_source",
        "official_email",
        "official_department",
        "official_city",
        "official_university",
        "official_field_sources",
        "match_result",
        "notes",
    }
    unknown = sorted(set(changes) - allowed)
    if unknown:
        joined = ", ".join(unknown)
        raise ValueError(f"Unknown professor fields: {joined}.")
    nullable = {
        "title",
        "department",
        "country",
        "city",
        "university_website",
        "professor_profile_url",
        "public_email",
        "last_verified",
        "source_kind",
        "verified_at",
        "official_profile_url",
        "official_department_url",
        "official_source",
        "official_email",
        "official_department",
        "official_city",
        "official_university",
    }
    for key, value in changes.items():
        if key in nullable and value == "":
            value = None
        if key == "papers" and isinstance(value, list):
            value = [
                item if isinstance(item, PaperRecord) else PaperRecord.from_dict(item) for item in value
            ]
        setattr(record, key, value)

"""Verify who a professor is and which university officially employs them.

Matching answers whether the research fits the user profile. This module
answers whether an official university source supports the candidate's
identity and affiliation. It does not score research, does not merge similar
names, and does not contact professors.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from app.models import ProfessorRecord
from app.sources import classify_public_url, is_generic_campus_page
from app.university_sources import (
    OFFICIAL_SOURCE_KINDS,
    OfficialPage,
    OfflineUniversitySource,
    UniversitySource,
    is_official_university_url,
)

# This module has no mail delivery. The flag is here so tests can check that.
SENDS_EMAIL = False

_REPUTABLE_KIND = "reputable_academic_paper_source"


def verify_candidates(
    records: list[ProfessorRecord],
    source: UniversitySource | None = None,
    *,
    verified_at: str | None = None,
) -> list[ProfessorRecord]:
    """Verify each candidate independently. Similar names are not merged."""

    return [verify_professor(record, source, verified_at=verified_at) for record in records]


def verify_professor(
    record: ProfessorRecord,
    source: UniversitySource | None = None,
    *,
    verified_at: str | None = None,
) -> ProfessorRecord:
    """Enrich one candidate from official pages the provider can actually read.

    The input record is not modified. OpenAlex fields stay in place. Official
    fields stay null when a page was not read or the identity did not match.
    """

    provider = source or OfflineUniversitySource()
    updated = ProfessorRecord.from_dict(record.to_dict())
    notes: list[str] = []
    accepted: list[tuple[OfficialPage, list[str]]] = []
    rejected: list[str] = []
    unread_official: list[str] = []

    for url in _candidate_urls(updated, provider):
        if not is_official_university_url(url):
            continue
        page = provider.read_page(url)
        if page is None or not is_official_university_url(page.url):
            unread_official.append(url)
            continue
        ok, signals, reason = _identity_decision(updated, page)
        if ok:
            accepted.append((page, signals))
        elif reason:
            rejected.append(reason)

    if accepted:
        page, signals = _best_page(accepted)
        _apply_official_page(updated, page, notes)
        notes.append("Identity signals: " + ", ".join(signals) + ".")
        updated.verification_status = "officially_verified"
    elif rejected:
        notes.extend(rejected)
        notes.append("Existing candidate values were kept. Official fields were left empty.")
        updated.verification_status = "verification_failed"
    elif _has_reputable_provenance(updated):
        if unread_official:
            notes.append(
                "An official university URL was listed, but its page was not read, so it was not accepted."
            )
        notes.append(
            "Name and university come from a reputable academic source. "
            "An official university profile has not confirmed this identity."
        )
        updated.verification_status = "partially_verified"
    else:
        if unread_official:
            notes.append(
                "An official university URL was listed, but its page was not read, so it was not accepted."
            )
        updated.verification_status = "unverified"

    updated.verification_notes = _join(notes)
    if updated.verification_status == "unverified":
        updated.verified_at = None
    else:
        updated.verified_at = _timestamp(verified_at)
    return updated


def published_email(value: str | None) -> str | None:
    """Return an email only when a source already published one.

    This does not build an address from a name, initials, or a university domain.
    """

    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if cleaned.count("@") != 1 or any(mark in cleaned for mark in (" ", "/", "\\")):
        return None
    local, domain = cleaned.split("@")
    if not local or "." not in domain:
        return None
    return cleaned


def _apply_official_page(record: ProfessorRecord, page: OfficialPage, notes: list[str]) -> None:
    kind = classify_public_url(page.url)
    if kind not in OFFICIAL_SOURCE_KINDS:
        return
    record.official_source = kind
    if kind == "official_professor_or_lab_page" or (
        kind == "official_university_domain" and not is_generic_campus_page(page.url)
    ):
        _set_official(record, "official_profile_url", page.url, page.url)
    if page.department_url and is_official_university_url(page.department_url):
        _set_official(record, "official_department_url", page.department_url, page.department_url)
    elif kind == "official_department_page":
        _set_official(record, "official_department_url", page.url, page.url)

    if page.university and page.university.strip():
        official_university = page.university.strip()
        if record.university.strip() and not _exact(record.university, official_university):
            notes.append(
                f"OpenAlex university '{record.university}' conflicts with official university "
                f"'{official_university}' at {page.url}. The official affiliation is stored on "
                "official_university. The OpenAlex university was not changed."
            )
        _set_official(record, "official_university", official_university, page.url)

    if page.department and page.department.strip():
        official_department = page.department.strip()
        if record.department and not _exact(record.department, official_department):
            notes.append(
                f"OpenAlex department '{record.department}' conflicts with official department "
                f"'{official_department}' at {page.url}. The OpenAlex department was not changed."
            )
        _set_official(record, "official_department", official_department, page.url)

    if page.city and page.city.strip():
        official_city = page.city.strip()
        if record.city and not _exact(record.city, official_city):
            notes.append(
                f"OpenAlex city '{record.city}' conflicts with official city '{official_city}' "
                f"at {page.url}. The OpenAlex city was not changed."
            )
        _set_official(record, "official_city", official_city, page.url)

    email = published_email(page.email)
    if email:
        if record.public_email and not _exact(record.public_email, email):
            notes.append(
                f"OpenAlex email '{record.public_email}' conflicts with the email published at {page.url}. "
                "The OpenAlex email was not changed."
            )
        _set_official(record, "official_email", email, page.url)

    record.source_urls = _add_url(record.source_urls, page.url)


def _set_official(record: ProfessorRecord, field_name: str, value: str, source_url: str) -> None:
    setattr(record, field_name, value)
    record.official_field_sources[field_name] = source_url


def _identity_decision(record: ProfessorRecord, page: OfficialPage) -> tuple[bool, list[str], str]:
    """Require a normalized name plus one other signal. A lookalike name is not enough."""

    page_name = page.name.strip() if isinstance(page.name, str) else ""
    if not names_match(record.name, page_name):
        shown = page_name or "missing"
        if is_generic_campus_page(page.url):
            return (
                False,
                [],
                f"Generic university page {page.url} is not a professor profile, and page name "
                f"'{shown}' does not match '{record.name}'.",
            )
        return (
            False,
            [],
            f"Identity mismatch at {page.url}: page name '{shown}' does not match '{record.name}'.",
        )
    if is_generic_campus_page(page.url):
        return (
            False,
            [],
            f"Generic university page {page.url} is not proof of identity for '{record.name}'.",
        )

    signals = ["name"]
    if _university_on_page(record.university, page):
        signals.append("university")
    if record.department and page.department and (
        _exact(record.department, page.department) or _contains_phrase(page.department, record.department)
    ):
        signals.append("department")
    if _specific_shared(record.research_interests, page.research_areas):
        signals.append("research")
    if _shared([paper.title for paper in record.papers], page.paper_titles):
        signals.append("papers")

    extra = [item for item in signals if item != "name"]
    if not extra:
        return False, signals, f"Name alone is not enough to verify '{record.name}' at {page.url}."

    universities_conflict = bool(page.university) and not _university_agrees(record.university, page.university)
    independent = [item for item in extra if item in {"department", "research", "papers"}]
    if universities_conflict and not independent:
        return (
            False,
            signals,
            f"University conflict for '{record.name}' at {page.url} is not supported by department, research, or papers.",
        )
    if _is_ambiguous_name(record.name) and len(extra) < 2:
        return (
            False,
            signals,
            f"The name '{record.name}' is ambiguous and needs more than one supporting signal at {page.url}.",
        )
    return True, signals, ""


def _best_page(accepted: list[tuple[OfficialPage, list[str]]]) -> tuple[OfficialPage, list[str]]:
    def rank(item: tuple[OfficialPage, list[str]]) -> tuple[int, int]:
        page, signals = item
        kind = classify_public_url(page.url) or ""
        kind_rank = {
            "official_professor_or_lab_page": 3,
            "official_department_page": 2,
            "official_university_domain": 1,
        }.get(kind, 0)
        return (kind_rank, len(signals))

    return max(accepted, key=rank)


def _candidate_urls(record: ProfessorRecord, source: UniversitySource) -> list[str]:
    raw: list[str] = []
    for url in (record.professor_profile_url, record.university_website, *record.source_urls):
        if url and url.strip():
            raw.append(url.strip())
    raw.extend(url.strip() for url in source.candidate_urls(record) if url and url.strip())
    unique: list[str] = []
    seen: set[str] = set()
    for url in raw:
        key = url.rstrip("/").casefold()
        if key not in seen:
            seen.add(key)
            unique.append(url)
    return unique


def _has_reputable_provenance(record: ProfessorRecord) -> bool:
    if not record.name.strip() or not record.university.strip():
        return False
    for url in (record.professor_profile_url, record.university_website, *record.source_urls):
        if url and classify_public_url(url) == _REPUTABLE_KIND:
            return True
    return False


def _is_ambiguous_name(name: str) -> bool:
    """A one-word name is too common to verify from the university alone."""

    return len(_label(name).split()) < 2


def _shared(left: list[str], right: list[str]) -> bool:
    keys = {_label(item) for item in left if item and item.strip()}
    return any(_label(item) in keys for item in right if item and item.strip())


def _contains_phrase(text: str, phrase: str) -> bool:
    if not text.strip() or not phrase.strip():
        return False
    return _label(phrase) in _label(text)


_GENERIC_RESEARCH = {"computer science"}
_DASH_TRANSLATION = str.maketrans(
    {
        "\u2010": "",
        "\u2011": "",
        "\u2012": "",
        "\u2013": "",
        "\u2014": "",
        "\u2015": "",
        "\u2212": "",
        "\ufe63": "",
        "\uff0d": "",
        "-": "",
        "\u00ad": "",
        ",": " ",
        "，": " ",
        "、": " ",
        "·": " ",
        "•": " ",
        ".": " ",
    }
)


def fold_name_text(value: str) -> str:
    """Compare names after case, spacing, hyphen, and punctuation differences."""

    folded = value.casefold().translate(_DASH_TRANSLATION)
    return " ".join(folded.split())


def names_match(left: str | None, right: str | None) -> bool:
    """True when both strings are the same person-name after normalization.

    Two or three name parts may appear family-name first. A shared surname is not a match.
    """

    if not left or not right:
        return False
    left_text = fold_name_text(left)
    right_text = fold_name_text(right)
    if not left_text or not right_text:
        return False
    if left_text == right_text:
        return True
    left_tokens = left_text.split()
    right_tokens = right_text.split()
    if len(left_tokens) < 2 or len(left_tokens) != len(right_tokens):
        return False
    return left_tokens == list(reversed(right_tokens))


def page_mentions_name(text: str, name: str) -> bool:
    """True when the page text contains the candidate name, including reversed order."""

    if not text or not name:
        return False
    folded_page = fold_name_text(text)
    tokens = fold_name_text(name).split()
    if len(tokens) < 2:
        token = tokens[0] if tokens else ""
        return bool(token) and f" {token} " in f" {folded_page} "
    forward = " ".join(tokens)
    backward = " ".join(reversed(tokens))
    return forward in folded_page or backward in folded_page


def matched_display_name(text: str, name: str) -> str | None:
    """Return the name as written on the page when it matches the candidate."""

    if not name or not page_mentions_name(text, name):
        return None
    parts = [part for part in re.split(r"\s+", name.strip()) if part]
    if len(parts) < 2:
        return name.strip()

    def part_pattern(part: str) -> str:
        letters = [char for char in part if char.isalnum()]
        if not letters:
            return re.escape(part)
        return r"[\-‐‑‒–—―]*".join(re.escape(char) for char in letters)

    separator = r"[\s,，、·•.\-‐‑‒–—―]+"
    options = [
        separator.join(part_pattern(part) for part in parts),
        separator.join(part_pattern(part) for part in reversed(parts)),
    ]
    pattern = re.compile(
        r"(?<![A-Za-z0-9])(?:" + "|".join(options) + r")(?![A-Za-z0-9])",
        flags=re.IGNORECASE,
    )
    match = pattern.search(text)
    if match:
        return " ".join(match.group(0).split())
    return name.strip()


def _university_agrees(candidate: str | None, official: str | None) -> bool:
    if not candidate or not official:
        return False
    if _exact(candidate, official):
        return True
    return _contains_phrase(official, candidate)


def _university_on_page(university: str, page: OfficialPage) -> bool:
    if page.university and _university_agrees(university, page.university):
        return True
    return bool(page.affiliation) and _contains_phrase(page.affiliation, university)


def _specific_shared(left: list[str], right: list[str]) -> bool:
    keys = {
        _label(item)
        for item in left
        if item and item.strip() and _label(item) not in _GENERIC_RESEARCH
    }
    return any(
        _label(item) in keys
        for item in right
        if item and item.strip() and _label(item) not in _GENERIC_RESEARCH
    )


def _exact(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return _label(left) == _label(right)


def _label(value: str) -> str:
    return " ".join(value.casefold().split())


def _add_url(urls: list[str], url: str) -> list[str]:
    keys = {item.strip().rstrip("/").casefold() for item in urls}
    if url.strip().rstrip("/").casefold() in keys:
        return list(urls)
    return [*urls, url]


def _join(parts: list[str]) -> str:
    return " ".join(part.strip() for part in parts if part and part.strip())


def _timestamp(value: str | None) -> str:
    if value is None:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    if not isinstance(value, str) or "T" not in value:
        raise ValueError("verified_at must be an ISO timestamp.")
    return value

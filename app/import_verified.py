"""Import professor records that were already verified from official pages.

The input file is data/verified_import.json. A dry run only reports what would
change. data/professors.json is written only when --apply is used.

This module does not search the web, invent professors, or send email.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.config import get_data_dir, get_professors_path
from app.models import PaperRecord, ProfessorRecord
from app.sources import classify_public_url
from app.storage import (
    ProfessorStore,
    is_same_professor,
    make_professor_id,
    merge_professors,
    normalize_label,
    normalize_url,
)
from app.university_sources import OFFICIAL_SOURCE_KINDS, is_official_university_url
from app.verification import published_email

SENDS_EMAIL = False


def default_import_path() -> Path:
    return get_data_dir() / "verified_import.json"


def load_import_items(path: Path) -> list[object]:
    """Read the import file. It must be a JSON list. An empty list is valid."""

    if not path.exists():
        raise FileNotFoundError(f"Verified import file not found: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("The verified import file must contain a JSON list.")
    return raw


def validate_import_item(item: object, index: int) -> tuple[ProfessorRecord | None, list[str]]:
    """Check one import object. A missing email is allowed. A bad URL is not."""

    if not isinstance(item, dict):
        return None, [f"Record {index} must be a JSON object."]
    errors: list[str] = []
    name = _required_text(item.get("name"), "name", errors)
    university = _required_text(item.get("university"), "university", errors)
    profile_url = _required_text(item.get("official_profile_url"), "official_profile_url", errors)
    status = _required_text(item.get("verification_status"), "verification_status", errors)
    if status and status != "officially_verified":
        errors.append("verification_status must be officially_verified.")
    if profile_url and not is_official_university_url(profile_url):
        errors.append("official_profile_url is not an official .edu.cn or .ac.cn page.")
    department_url = _optional_text(item.get("official_department_url"))
    if department_url and not is_official_university_url(department_url):
        errors.append("official_department_url is not an official .edu.cn or .ac.cn page.")
    email, email_error = _supplied_email(item.get("public_email"))
    if email_error:
        errors.append(email_error)
    interests, interest_error = _string_list(item.get("research_interests", []), "research_interests")
    if interest_error:
        errors.append(interest_error)
    papers, paper_error = _papers(item.get("papers", []))
    if paper_error:
        errors.append(paper_error)
    field_sources, source_error = _field_sources(item.get("official_field_sources", {}))
    if source_error:
        errors.append(source_error)
    notes = item.get("verification_notes", "")
    if notes is None:
        notes = ""
    if not isinstance(notes, str):
        errors.append("verification_notes must be a string.")
        notes = ""
    official_source = _optional_text(item.get("official_source"))
    if official_source and official_source not in OFFICIAL_SOURCE_KINDS:
        errors.append("official_source is not an official university source kind.")
    if errors or not name or not university or not profile_url:
        return None, errors
    if profile_url:
        field_sources.setdefault("official_profile_url", profile_url)
    kind = official_source or classify_public_url(profile_url)
    record = ProfessorRecord(
        name=name,
        university=university,
        country=_optional_text(item.get("country")),
        city=_optional_text(item.get("city")),
        department=_optional_text(item.get("department")),
        public_email=email,
        official_email=email,
        professor_profile_url=profile_url,
        official_profile_url=profile_url,
        official_department_url=department_url,
        official_source=kind,
        official_university=university,
        official_department=_optional_text(item.get("department")),
        official_city=_optional_text(item.get("city")),
        research_interests=interests or [],
        papers=papers or [],
        source_urls=[url for url in (profile_url, department_url) if url],
        verification_status="officially_verified",
        verification_notes=notes.strip(),
        official_field_sources=field_sources or {},
    )
    return record, []


def run_import(
    *,
    import_path: Path | None = None,
    professors_path: Path | None = None,
    apply: bool = False,
) -> dict[str, object]:
    """Plan an import. Write the professor file only when apply is true."""

    if SENDS_EMAIL:
        raise RuntimeError("Verified import must not send email.")
    source = import_path or default_import_path()
    store = ProfessorStore(professors_path or get_professors_path())
    items = load_import_items(source)
    existing = store.load()
    working = [ProfessorRecord.from_dict(record.to_dict()) for record in existing]
    invalid_rows: list[dict[str, object]] = []
    would_add = 0
    would_update = 0
    for index, item in enumerate(items):
        record, errors = validate_import_item(item, index)
        if errors or record is None:
            invalid_rows.append({"index": index, "errors": errors})
            continue
        match_index = _find_match(working, record)
        if match_index is None:
            if not record.professor_id:
                record.professor_id = make_professor_id(record)
            working.append(record)
            would_add += 1
        else:
            working[match_index] = integrate_verified(working[match_index], record)
            would_update += 1
    if apply and (would_add or would_update):
        store.save(working)
    return {
        "found": len(items),
        "valid": would_add + would_update,
        "invalid": len(invalid_rows),
        "duplicates": would_update,
        "would_add": would_add,
        "would_update": would_update,
        "applied": bool(apply and (would_add or would_update)),
        "invalid_rows": invalid_rows,
    }


def integrate_verified(existing: ProfessorRecord, incoming: ProfessorRecord) -> ProfessorRecord:
    """Keep OpenAlex values when they conflict, and store the official value beside them."""

    prepared = ProfessorRecord.from_dict(incoming.to_dict())
    notes = [prepared.verification_notes] if prepared.verification_notes else []
    if _conflicts(existing.university, prepared.university):
        notes.append(
            f"OpenAlex university '{existing.university}' conflicts with official university "
            f"'{prepared.university}'. The official value is stored on official_university. "
            "The OpenAlex university was kept."
        )
        prepared.official_university = prepared.university
    if _conflicts(existing.department, prepared.department):
        notes.append(
            f"OpenAlex department '{existing.department}' conflicts with official department "
            f"'{prepared.department}'. The official value is stored on official_department. "
            "The OpenAlex department was kept."
        )
        prepared.official_department = prepared.department
        prepared.department = existing.department
    if _conflicts(existing.city, prepared.city):
        notes.append(
            f"OpenAlex city '{existing.city}' conflicts with official city '{prepared.city}'. "
            "The official value is stored on official_city. The OpenAlex city was kept."
        )
        prepared.official_city = prepared.city
        prepared.city = existing.city
    if _conflicts(existing.public_email, prepared.public_email):
        notes.append(
            f"OpenAlex email '{existing.public_email}' conflicts with the supplied official email. "
            "The OpenAlex email was kept."
        )
        prepared.official_email = prepared.public_email
        prepared.public_email = existing.public_email
    prepared.verification_notes = " ".join(note for note in notes if note).strip()
    merged = merge_professors(existing, prepared)
    for url in prepared.source_urls:
        if url and url not in merged.source_urls:
            merged.source_urls.append(url)
    for field_name, source_url in prepared.official_field_sources.items():
        merged.official_field_sources.setdefault(field_name, source_url)
    return merged


def main(argv: list[str] | None = None) -> int:
    """Report a dry run, or apply the import when --apply is present."""

    parser = argparse.ArgumentParser(
        description="Import officially verified professor records. The default is a dry run."
    )
    parser.add_argument("--dry-run", action="store_true", help="Report changes and do not write professors.json.")
    parser.add_argument("--apply", action="store_true", help="Write valid records into professors.json.")
    parser.add_argument("--input", type=Path, default=None, help="Verified import JSON file.")
    parser.add_argument("--professors", type=Path, default=None, help="Professor database to update.")
    args = parser.parse_args(argv)
    if args.apply and args.dry_run:
        print("Choose either --dry-run or --apply.")
        return 2
    try:
        result = run_import(
            import_path=args.input,
            professors_path=args.professors,
            apply=bool(args.apply),
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(exc)
        return 1
    _print_result(result, applied=bool(args.apply))
    return 0


def _print_result(result: dict[str, object], *, applied: bool) -> None:
    print(f"records found: {result['found']}")
    print(f"valid records: {result['valid']}")
    print(f"invalid records: {result['invalid']}")
    print(f"duplicates: {result['duplicates']}")
    print(f"records that would be added: {result['would_add']}")
    print(f"records that would be updated: {result['would_update']}")
    if applied and result["applied"]:
        print("professors.json updated.")
    else:
        print("professors.json was not modified.")
    invalid_rows = result["invalid_rows"]
    if isinstance(invalid_rows, list):
        for row in invalid_rows:
            if isinstance(row, dict):
                print(f"invalid record {row.get('index')}: {'; '.join(row.get('errors', []))}")


def _find_match(records: list[ProfessorRecord], incoming: ProfessorRecord) -> int | None:
    incoming_url = normalize_url(incoming.official_profile_url)
    for index, existing in enumerate(records):
        if is_same_professor(existing, incoming):
            return index
        if incoming_url and incoming_url in {
            normalize_url(existing.official_profile_url),
            normalize_url(existing.professor_profile_url),
        }:
            return index
    return None


def _conflicts(current: str | None, incoming: str | None) -> bool:
    if not current or not incoming:
        return False
    return normalize_label(current) != normalize_label(incoming)


def _required_text(value: object, label: str, errors: list[str]) -> str:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{label} is required.")
        return ""
    return value.strip()


def _optional_text(value: object) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _supplied_email(value: object) -> tuple[str | None, str | None]:
    """Keep an email only when the import file already contains one."""

    if value is None or value == "":
        return None, None
    if not isinstance(value, str):
        return None, "public_email must be a string or null."
    parsed = published_email(value)
    if parsed is None or parsed != value.strip():
        return None, "public_email must be the email published by the official source."
    return parsed, None


def _string_list(value: object, label: str) -> tuple[list[str] | None, str | None]:
    if value is None:
        return [], None
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        return None, f"{label} must be a list of strings."
    return [item.strip() for item in value], None


def _papers(value: object) -> tuple[list[PaperRecord] | None, str | None]:
    if value is None:
        return [], None
    if not isinstance(value, list):
        return None, "papers must be a list."
    papers: list[PaperRecord] = []
    for item in value:
        if not isinstance(item, dict):
            return None, "Each paper must be an object."
        try:
            papers.append(PaperRecord.from_dict(item))
        except ValueError as exc:
            return None, str(exc)
    return papers, None


def _field_sources(value: object) -> tuple[dict[str, str] | None, str | None]:
    if value is None:
        return {}, None
    if not isinstance(value, dict):
        return None, "official_field_sources must be an object."
    mapped: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str) or not item.strip():
            return None, "official_field_sources must map field names to URLs."
        url = item.strip()
        if not is_official_university_url(url):
            return None, f"official_field_sources URL is not an official university page: {url}"
        mapped[key.strip()] = url
    return mapped, None


if __name__ == "__main__":
    sys.exit(main())

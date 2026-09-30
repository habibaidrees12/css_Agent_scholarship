"""Load, check, and save the user profile JSON file.

Edit data/user_profile.json to change your information and filters.
Blank placeholder rows are kept so the field names stay visible, and this
module treats those blank rows as not filled in yet.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.config import get_profile_path, selection_errors
from app.models import (
    ActivityRecord,
    CertificationRecord,
    EducationRecord,
    InternshipRecord,
    ProjectRecord,
    UserProfile,
)

TOP_LEVEL_KEYS = {
    "schema_version",
    "notes",
    "personal",
    "education",
    "technical_skills",
    "projects",
    "internships",
    "certifications",
    "activities",
    "research_interests",
    "preferred_fields",
    "priority_fields",
    "preferred_countries",
    "preferred_cities",
    "preferred_universities",
    "department_filter",
    "field_filter",
    "scholarship_targets",
}

PERSONAL_KEYS = {"full_name", "email", "phone", "nationality"}
EDUCATION_KEYS = {
    "degree",
    "field_of_study",
    "university",
    "city",
    "country",
    "graduation_year",
    "gpa",
    "notes",
}
PROJECT_KEYS = {"title", "description", "technologies", "year", "url"}
INTERNSHIP_KEYS = {"organization", "role", "description", "start_year", "end_year"}
CERTIFICATION_KEYS = {"name", "issuer", "year"}
ACTIVITY_KEYS = {"name", "organization", "description", "year"}
INTEREST_KEYS = {"name", "priority", "keywords", "notes"}
SCHOLARSHIP_KEYS = {
    "id",
    "name",
    "active",
    "degree_level",
    "funding",
    "countries",
    "notes",
}


def load_profile(path: Path | None = None) -> UserProfile:
    """Read the profile JSON file and raise ValueError when the schema is invalid."""

    profile_path = path or get_profile_path()
    if not profile_path.exists():
        raise FileNotFoundError(
            f"Profile file not found: {profile_path}. Check the path or DATA_DIR in .env."
        )
    with profile_path.open(encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        raise ValueError("The profile file must contain a JSON object.")

    profile = UserProfile.from_dict(raw)
    errors, warnings = validate_profile(profile)
    warnings.extend(unknown_key_warnings(raw))
    if errors:
        details = "\n".join(f"- {item}" for item in errors)
        raise ValueError(f"Profile file has errors:\n{details}")
    profile.load_warnings = warnings
    return profile


def save_profile(profile: UserProfile, path: Path | None = None) -> Path:
    """Write the profile back to JSON. Blank placeholder rows are kept."""

    profile_path = path or get_profile_path()
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    with profile_path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(profile_to_dict(profile), handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    return profile_path


def validate_profile(profile: UserProfile) -> tuple[list[str], list[str]]:
    """Return (errors, warnings). Errors mean the filters cannot be trusted."""

    errors: list[str] = []
    warnings: list[str] = []
    errors.extend(
        selection_errors(
            "preferred_countries",
            profile.preferred_countries.mode,
            profile.preferred_countries.countries,
        )
    )
    errors.extend(
        selection_errors(
            "preferred_cities",
            profile.preferred_cities.mode,
            profile.preferred_cities.cities,
        )
    )
    errors.extend(
        selection_errors(
            "preferred_universities",
            profile.preferred_universities.mode,
            profile.preferred_universities.universities,
        )
    )
    errors.extend(
        selection_errors(
            "department_filter",
            profile.department_filter.mode,
            profile.department_filter.departments,
        )
    )
    errors.extend(
        selection_errors(
            "field_filter",
            profile.field_filter.mode,
            profile.field_filter.fields,
        )
    )

    if not profile.scholarship_targets:
        warnings.append("No scholarship targets are listed.")
    elif not any(item.active for item in profile.scholarship_targets):
        warnings.append("No scholarship target is active.")

    known_areas = {_key(item) for item in profile.preferred_fields}
    known_areas.update(_key(item.name) for item in profile.research_interests)
    for name in profile.priority_fields:
        if _key(name) not in known_areas:
            warnings.append(
                f"Priority field '{name}' is not in preferred_fields or research_interests."
            )
    return errors, warnings


def unknown_key_warnings(raw: dict[str, Any]) -> list[str]:
    """Point out JSON keys the program will ignore, including likely typos."""

    warnings = _unknown(raw, TOP_LEVEL_KEYS, "profile")
    personal = raw.get("personal")
    if isinstance(personal, dict):
        warnings.extend(_unknown(personal, PERSONAL_KEYS, "personal"))
    warnings.extend(_unknown_rows(raw.get("education"), EDUCATION_KEYS, "education"))
    warnings.extend(_unknown_rows(raw.get("projects"), PROJECT_KEYS, "projects"))
    warnings.extend(_unknown_rows(raw.get("internships"), INTERNSHIP_KEYS, "internships"))
    warnings.extend(
        _unknown_rows(raw.get("certifications"), CERTIFICATION_KEYS, "certifications")
    )
    warnings.extend(_unknown_rows(raw.get("activities"), ACTIVITY_KEYS, "activities"))
    warnings.extend(
        _unknown_rows(raw.get("research_interests"), INTEREST_KEYS, "research_interests")
    )
    warnings.extend(
        _unknown_rows(raw.get("scholarship_targets"), SCHOLARSHIP_KEYS, "scholarship_targets")
    )
    warnings.extend(
        _unknown_filter(raw.get("preferred_countries"), "preferred_countries", "countries")
    )
    warnings.extend(_unknown_filter(raw.get("preferred_cities"), "preferred_cities", "cities"))
    warnings.extend(
        _unknown_filter(
            raw.get("preferred_universities"), "preferred_universities", "universities"
        )
    )
    warnings.extend(
        _unknown_filter(raw.get("department_filter"), "department_filter", "departments")
    )
    warnings.extend(_unknown_filter(raw.get("field_filter"), "field_filter", "fields"))
    return warnings


def is_blank_education(record: EducationRecord) -> bool:
    return not any(
        (
            record.degree,
            record.field_of_study,
            record.university,
            record.city,
            record.country,
            record.notes,
            record.graduation_year is not None,
            record.gpa is not None,
        )
    )


def is_blank_project(record: ProjectRecord) -> bool:
    return not any(
        (record.title, record.description, record.technologies, record.url, record.year is not None)
    )


def is_blank_internship(record: InternshipRecord) -> bool:
    return not any(
        (
            record.organization,
            record.role,
            record.description,
            record.start_year is not None,
            record.end_year is not None,
        )
    )


def is_blank_certification(record: CertificationRecord) -> bool:
    return not any((record.name, record.issuer, record.year is not None))


def filled_education(profile: UserProfile) -> list[EducationRecord]:
    return [item for item in profile.education if not is_blank_education(item)]


def filled_projects(profile: UserProfile) -> list[ProjectRecord]:
    return [item for item in profile.projects if not is_blank_project(item)]


def filled_internships(profile: UserProfile) -> list[InternshipRecord]:
    return [item for item in profile.internships if not is_blank_internship(item)]


def filled_certifications(profile: UserProfile) -> list[CertificationRecord]:
    return [item for item in profile.certifications if not is_blank_certification(item)]


def is_blank_activity(record: ActivityRecord) -> bool:
    return not any(
        (record.name, record.organization, record.description, record.year is not None)
    )


def filled_activities(profile: UserProfile) -> list[ActivityRecord]:
    return [item for item in profile.activities if not is_blank_activity(item)]


def profile_to_dict(profile: UserProfile) -> dict[str, Any]:
    """Convert a profile to the JSON schema. Loader-only warnings are omitted."""

    return {
        "schema_version": profile.schema_version,
        "notes": profile.notes,
        "personal": {
            "full_name": profile.personal.full_name,
            "email": profile.personal.email,
            "phone": profile.personal.phone,
            "nationality": profile.personal.nationality,
        },
        "education": [
            {
                "degree": item.degree,
                "field_of_study": item.field_of_study,
                "university": item.university,
                "city": item.city,
                "country": item.country,
                "graduation_year": item.graduation_year,
                "gpa": item.gpa,
                "notes": item.notes,
            }
            for item in profile.education
        ],
        "technical_skills": list(profile.technical_skills),
        "projects": [
            {
                "title": item.title,
                "description": item.description,
                "technologies": list(item.technologies),
                "year": item.year,
                "url": item.url,
            }
            for item in profile.projects
        ],
        "internships": [
            {
                "organization": item.organization,
                "role": item.role,
                "description": item.description,
                "start_year": item.start_year,
                "end_year": item.end_year,
            }
            for item in profile.internships
        ],
        "certifications": [
            {"name": item.name, "issuer": item.issuer, "year": item.year}
            for item in profile.certifications
        ],
        "activities": [
            {
                "name": item.name,
                "organization": item.organization,
                "description": item.description,
                "year": item.year,
            }
            for item in profile.activities
        ],
        "research_interests": [
            {
                "name": item.name,
                "priority": item.priority,
                "keywords": list(item.keywords),
                "notes": item.notes,
            }
            for item in profile.research_interests
        ],
        "preferred_fields": list(profile.preferred_fields),
        "priority_fields": list(profile.priority_fields),
        "preferred_countries": {
            "mode": profile.preferred_countries.mode,
            "countries": list(profile.preferred_countries.countries),
        },
        "preferred_cities": {
            "mode": profile.preferred_cities.mode,
            "cities": list(profile.preferred_cities.cities),
        },
        "preferred_universities": {
            "mode": profile.preferred_universities.mode,
            "universities": list(profile.preferred_universities.universities),
        },
        "department_filter": {
            "mode": profile.department_filter.mode,
            "departments": list(profile.department_filter.departments),
        },
        "field_filter": {
            "mode": profile.field_filter.mode,
            "fields": list(profile.field_filter.fields),
        },
        "scholarship_targets": [
            {
                "id": item.id,
                "name": item.name,
                "active": item.active,
                "degree_level": item.degree_level,
                "funding": item.funding,
                "countries": list(item.countries),
                "notes": item.notes,
            }
            for item in profile.scholarship_targets
        ],
    }


def _unknown(data: dict[str, Any], allowed: set[str], label: str) -> list[str]:
    return [
        f"{label} has an unknown field '{key}', and the program will ignore it."
        for key in sorted(set(data) - allowed)
    ]


def _unknown_rows(value: object, allowed: set[str], label: str) -> list[str]:
    if not isinstance(value, list):
        return []
    warnings: list[str] = []
    for index, item in enumerate(value, start=1):
        if isinstance(item, dict):
            warnings.extend(_unknown(item, allowed, f"{label} item {index}"))
    return warnings


def _unknown_filter(value: object, label: str, values_key: str) -> list[str]:
    if not isinstance(value, dict):
        return []
    return _unknown(value, {"mode", values_key}, label)


def _key(value: str) -> str:
    return " ".join(value.casefold().split())

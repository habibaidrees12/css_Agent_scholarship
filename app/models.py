"""Data shapes for the profile, professors, and match results.

These classes describe the information the program expects. They do not
contain a professor directory. A later data source can build Professor
objects and pass them to the matching module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _as_text(value: object, label: str) -> str:
    """Return a stripped string, or an empty string when the value is null."""

    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string.")
    return value.strip()


def _as_optional_year(value: object, label: str) -> int | None:
    """Return a calendar year, or None when the field is blank."""

    if value is None or value == "":
        return None
    # bool is a subclass of int, and it is not a valid year.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be a whole year, such as 2024, or null.")
    if value < 1900 or value > 2100:
        raise ValueError(f"{label} must be between 1900 and 2100.")
    return value


def _as_optional_gpa(value: object, label: str) -> str | None:
    """Keep GPA as text so scales such as 3.7/4.0 or 85/100 can be stored."""

    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{label} must be a number, a string, or null.")
    return str(value).strip()


def _as_string_list(value: object, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{label} must be a list of strings.")
    return [item.strip() for item in value if item.strip()]


# How far official-university verification got. This is separate from research matching.
VERIFICATION_STATUSES = (
    "unverified",
    "partially_verified",
    "officially_verified",
    "verification_failed",
)


def _as_verification_status(value: object) -> str:
    if value is None or value == "":
        return "unverified"
    if not isinstance(value, str):
        raise ValueError(
            "verification_status must be unverified, partially_verified, officially_verified, or verification_failed."
        )
    cleaned = value.strip().casefold()
    if cleaned not in VERIFICATION_STATUSES:
        raise ValueError(
            "verification_status must be unverified, partially_verified, officially_verified, or verification_failed."
        )
    return cleaned


def _as_string_map(value: object, label: str) -> dict[str, str]:
    """Map a verified field name to the URL that published it."""

    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object of strings.")
    mapped: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise ValueError(f"{label} must be an object of strings.")
        cleaned_key = key.strip()
        cleaned_value = item.strip()
        if cleaned_key and cleaned_value:
            mapped[cleaned_key] = cleaned_value
    return mapped


def _as_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be true or false.")
    return value


def _as_priority(value: object, label: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be high, medium, low, or null.")
    cleaned = value.strip().casefold()
    if cleaned not in {"high", "medium", "low"}:
        raise ValueError(f"{label} must be high, medium, low, or null.")
    return cleaned


def _as_mode(value: object, label: str) -> str:
    if value is None or value == "":
        return "any"
    if not isinstance(value, str):
        raise ValueError(f"{label} must be any, one, or multiple.")
    return value.strip().casefold()


@dataclass
class PersonalInfo:
    """Contact details for a later email draft. Leave them blank for now."""

    full_name: str = ""
    email: str = ""
    phone: str = ""
    nationality: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PersonalInfo:
        return cls(
            full_name=_as_text(data.get("full_name"), "personal.full_name"),
            email=_as_text(data.get("email"), "personal.email"),
            phone=_as_text(data.get("phone"), "personal.phone"),
            nationality=_as_text(data.get("nationality"), "personal.nationality"),
        )


@dataclass
class EducationRecord:
    """One degree. degree, university, and graduation_year live on this row."""

    degree: str = ""
    field_of_study: str = ""
    university: str = ""
    city: str = ""
    country: str = ""
    graduation_year: int | None = None
    gpa: str | None = None
    notes: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EducationRecord:
        return cls(
            degree=_as_text(data.get("degree"), "education.degree"),
            field_of_study=_as_text(data.get("field_of_study"), "education.field_of_study"),
            university=_as_text(data.get("university"), "education.university"),
            city=_as_text(data.get("city"), "education.city"),
            country=_as_text(data.get("country"), "education.country"),
            graduation_year=_as_optional_year(
                data.get("graduation_year"), "education.graduation_year"
            ),
            gpa=_as_optional_gpa(data.get("gpa"), "education.gpa"),
            notes=_as_text(data.get("notes"), "education.notes"),
        )


@dataclass
class ProjectRecord:
    title: str = ""
    description: str = ""
    technologies: list[str] = field(default_factory=list)
    year: int | None = None
    url: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProjectRecord:
        return cls(
            title=_as_text(data.get("title"), "projects.title"),
            description=_as_text(data.get("description"), "projects.description"),
            technologies=_as_string_list(data.get("technologies", []), "projects.technologies"),
            year=_as_optional_year(data.get("year"), "projects.year"),
            url=_as_text(data.get("url"), "projects.url"),
        )


@dataclass
class InternshipRecord:
    organization: str = ""
    role: str = ""
    description: str = ""
    start_year: int | None = None
    end_year: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InternshipRecord:
        return cls(
            organization=_as_text(data.get("organization"), "internships.organization"),
            role=_as_text(data.get("role"), "internships.role"),
            description=_as_text(data.get("description"), "internships.description"),
            start_year=_as_optional_year(data.get("start_year"), "internships.start_year"),
            end_year=_as_optional_year(data.get("end_year"), "internships.end_year"),
        )


@dataclass
class CertificationRecord:
    name: str = ""
    issuer: str = ""
    year: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CertificationRecord:
        return cls(
            name=_as_text(data.get("name"), "certifications.name"),
            issuer=_as_text(data.get("issuer"), "certifications.issuer"),
            year=_as_optional_year(data.get("year"), "certifications.year"),
        )


@dataclass
class ActivityRecord:
    """Volunteering or student activity. This is separate from certifications."""

    name: str = ""
    organization: str = ""
    description: str = ""
    year: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ActivityRecord:
        return cls(
            name=_as_text(data.get("name"), "activities.name"),
            organization=_as_text(data.get("organization"), "activities.organization"),
            description=_as_text(data.get("description"), "activities.description"),
            year=_as_optional_year(data.get("year"), "activities.year"),
        )


@dataclass
class ResearchInterest:
    """A research area and an optional priority of high, medium, or low."""

    name: str
    priority: str | None = None
    keywords: list[str] = field(default_factory=list)
    notes: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ResearchInterest:
        name = _as_text(data.get("name"), "research_interests.name")
        if not name:
            raise ValueError("Each research interest needs a name.")
        return cls(
            name=name,
            priority=_as_priority(data.get("priority"), "research_interests.priority"),
            keywords=_as_string_list(data.get("keywords", []), "research_interests.keywords"),
            notes=_as_text(data.get("notes"), "research_interests.notes"),
        )


@dataclass
class CountryFilter:
    mode: str = "any"
    countries: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CountryFilter:
        return cls(
            mode=_as_mode(data.get("mode", "any"), "preferred_countries.mode"),
            countries=_as_string_list(
                data.get("countries", []), "preferred_countries.countries"
            ),
        )


@dataclass
class CityFilter:
    """City selection: any city, one city, or multiple cities."""

    mode: str = "any"
    cities: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CityFilter:
        return cls(
            mode=_as_mode(data.get("mode", "any"), "preferred_cities.mode"),
            cities=_as_string_list(data.get("cities", []), "preferred_cities.cities"),
        )


@dataclass
class UniversityFilter:
    mode: str = "any"
    universities: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UniversityFilter:
        return cls(
            mode=_as_mode(data.get("mode", "any"), "preferred_universities.mode"),
            universities=_as_string_list(
                data.get("universities", []), "preferred_universities.universities"
            ),
        )


@dataclass
class DepartmentFilter:
    mode: str = "any"
    departments: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DepartmentFilter:
        return cls(
            mode=_as_mode(data.get("mode", "any"), "department_filter.mode"),
            departments=_as_string_list(
                data.get("departments", []), "department_filter.departments"
            ),
        )


@dataclass
class FieldFilter:
    """Optional hard limit on fields. preferred_fields is separate and is used for scoring."""

    mode: str = "any"
    fields: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FieldFilter:
        return cls(
            mode=_as_mode(data.get("mode", "any"), "field_filter.mode"),
            fields=_as_string_list(data.get("fields", []), "field_filter.fields"),
        )


@dataclass
class ScholarshipTarget:
    """One scholarship program. active false means it is reserved for later."""

    id: str
    name: str
    active: bool = False
    degree_level: str = "master"
    funding: str = "fully_funded"
    countries: list[str] = field(default_factory=list)
    notes: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScholarshipTarget:
        scholarship_id = _as_text(data.get("id"), "scholarship_targets.id")
        name = _as_text(data.get("name"), "scholarship_targets.name")
        if not scholarship_id or not name:
            raise ValueError("Each scholarship target needs an id and a name.")
        if "active" not in data:
            raise ValueError("Each scholarship target needs an active value of true or false.")
        return cls(
            id=scholarship_id,
            name=name,
            active=_as_bool(data.get("active"), "scholarship_targets.active"),
            degree_level=_as_text(data.get("degree_level"), "scholarship_targets.degree_level"),
            funding=_as_text(data.get("funding"), "scholarship_targets.funding"),
            countries=_as_string_list(data.get("countries", []), "scholarship_targets.countries"),
            notes=_as_text(data.get("notes"), "scholarship_targets.notes"),
        )


@dataclass
class UserProfile:
    """The configurable applicant profile loaded from data/user_profile.json."""

    schema_version: int = 1
    notes: str = ""
    personal: PersonalInfo = field(default_factory=PersonalInfo)
    education: list[EducationRecord] = field(default_factory=list)
    technical_skills: list[str] = field(default_factory=list)
    projects: list[ProjectRecord] = field(default_factory=list)
    internships: list[InternshipRecord] = field(default_factory=list)
    certifications: list[CertificationRecord] = field(default_factory=list)
    activities: list[ActivityRecord] = field(default_factory=list)
    research_interests: list[ResearchInterest] = field(default_factory=list)
    preferred_fields: list[str] = field(default_factory=list)
    priority_fields: list[str] = field(default_factory=list)
    preferred_countries: CountryFilter = field(default_factory=CountryFilter)
    preferred_cities: CityFilter = field(default_factory=CityFilter)
    preferred_universities: UniversityFilter = field(default_factory=UniversityFilter)
    department_filter: DepartmentFilter = field(default_factory=DepartmentFilter)
    field_filter: FieldFilter = field(default_factory=FieldFilter)
    scholarship_targets: list[ScholarshipTarget] = field(default_factory=list)
    # Filled by the loader. This is not part of the JSON file.
    load_warnings: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UserProfile:
        if not isinstance(data, dict):
            raise ValueError("The profile file must contain a JSON object.")
        _require_keys(data)
        if data.get("schema_version") != 1:
            raise ValueError("This program reads schema_version 1.")
        personal = data.get("personal")
        if not isinstance(personal, dict):
            raise ValueError("personal must be an object.")
        return cls(
            schema_version=1,
            notes=_as_text(data.get("notes", ""), "notes"),
            personal=PersonalInfo.from_dict(personal),
            education=_map_objects(data.get("education"), EducationRecord, "education"),
            technical_skills=_as_string_list(data.get("technical_skills"), "technical_skills"),
            projects=_map_objects(data.get("projects"), ProjectRecord, "projects"),
            internships=_map_objects(data.get("internships"), InternshipRecord, "internships"),
            certifications=_map_objects(
                data.get("certifications"), CertificationRecord, "certifications"
            ),
            activities=_map_objects(data.get("activities", []), ActivityRecord, "activities"),
            research_interests=_map_objects(
                data.get("research_interests"), ResearchInterest, "research_interests"
            ),
            preferred_fields=_as_string_list(data.get("preferred_fields"), "preferred_fields"),
            priority_fields=_as_string_list(data.get("priority_fields"), "priority_fields"),
            preferred_countries=_object_filter(
                data.get("preferred_countries"), CountryFilter, "preferred_countries"
            ),
            preferred_cities=_object_filter(
                data.get("preferred_cities"), CityFilter, "preferred_cities"
            ),
            preferred_universities=_object_filter(
                data.get("preferred_universities"), UniversityFilter, "preferred_universities"
            ),
            department_filter=_object_filter(
                data.get("department_filter"), DepartmentFilter, "department_filter"
            ),
            field_filter=_object_filter(data.get("field_filter"), FieldFilter, "field_filter"),
            scholarship_targets=_map_objects(
                data.get("scholarship_targets"), ScholarshipTarget, "scholarship_targets"
            ),
        )


def _require_keys(data: dict[str, Any]) -> None:
    required = (
        "schema_version",
        "personal",
        "education",
        "technical_skills",
        "projects",
        "internships",
        "certifications",
        "research_interests",
        "preferred_fields",
        "priority_fields",
        "preferred_countries",
        "preferred_cities",
        "preferred_universities",
        "department_filter",
        "field_filter",
        "scholarship_targets",
    )
    missing = [key for key in required if key not in data]
    if missing:
        joined = ", ".join(missing)
        raise ValueError(f"The profile file is missing: {joined}.")


def _map_objects(value: object, record_type: type, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list.")
    records = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"{label} item {index} must be an object.")
        records.append(record_type.from_dict(item))
    return records


def _object_filter(value: object, filter_type: type, label: str) -> Any:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object with a mode and a list of values.")
    return filter_type.from_dict(value)


@dataclass
class ResearchPaper:
    """A paper that is already in hand. This project does not download papers."""

    title: str = ""
    year: int | None = None
    abstract: str = ""
    keywords: list[str] = field(default_factory=list)
    url: str = ""
    source_url: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ResearchPaper:
        if not isinstance(data, dict):
            raise ValueError("Each paper must be an object.")
        return cls(
            title=_as_text(data.get("title"), "papers.title"),
            year=_as_optional_year(data.get("year"), "papers.year"),
            abstract=_as_text(data.get("abstract"), "papers.abstract"),
            keywords=_as_string_list(data.get("keywords", []), "papers.keywords"),
            url=_as_text(data.get("url"), "papers.url"),
            source_url=_as_text(data.get("source_url", ""), "papers.source_url"),
        )


@dataclass
class ProfessorContact:
    """Public contact fields. Sending email is not implemented."""

    email: str = ""
    phone: str = ""
    website: str = ""
    profile_url: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProfessorContact:
        return cls(
            email=_as_text(data.get("email"), "contact.email"),
            phone=_as_text(data.get("phone"), "contact.phone"),
            website=_as_text(data.get("website"), "contact.website"),
            profile_url=_as_text(data.get("profile_url"), "contact.profile_url"),
        )


@dataclass
class Professor:
    """One professor record supplied by a future data source."""

    name: str = ""
    university: str = ""
    department: str = ""
    city: str = ""
    country: str = ""
    research_areas: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    papers: list[ResearchPaper] = field(default_factory=list)
    contact: ProfessorContact = field(default_factory=ProfessorContact)
    source_urls: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Professor:
        if not isinstance(data, dict):
            raise ValueError("A professor record must be an object.")
        papers = data.get("papers", [])
        if not isinstance(papers, list):
            raise ValueError("professor.papers must be a list.")
        contact = data.get("contact", {})
        if contact is None:
            contact = {}
        if not isinstance(contact, dict):
            raise ValueError("professor.contact must be an object.")
        return cls(
            name=_as_text(data.get("name"), "professor.name"),
            university=_as_text(data.get("university"), "professor.university"),
            department=_as_text(data.get("department"), "professor.department"),
            city=_as_text(data.get("city"), "professor.city"),
            country=_as_text(data.get("country"), "professor.country"),
            research_areas=_as_string_list(
                data.get("research_areas", []), "professor.research_areas"
            ),
            keywords=_as_string_list(data.get("keywords", []), "professor.keywords"),
            papers=[ResearchPaper.from_dict(item) for item in papers],
            contact=ProfessorContact.from_dict(contact),
            source_urls=_as_string_list(data.get("source_urls", []), "professor.source_urls"),
        )


@dataclass
class MatchResult:
    """The outcome of comparing one profile with one professor."""

    professor_name: str
    university: str
    passed_filters: bool
    filter_notes: list[str]
    research_score: int
    matched_interests: list[str]
    matched_fields: list[str]
    paper_notes: list[str]
    summary: str
    matched_areas: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    research_confidence: str = "low"


def _as_optional_text(value: object, label: str) -> str | None:
    """Return None for blank values so missing facts stay missing."""

    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string or null.")
    cleaned = value.strip()
    return cleaned or None


@dataclass
class PaperRecord:
    """One paper taken from a source. A missing abstract stays null."""

    title: str = ""
    year: int | None = None
    abstract: str | None = None
    keywords: list[str] = field(default_factory=list)
    paper_url: str | None = None
    source_url: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PaperRecord:
        if not isinstance(data, dict):
            raise ValueError("Each paper must be an object.")
        return cls(
            title=_as_text(data.get("title"), "papers.title"),
            year=_as_optional_year(data.get("year"), "papers.year"),
            abstract=_as_optional_text(data.get("abstract"), "papers.abstract"),
            keywords=_as_string_list(data.get("keywords", []), "papers.keywords"),
            paper_url=_as_optional_text(data.get("paper_url"), "papers.paper_url"),
            source_url=_as_optional_text(data.get("source_url"), "papers.source_url"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "year": self.year,
            "abstract": self.abstract,
            "keywords": list(self.keywords),
            "paper_url": self.paper_url,
            "source_url": self.source_url,
        }


@dataclass
class ProfessorRecord:
    """A discovered professor. Unknown email, city, and abstract stay null."""

    professor_id: str = ""
    name: str = ""
    title: str | None = None
    university: str = ""
    department: str | None = None
    country: str | None = None
    city: str | None = None
    university_website: str | None = None
    professor_profile_url: str | None = None
    public_email: str | None = None
    research_interests: list[str] = field(default_factory=list)
    papers: list[PaperRecord] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)
    last_verified: str | None = None
    verified: bool = False
    source_kind: str | None = None
    verification_status: str = "unverified"
    verification_notes: str = ""
    verified_at: str | None = None
    official_profile_url: str | None = None
    official_department_url: str | None = None
    official_source: str | None = None
    official_email: str | None = None
    official_department: str | None = None
    official_city: str | None = None
    official_university: str | None = None
    official_field_sources: dict[str, str] = field(default_factory=dict)
    match_result: dict[str, Any] | None = None
    notes: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProfessorRecord:
        if not isinstance(data, dict):
            raise ValueError("A professor record must be an object.")
        papers = data.get("papers", [])
        if not isinstance(papers, list):
            raise ValueError("papers must be a list.")
        match_result = data.get("match_result")
        if match_result is not None and not isinstance(match_result, dict):
            raise ValueError("match_result must be an object or null.")
        verified = data.get("verified", False)
        if not isinstance(verified, bool):
            raise ValueError("verified must be true or false.")
        return cls(
            professor_id=_as_text(data.get("professor_id", ""), "professor_id"),
            name=_as_text(data.get("name"), "name"),
            title=_as_optional_text(data.get("title"), "title"),
            university=_as_text(data.get("university"), "university"),
            department=_as_optional_text(data.get("department"), "department"),
            country=_as_optional_text(data.get("country"), "country"),
            city=_as_optional_text(data.get("city"), "city"),
            university_website=_as_optional_text(data.get("university_website"), "university_website"),
            professor_profile_url=_as_optional_text(
                data.get("professor_profile_url"), "professor_profile_url"
            ),
            public_email=_as_optional_text(data.get("public_email"), "public_email"),
            research_interests=_as_string_list(
                data.get("research_interests", []), "research_interests"
            ),
            papers=[PaperRecord.from_dict(item) for item in papers],
            source_urls=_as_string_list(data.get("source_urls", []), "source_urls"),
            last_verified=_as_optional_text(data.get("last_verified"), "last_verified"),
            verified=verified,
            source_kind=_as_optional_text(data.get("source_kind"), "source_kind"),
            verification_status=_as_verification_status(data.get("verification_status", "unverified")),
            verification_notes=_as_text(data.get("verification_notes", ""), "verification_notes"),
            verified_at=_as_optional_text(data.get("verified_at"), "verified_at"),
            official_profile_url=_as_optional_text(
                data.get("official_profile_url"), "official_profile_url"
            ),
            official_department_url=_as_optional_text(
                data.get("official_department_url"), "official_department_url"
            ),
            official_source=_as_optional_text(data.get("official_source"), "official_source"),
            official_email=_as_optional_text(data.get("official_email"), "official_email"),
            official_department=_as_optional_text(
                data.get("official_department"), "official_department"
            ),
            official_city=_as_optional_text(data.get("official_city"), "official_city"),
            official_university=_as_optional_text(
                data.get("official_university"), "official_university"
            ),
            official_field_sources=_as_string_map(
                data.get("official_field_sources", {}), "official_field_sources"
            ),
            match_result=match_result,
            notes=_as_text(data.get("notes", ""), "notes"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "professor_id": self.professor_id,
            "name": self.name,
            "title": self.title,
            "university": self.university,
            "department": self.department,
            "country": self.country,
            "city": self.city,
            "university_website": self.university_website,
            "professor_profile_url": self.professor_profile_url,
            "public_email": self.public_email,
            "research_interests": list(self.research_interests),
            "papers": [paper.to_dict() for paper in self.papers],
            "source_urls": list(self.source_urls),
            "last_verified": self.last_verified,
            "verified": self.verified,
            "source_kind": self.source_kind,
            "verification_status": self.verification_status,
            "verification_notes": self.verification_notes,
            "verified_at": self.verified_at,
            "official_profile_url": self.official_profile_url,
            "official_department_url": self.official_department_url,
            "official_source": self.official_source,
            "official_email": self.official_email,
            "official_department": self.official_department,
            "official_city": self.official_city,
            "official_university": self.official_university,
            "official_field_sources": dict(self.official_field_sources),
            "match_result": self.match_result,
            "notes": self.notes,
        }

    def to_match_professor(self) -> Professor:
        """Convert this record into the shape match_professor already accepts.

        Null city, country, and email become empty strings only for that
        comparison. The stored record keeps null.
        """

        papers = [
            ResearchPaper(
                title=paper.title,
                year=paper.year,
                abstract=paper.abstract or "",
                keywords=list(paper.keywords),
                url=paper.paper_url or "",
                source_url=paper.source_url or "",
            )
            for paper in self.papers
        ]
        return Professor(
            name=self.name,
            university=self.university,
            department=self.department or "",
            city=self.city or "",
            country=self.country or "",
            research_areas=list(self.research_interests),
            papers=papers,
            source_urls=list(self.source_urls),
            contact=ProfessorContact(
                email=self.public_email or "",
                website=self.university_website or "",
                profile_url=self.professor_profile_url or "",
            ),
        )

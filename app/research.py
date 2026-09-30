"""Search settings and professor scoring for discovery.

Country, city, and university limits come from data/search_config.json.
Research overlap, including recent papers, still goes through match_professor.
This module does not send email and does not create professor names.
"""

from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.config import BROAD_ACADEMIC_FIELDS, get_search_config_path, selection_errors
from app.matching import CONFIDENCE_RANK, match_professor
from app.models import (
    CityFilter,
    CountryFilter,
    MatchResult,
    ProfessorRecord,
    ResearchInterest,
    UniversityFilter,
    UserProfile,
)
from app.sources import AcademicSource, SearchQuery


@dataclass
class ResearchAreaFilter:
    """Any broad CS/IT field, one field, or several fields."""

    mode: str = "any"
    areas: list[str] = field(default_factory=list)


@dataclass
class SearchConfig:
    """Limits for a discovery run. The starter file searches China broadly."""

    country: CountryFilter = field(default_factory=lambda: CountryFilter(mode="one", countries=["China"]))
    cities: CityFilter = field(default_factory=CityFilter)
    universities: UniversityFilter = field(default_factory=UniversityFilter)
    research_areas: ResearchAreaFilter = field(
        default_factory=lambda: ResearchAreaFilter(mode="any", areas=[])
    )
    minimum_research_confidence: str = "medium"
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "notes": self.notes,
            "country": {"mode": self.country.mode, "countries": list(self.country.countries)},
            "cities": {"mode": self.cities.mode, "cities": list(self.cities.cities)},
            "universities": {
                "mode": self.universities.mode,
                "universities": list(self.universities.universities),
            },
            "research_areas": {
                "mode": self.research_areas.mode,
                "areas": list(self.research_areas.areas),
            },
            "minimum_research_confidence": self.minimum_research_confidence,
        }


def load_search_config(path: Path | None = None) -> SearchConfig:
    """Read search_config.json and reject a city or area mode that cannot be applied."""

    config_path = path or get_search_config_path()
    if not config_path.exists():
        raise FileNotFoundError(f"Search config not found: {config_path}")
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("The search config must be a JSON object.")
    config = SearchConfig(
        country=_country_filter(raw.get("country")),
        cities=_city_filter(raw.get("cities")),
        universities=_university_filter(raw.get("universities")),
        research_areas=_area_filter(raw.get("research_areas")),
        minimum_research_confidence=_confidence_setting(raw.get("minimum_research_confidence", "medium")),
        notes=raw.get("notes") if isinstance(raw.get("notes"), str) else "",
    )
    errors = config_errors(config)
    if errors:
        details = "\n".join(f"- {item}" for item in errors)
        raise ValueError(f"Search config has errors:\n{details}")
    return config


def config_errors(config: SearchConfig) -> list[str]:
    errors = []
    errors.extend(selection_errors("country", config.country.mode, config.country.countries))
    errors.extend(selection_errors("cities", config.cities.mode, config.cities.cities))
    errors.extend(
        selection_errors("universities", config.universities.mode, config.universities.universities)
    )
    errors.extend(
        selection_errors("research_areas", config.research_areas.mode, config.research_areas.areas)
    )
    return errors


def resolve_areas(config: SearchConfig) -> list[str]:
    """Return the areas to search. Mode any means the broad CS/IT list."""

    if config.research_areas.mode == "any":
        return list(BROAD_ACADEMIC_FIELDS)
    return list(config.research_areas.areas)


def build_queries(config: SearchConfig) -> list[SearchQuery]:
    """Build one query per country and research area. Do not reduce this to one department."""

    if config.country.mode == "any":
        countries: list[str | None] = [None]
    else:
        countries = list(config.country.countries)
    queries = []
    for country in countries:
        for area in resolve_areas(config):
            queries.append(
                SearchQuery(
                    research_area=area,
                    country=country,
                    city_mode=config.cities.mode,
                    cities=list(config.cities.cities),
                    university_mode=config.universities.mode,
                    universities=list(config.universities.universities),
                )
            )
    return queries


def evaluate_professor(
    profile: UserProfile, config: SearchConfig, record: ProfessorRecord
) -> MatchResult:
    """Apply search filters, then score the professor with match_professor.

    The department does not have to be exactly Software Engineering. A match
    can come from the department text, research interests, or papers.
    """

    scoped = copy.deepcopy(profile)
    scoped.preferred_countries = CountryFilter(
        mode=config.country.mode, countries=list(config.country.countries)
    )
    scoped.preferred_cities = CityFilter(mode=config.cities.mode, cities=list(config.cities.cities))
    scoped.preferred_universities = UniversityFilter(
        mode=config.universities.mode, universities=list(config.universities.universities)
    )
    result = match_professor(scoped, record.to_match_professor())
    area_ok = overlaps_selected_areas(record, resolve_areas(config))
    confidence_ok = _confidence_meets(
        result.research_confidence, config.minimum_research_confidence
    )
    notes = list(result.filter_notes)
    if not area_ok:
        notes.append(
            "Research area filter: none of the selected areas had specific evidence in the department, interests, or papers."
        )
    if not confidence_ok:
        notes.append(
            "Research confidence is "
            f"{result.research_confidence}, below the minimum of {config.minimum_research_confidence}."
        )
    passed = result.passed_filters and area_ok and confidence_ok
    summary = result.summary
    if not passed and summary.startswith("Passes your filters."):
        summary = "Does not pass your filters." + summary[len("Passes your filters.") :]
    return _copy_result(result, passed_filters=passed, filter_notes=notes, summary=summary)


def overlaps_selected_areas(record: ProfessorRecord, areas: list[str]) -> bool:
    """Use the existing matcher to see whether any selected area appears."""

    if not areas:
        return False
    probe = UserProfile(
        research_interests=[ResearchInterest(area) for area in areas],
        preferred_fields=list(areas),
    )
    result = match_professor(probe, record.to_match_professor())
    return result.research_score > 0


def attach_match(
    profile: UserProfile, config: SearchConfig, record: ProfessorRecord
) -> ProfessorRecord:
    """Store the match result on the professor record."""

    result = evaluate_professor(profile, config, record)
    record.match_result = asdict(result)
    return record


def collect_from_sources(
    config: SearchConfig, sources: list[AcademicSource]
) -> list[ProfessorRecord]:
    """Ask each source for every configured area. Offline sources return nothing."""

    found: list[ProfessorRecord] = []
    for source in sources:
        for query in build_queries(config):
            for record in source.search_professors(query):
                if not record.papers:
                    author_key = record.professor_profile_url or _first(record.source_urls)
                    if author_key:
                        papers = source.get_papers(author_key)
                        if papers:
                            record.papers = papers
                found.append(record)
    return found


def _confidence_meets(actual: str, minimum: str) -> bool:
    return CONFIDENCE_RANK.get(actual, 1) >= CONFIDENCE_RANK.get(minimum, 2)


def _confidence_setting(value: object) -> str:
    if value is None or value == "":
        return "medium"
    if not isinstance(value, str):
        raise ValueError("minimum_research_confidence must be low, medium, or high.")
    cleaned = value.strip().casefold()
    if cleaned not in CONFIDENCE_RANK:
        raise ValueError("minimum_research_confidence must be low, medium, or high.")
    return cleaned


def _copy_result(
    result: MatchResult,
    *,
    passed_filters: bool,
    filter_notes: list[str],
    summary: str,
) -> MatchResult:
    return MatchResult(
        professor_name=result.professor_name,
        university=result.university,
        passed_filters=passed_filters,
        filter_notes=filter_notes,
        research_score=result.research_score,
        matched_interests=list(result.matched_interests),
        matched_fields=list(result.matched_fields),
        paper_notes=list(result.paper_notes),
        summary=summary,
        matched_areas=list(result.matched_areas),
        evidence=list(result.evidence),
        research_confidence=result.research_confidence,
    )


def _country_filter(value: object) -> CountryFilter:
    if not isinstance(value, dict):
        raise ValueError("country must be an object with mode and countries.")
    return CountryFilter.from_dict(value)


def _city_filter(value: object) -> CityFilter:
    if not isinstance(value, dict):
        raise ValueError("cities must be an object with mode and cities.")
    return CityFilter.from_dict(value)


def _university_filter(value: object) -> UniversityFilter:
    if not isinstance(value, dict):
        raise ValueError("universities must be an object with mode and universities.")
    return UniversityFilter.from_dict(value)


def _area_filter(value: object) -> ResearchAreaFilter:
    if not isinstance(value, dict):
        raise ValueError("research_areas must be an object with mode and areas.")
    mode = value.get("mode", "any")
    areas = value.get("areas", [])
    if not isinstance(mode, str):
        raise ValueError("research_areas.mode must be any, one, or multiple.")
    if not isinstance(areas, list) or not all(isinstance(item, str) for item in areas):
        raise ValueError("research_areas.areas must be a list of strings.")
    return ResearchAreaFilter(
        mode=mode.strip().casefold(),
        areas=[item.strip() for item in areas if item.strip()],
    )


def _first(values: list[str]) -> str:
    return values[0] if values else ""

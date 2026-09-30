"""Run professor discovery and save records that pass the search filters.

The default sources do not use the network, so running discovery without an
HTTP client leaves the professor file unchanged. This module does not send email.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app.config import get_professors_path
from app.models import ProfessorRecord
from app.profile import load_profile
from app.research import attach_match, build_queries, load_search_config
from app.sources import AcademicSource, OfficialUniversitySource, OpenAlexSource
from app.storage import ProfessorStore, record_errors


@dataclass
class DiscoveryReport:
    """Counts from one discovery run. This does not contain invented profile fields."""

    api_results: int = 0
    discovered: int = 0
    passed_filters: int = 0
    saved_count: int = 0
    skipped_missing_name: int = 0
    skipped_missing_university: int = 0
    missing_country: int = 0
    outside_china: int = 0
    errors: list[str] = field(default_factory=list)


def default_sources() -> list[AcademicSource]:
    """Return the built-in sources with live network access left off."""

    return [OpenAlexSource(), OfficialUniversitySource()]


def run_discovery(
    *,
    profile_path: Path | None = None,
    config_path: Path | None = None,
    store_path: Path | None = None,
    sources: list[AcademicSource] | None = None,
    max_saved: int | None = None,
    report: DiscoveryReport | None = None,
) -> list[ProfessorRecord]:
    """Search configured sources, score matches, and store professors who pass.

    Professors outside the country, city, university, or research-area limits
    are not stored. Missing emails and cities stay null when a source omits them.
    max_saved stops the run before more than that many records are written.
    """

    profile = load_profile(profile_path)
    config = load_search_config(config_path)
    store = ProfessorStore(store_path or get_professors_path())
    active_sources = default_sources() if sources is None else sources
    saved: list[ProfessorRecord] = []
    saved_ids: set[str] = set()
    for source in active_sources:
        for query in build_queries(config):
            if max_saved is not None and len(saved) >= max_saved:
                break
            try:
                batch = source.search_professors(query)
            except (OSError, ValueError, TimeoutError) as exc:
                if report is not None:
                    report.errors.append(f"{source.source_name} / {query.research_area}: {exc}")
                continue
            _copy_source_counts(source, report)
            for record in batch:
                if report is not None:
                    report.discovered += 1
                    if record.country is None:
                        report.missing_country += 1
                    elif record.country.casefold() != "china":
                        report.outside_china += 1
                if not record.papers:
                    author_key = record.professor_profile_url or _first_url(record.source_urls)
                    if author_key:
                        try:
                            papers = source.get_papers(author_key)
                        except (OSError, ValueError, TimeoutError) as exc:
                            papers = []
                            if report is not None:
                                report.errors.append(f"papers for {record.name}: {exc}")
                        if papers:
                            record.papers = papers
                if record_errors(record):
                    continue
                scored = attach_match(profile, config, record)
                passed = bool(scored.match_result and scored.match_result.get("passed_filters"))
                if not passed:
                    continue
                if report is not None:
                    report.passed_filters += 1
                if max_saved is not None and len(saved) >= max_saved:
                    break
                stored = store.add(scored)
                if stored.professor_id not in saved_ids:
                    saved.append(stored)
                    saved_ids.add(stored.professor_id)
        if max_saved is not None and len(saved) >= max_saved:
            break
    if report is not None:
        report.saved_count = len(saved)
        _copy_source_counts(active_sources[0] if active_sources else None, report)
    return saved


def _copy_source_counts(source: AcademicSource | None, report: DiscoveryReport | None) -> None:
    if report is None or source is None:
        return
    report.api_results = getattr(source, "api_result_count", report.api_results)
    report.skipped_missing_name = getattr(source, "skipped_missing_name", report.skipped_missing_name)
    report.skipped_missing_university = getattr(
        source, "skipped_missing_university", report.skipped_missing_university
    )


def _first_url(values: list[str]) -> str:
    return values[0] if values else ""

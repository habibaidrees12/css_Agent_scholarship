"""Autonomous CSC research pass.

The pass loads stored candidates, resolves an official university domain from
search results, fetches official pages, verifies identity, scores research
overlap with the existing matcher, and prepares drafts. It does not send email.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from app.models import MatchResult, ProfessorRecord, UserProfile
from app.outreach import (
    SENDS_EMAIL as _OUTREACH_SENDS_EMAIL,
    draft_csc_email,
    drafts_path,
    recent_papers,
    shortlist_path,
    tracker_entry,
    tracker_path,
    write_json,
)
from app.profile import load_profile
from app.research import SearchConfig, attach_match, evaluate_professor, load_search_config
from app.search_api import NO_SEARCH_API, MemorySearchCache, SearchCache, build_search_provider, read_search_settings
from app.sources import AcademicSource, attach_recent_papers, classify_public_url, is_generic_campus_page
from app.storage import ProfessorStore, is_same_professor
from app.university_sources import OFFICIAL_SOURCE_KINDS, CatalogUniversitySource, is_official_university_url
from app.verification import SENDS_EMAIL as _VERIFICATION_SENDS_EMAIL
from app.verification import page_mentions_name, verify_professor
from app.web_discovery import SearchResponse, extract_page_facts, reject_reason
from app.web_discovery import _page_from_facts
from app.web_fetcher import OfficialPageFetcher

SENDS_EMAIL = False
_STOP_WORDS = {
    "university",
    "institute",
    "technology",
    "college",
    "school",
    "department",
    "faculty",
    "china",
    "chinese",
    "national",
    "and",
    "the",
    "for",
    "of",
}


@dataclass
class PipelineLimits:
    """Conservative caps. Raise these only after a small run looks right."""

    max_candidates_per_run: int = 10
    max_search_queries_per_candidate: int = 5
    max_results_per_query: int = 5
    max_urls_per_candidate: int = 2


@dataclass
class UniversityDomain:
    university: str
    official_domain: str | None = None
    domain_source: str | None = None
    domain_confidence: str = "none"


@dataclass
class PipelineReport:
    message: str = ""
    api_configured: bool = False
    provider: str | None = None
    candidates_processed: int = 0
    professors_discovered: int = 0
    official_profiles_found: int = 0
    verified_professors: int = 0
    public_academic_emails_found: int = 0
    strong_matches: int = 0
    email_drafts_generated: int = 0
    emails_sent: int = 0
    domains: list[UniversityDomain] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "message": self.message,
            "api_configured": self.api_configured,
            "provider": self.provider,
            "candidates_processed": self.candidates_processed,
            "professors_discovered": self.professors_discovered,
            "official_profiles_found": self.official_profiles_found,
            "verified_professors": self.verified_professors,
            "public_academic_emails_found": self.public_academic_emails_found,
            "strong_matches": self.strong_matches,
            "email_drafts_generated": self.email_drafts_generated,
            "emails_sent": self.emails_sent,
        }


def build_targeted_queries(
    name: str,
    university: str,
    department: str | None = None,
    research_areas: list[str] | None = None,
    official_domain: str | None = None,
    *,
    limit: int = 5,
) -> list[str]:
    """Build several specific queries. One generic query is not the whole search."""

    queries = [
        f'"{name}" "{university}" professor',
        f'"{name}" "{university}" faculty',
    ]
    if official_domain:
        queries.append(f'"{name}" "{university}" site:{official_domain}')
    for area in research_areas or []:
        cleaned = area.strip()
        if cleaned and cleaned.casefold() != "computer science" and len(cleaned) <= 120:
            queries.append(f'"{name}" "{university}" "{cleaned}"')
            break
    queries.append(f'"{name}" "{university}" email')
    if department and department.strip():
        queries.append(f'"{name}" "{university}" "{department.strip()}"')
    unique: list[str] = []
    for query in queries:
        if query not in unique:
            unique.append(query)
    if limit < 1:
        return []
    return unique[:limit]


def university_queries(university: str) -> list[str]:
    return [
        f'"{university}" official website',
        f'"{university}" site:edu.cn',
        f'"{university}" site:ac.cn',
    ]


def distinctive_tokens(university: str) -> list[str]:
    tokens = re.findall(r"[A-Za-z]{4,}", university)
    return [token for token in tokens if token.casefold() not in _STOP_WORDS]


def official_domain_from_url(url: str) -> str | None:
    host = urlparse(url).hostname
    if not host:
        return None
    host = host.casefold()
    if host.startswith("www."):
        host = host[4:]
    if not is_official_university_url(f"https://{host}/"):
        return None
    return host


def institution_domain(host: str) -> str | None:
    """Return the campus domain, such as bit.edu.cn from pure.bit.edu.cn.

    english.bit.edu.cn and pure.bit.edu.cn share one institution. cau.edu.cn and
    scau.edu.cn stay different. Hosts outside .edu.cn and .ac.cn return None.
    """

    cleaned = host.casefold()
    if cleaned.startswith("www."):
        cleaned = cleaned[4:]
    parts = cleaned.split(".")
    if len(parts) < 3 or ".".join(parts[-2:]) not in {"edu.cn", "ac.cn"}:
        return None
    return ".".join(parts[-3:])


def domain_matches(url: str, domain: str | None) -> bool:
    if not domain:
        return False
    host = urlparse(url).hostname
    if not host:
        return False
    host = host.casefold()
    if host.startswith("www."):
        host = host[4:]
    domain = domain.casefold()
    if domain.startswith("www."):
        domain = domain[4:]
    if host == domain or host.endswith("." + domain):
        return True
    left = institution_domain(host)
    right = institution_domain(domain)
    return bool(left and right and left == right)


def page_supports_university(text: str, university: str) -> bool:
    haystack = text.casefold()
    if university.casefold() in haystack:
        return True
    tokens = distinctive_tokens(university)
    if not tokens:
        return False
    return all(token.casefold() in haystack for token in tokens[:2])


def qualifies_for_shortlist(record: ProfessorRecord, match: MatchResult) -> bool:
    """Keep a candidate only with an official identity and specific overlap."""

    if record.verification_status != "officially_verified":
        return False
    if not record.official_profile_url and not record.official_department_url:
        return False
    if record.official_source not in OFFICIAL_SOURCE_KINDS:
        return False
    if match.research_confidence not in {"medium", "high"}:
        return False
    if not match.passed_filters or match.research_score <= 0:
        return False
    areas = [area for area in match.matched_areas if area.casefold() != "computer science"]
    return bool(areas)


def shortlist_payload(record: ProfessorRecord, match: MatchResult) -> dict[str, object]:
    papers = [paper.to_dict() for paper in record.papers]
    return {
        "name": record.name,
        "university": record.university,
        "country": record.country,
        "city": record.city,
        "department": record.department or record.official_department,
        "public_email": record.public_email,
        "official_profile_url": record.official_profile_url,
        "official_department_url": record.official_department_url,
        "official_source": record.official_source,
        "research_interests": list(record.research_interests),
        "papers": papers,
        "matched_areas": list(match.matched_areas),
        "match_score": match.research_score,
        "research_confidence": match.research_confidence,
        "evidence": list(match.evidence),
        "recent_research": recent_papers(record),
        "reasons": [match.summary] if match.summary else [],
        "verification_status": record.verification_status,
        "verification_notes": record.verification_notes,
        "verified_at": record.verified_at,
        "source_urls": list(record.source_urls),
    }


def publish_review(
    records: list[ProfessorRecord] | None = None,
    *,
    profile: UserProfile | None = None,
    config: SearchConfig | None = None,
    output_dir: Path | None = None,
    write_outputs: bool = True,
) -> PipelineReport:
    """Rank stored records and write drafts. This does not search or send mail."""

    user = profile or load_profile()
    search_config = config or load_search_config()
    chosen = list(records or [])
    report = PipelineReport(
        message="Review uses stored records. No search was run.",
        api_configured=configuration_report().api_configured,
        emails_sent=0,
    )
    shortlist: list[dict[str, object]] = []
    drafts: list[dict[str, object]] = []
    tracker = []
    for record in chosen:
        report.candidates_processed += 1
        report.professors_discovered += 1
        matched = attach_match(user, search_config, record)
        result = evaluate_professor(user, search_config, matched)
        if matched.verification_status == "officially_verified":
            report.verified_professors += 1
        if matched.official_profile_url or matched.official_department_url:
            report.official_profiles_found += 1
        if matched.public_email:
            report.public_academic_emails_found += 1
        if qualifies_for_shortlist(matched, result):
            report.strong_matches += 1
            shortlist.append(shortlist_payload(matched, result))
            draft = draft_csc_email(user, matched, result)
            if draft is not None:
                drafts.append(draft)
                report.email_drafts_generated += 1
            tracker.append(tracker_entry(matched, result, has_draft=draft is not None).to_dict())
    shortlist.sort(key=lambda item: int(item["match_score"]), reverse=True)
    if write_outputs:
        write_json(shortlist_path(output_dir), shortlist)
        write_json(drafts_path(output_dir), drafts)
        write_json(tracker_path(output_dir), tracker)
    report.emails_sent = 0
    return report


def configuration_report() -> PipelineReport:
    settings = read_search_settings()
    provider = build_search_provider(settings)
    report = PipelineReport(
        message=settings.status_message(),
        api_configured=provider is not None,
        provider=provider.name if provider is not None else None,
        emails_sent=0,
    )
    if provider is None:
        report.message = NO_SEARCH_API
    return report


def run_pipeline(
    *,
    records: list[ProfessorRecord] | None = None,
    provider=None,
    fetcher: OfficialPageFetcher | None = None,
    limits: PipelineLimits | None = None,
    profile: UserProfile | None = None,
    config: SearchConfig | None = None,
    store: ProfessorStore | None = None,
    output_dir: Path | None = None,
    cache: SearchCache | None = None,
    write_outputs: bool = True,
    use_configured_provider: bool = True,
    paper_source: AcademicSource | None = None,
) -> PipelineReport:
    """Run a bounded research pass. With no search API, return a configuration check."""

    if SENDS_EMAIL or _OUTREACH_SENDS_EMAIL or _VERIFICATION_SENDS_EMAIL:
        raise RuntimeError("Email sending is not enabled.")
    settings = read_search_settings()
    active = provider
    if active is None and use_configured_provider:
        active = build_search_provider(settings, cache=cache)
    report = PipelineReport(emails_sent=0)
    if active is None:
        report.message = NO_SEARCH_API
        report.api_configured = False
        return report

    report.api_configured = True
    report.provider = getattr(active, "name", None)
    report.message = f"Search provider: {report.provider}."
    bounds = limits or PipelineLimits()
    user = profile or load_profile()
    search_config = config or load_search_config()
    page_fetcher = fetcher or OfficialPageFetcher(timeout=12)
    memory = cache or getattr(active, "cache", None) or MemorySearchCache()
    chosen = list(records if records is not None else (store.load() if store is not None else []))
    if bounds.max_candidates_per_run >= 0:
        chosen = chosen[: bounds.max_candidates_per_run]

    domains: dict[str, UniversityDomain] = {}
    shortlist: list[dict[str, object]] = []
    drafts: list[dict[str, object]] = []
    tracker = []
    verified_rows: list[ProfessorRecord] = []

    for record in chosen:
        report.candidates_processed += 1
        report.professors_discovered += 1
        domain = domains.get(_domain_key(record.university))
        if domain is None:
            domain = resolve_university_domain(
                record.university,
                active,
                page_fetcher,
                cache=memory,
                result_limit=bounds.max_results_per_query,
            )
            domains[_domain_key(record.university)] = domain
            report.domains.append(domain)
        verified = research_candidate(
            record,
            provider=active,
            fetcher=page_fetcher,
            domain=domain,
            limits=bounds,
            cache=memory,
        )
        if paper_source is not None:
            verified = attach_recent_papers(verified, paper_source)
        matched = attach_match(user, search_config, verified)
        result = evaluate_professor(user, search_config, matched)
        if verified.verification_status == "officially_verified":
            report.verified_professors += 1
        if verified.official_profile_url or verified.official_department_url:
            report.official_profiles_found += 1
        if verified.public_email:
            report.public_academic_emails_found += 1
        if qualifies_for_shortlist(matched, result):
            report.strong_matches += 1
            shortlist.append(shortlist_payload(matched, result))
            draft = draft_csc_email(user, matched, result)
            if draft is not None:
                drafts.append(draft)
                report.email_drafts_generated += 1
            tracker.append(tracker_entry(matched, result, has_draft=draft is not None).to_dict())
        verified_rows.append(matched)

    shortlist.sort(key=lambda item: int(item["match_score"]), reverse=True)
    if store is not None:
        _save_verified(store, verified_rows)
    if write_outputs:
        folder = output_dir
        write_json(shortlist_path(folder), shortlist)
        write_json(drafts_path(folder), drafts)
        write_json(tracker_path(folder), tracker)
    report.emails_sent = 0
    report.message = (
        f"Search provider: {report.provider}. "
        f"Processed {report.candidates_processed} stored candidates. No email was sent."
    )
    return report


def research_and_draft(
    *,
    limit: int = 5,
    records: list[ProfessorRecord] | None = None,
    provider=None,
    fetcher: OfficialPageFetcher | None = None,
    paper_source: AcademicSource | None = None,
    store: ProfessorStore | None = None,
    output_dir: Path | None = None,
    profile: UserProfile | None = None,
    config: SearchConfig | None = None,
    cache: SearchCache | None = None,
    write_outputs: bool = True,
    enable_openalex: bool = False,
) -> PipelineReport:
    """Run one batch: discover, verify, papers, match, shortlist, and unsent drafts.

    OpenAlex is contacted only when enable_openalex is true or a paper source
    is passed in. This function does not send email.
    """

    if SENDS_EMAIL or _OUTREACH_SENDS_EMAIL or _VERIFICATION_SENDS_EMAIL:
        raise RuntimeError("Email sending is not enabled.")
    bounds = PipelineLimits()
    bounds.max_candidates_per_run = limit
    saving_store = store
    if records is None:
        saving_store = store if store is not None else ProfessorStore()
        chosen = saving_store.load()
    else:
        chosen = list(records)
    source = paper_source
    if source is None and enable_openalex:
        from app.sources import OpenAlexSource

        source = OpenAlexSource(enable_network=True)
    report = run_pipeline(
        records=chosen,
        provider=provider,
        fetcher=fetcher,
        limits=bounds,
        profile=profile,
        config=config,
        store=saving_store,
        output_dir=output_dir,
        cache=cache,
        write_outputs=write_outputs,
        use_configured_provider=provider is None,
        paper_source=source,
    )
    report.emails_sent = 0
    report.message = (
        f"{report.message} Paper titles come from OpenAlex when that source returned them. "
        "Drafts are saved for a later Gmail step. Gmail was not contacted."
    )
    return report


def resolve_university_domain(
    university: str,
    provider,
    fetcher: OfficialPageFetcher,
    *,
    cache: SearchCache | None = None,
    result_limit: int = 5,
) -> UniversityDomain:
    """Find the official domain from search results and a fetched page.

    The university name is not converted into a domain by a lookup table.
    """

    found = UniversityDomain(university=university)
    if not university.strip():
        return found
    for query in university_queries(university):
        response = _cached_search(provider, query, result_limit, cache)
        for result in response.results:
            if reject_reason(result.url):
                continue
            domain = official_domain_from_url(result.url)
            if not domain:
                continue
            fetched = fetcher.fetch(result.url)
            final_url = fetched.final_url or fetched.requested_url
            if fetched.blocked or fetched.error or not fetched.text.strip():
                continue
            if reject_reason(final_url):
                continue
            if not page_supports_university("\n".join((fetched.title, fetched.text)), university):
                continue
            confirmed = official_domain_from_url(final_url) or domain
            found.official_domain = confirmed
            found.domain_source = final_url
            found.domain_confidence = "high"
            return found
    return found


def research_candidate(
    record: ProfessorRecord,
    *,
    provider,
    fetcher: OfficialPageFetcher,
    domain: UniversityDomain,
    limits: PipelineLimits,
    cache: SearchCache | None = None,
) -> ProfessorRecord:
    """Search inside the official domain, then verify any page that was fetched."""

    if not domain.official_domain:
        return record
    queries = build_targeted_queries(
        record.name,
        record.university,
        record.department,
        record.research_interests,
        domain.official_domain,
        limit=limits.max_search_queries_per_candidate,
    )
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for query in queries:
        response = _cached_search(provider, query, limits.max_results_per_query, cache)
        for result in response.results:
            key = result.url.rstrip("/").casefold()
            if key in seen or not _acceptable_profile_url(result.url, domain.official_domain):
                continue
            seen.add(key)
            found.append((result.url, result.title))
    specific = [item for item in found if not is_generic_campus_page(item[0])]
    ranked = sorted(specific or found, key=lambda item: _profile_priority(item[0], item[1], record.name))
    urls = [url for url, _title in ranked[: limits.max_urls_per_candidate]]

    current = record
    checked = 0
    for url in urls:
        fetched = fetcher.fetch(url)
        final_url = fetched.final_url or fetched.requested_url
        if fetched.blocked or fetched.error or not fetched.text.strip():
            continue
        if not _acceptable_profile_url(final_url, domain.official_domain):
            continue
        facts = extract_page_facts(fetched.text, title=fetched.title, url=final_url, record=current)
        page = _page_from_facts(facts, final_url)
        if classify_public_url(final_url) not in OFFICIAL_SOURCE_KINDS:
            continue
        verified = verify_professor(
            current,
            CatalogUniversitySource(
                pages=[page],
                assignments=[(current.name, current.university, [final_url])],
            ),
            verified_at=_now(),
        )
        checked += 1
        if verified.verification_status == "officially_verified":
            _fill_public_gaps(verified)
            return verified
        current = verified
        if checked >= limits.max_urls_per_candidate:
            break
    return current


def _profile_priority(url: str, title: str, name: str) -> tuple[int, int, int]:
    """Prefer a page whose title names the person over a faculty directory."""

    path = urlparse(url).path.casefold()
    person_path = any(
        marker in path
        for marker in ("/teacher", "/profile", "/lab", "/people", "/persons", "/staff", "/szdw")
    )
    named = page_mentions_name(title or "", name)
    return (0 if named else 1, 0 if person_path else 1, 0 if not is_generic_campus_page(url) else 1)


def _acceptable_profile_url(url: str, domain: str) -> bool:
    if reject_reason(url):
        return False
    if not domain_matches(url, domain):
        return False
    return classify_public_url(url) in OFFICIAL_SOURCE_KINDS


def _fill_public_gaps(record: ProfessorRecord) -> None:
    """Copy an official value into a blank public field. Conflicts stay unchanged."""

    if record.public_email is None and record.official_email:
        record.public_email = record.official_email
    if record.department is None and record.official_department:
        record.department = record.official_department
    if record.city is None and record.official_city:
        record.city = record.official_city


def _cached_search(provider, query: str, limit: int, cache: SearchCache | None) -> SearchResponse:
    key = f"{getattr(provider, 'name', 'search')}|{query}|{limit}"
    if cache is not None:
        cached = cache.get(key)
        if cached is not None:
            return cached
    response = provider.search(query, limit=limit)
    if cache is not None:
        cache.put(key, response)
    return response


def _save_verified(store: ProfessorStore, records: list[ProfessorRecord]) -> None:
    stored = store.load()
    for incoming in records:
        replaced = False
        for index, existing in enumerate(stored):
            if existing.professor_id and existing.professor_id == incoming.professor_id:
                stored[index] = incoming
                replaced = True
                break
            if is_same_professor(existing, incoming):
                incoming.professor_id = existing.professor_id or incoming.professor_id
                stored[index] = incoming
                replaced = True
                break
        if not replaced:
            stored.append(incoming)
    store.save(stored)


def _domain_key(university: str) -> str:
    return " ".join(university.casefold().split())


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

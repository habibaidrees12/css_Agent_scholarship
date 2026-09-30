"""Academic sources that can supply professor and paper records.

This module defines the provider interface. The first provider talks to the
OpenAlex academic-paper API only when a caller turns on network access.
Official university pages are represented as a source, and this module does
not fetch them yet. Nothing here writes the professor database, sends email,
or fills in a missing email, city, or abstract.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date

from app.config import RECENT_PAPER_YEARS
from app.models import PaperRecord, ProfessorRecord

# Later collection should prefer these, in this order.
# Blind scraping of unrelated websites is not one of them.
SOURCE_PRIORITY = (
    "official_university_domain",
    "official_department_page",
    "official_professor_or_lab_page",
    "reputable_academic_paper_source",
)

# Country names we can store when an API sends an ISO code and no country name.
# This does not infer a city.
COUNTRY_NAMES_BY_CODE = {
    "CN": "China",
    "KR": "South Korea",
}

OPENALEX_AUTHORS_URL = "https://api.openalex.org/authors"
OPENALEX_WORKS_URL = "https://api.openalex.org/works"


@dataclass
class SearchQuery:
    """One discovery query. City and university limits are optional."""

    research_area: str
    country: str | None = None
    city_mode: str = "any"
    cities: list[str] = field(default_factory=list)
    university_mode: str = "any"
    universities: list[str] = field(default_factory=list)


class AcademicSource(ABC):
    """A public academic source that can search, open a profile, and list papers."""

    source_name = "academic-source"
    source_kind = "unknown"
    trustworthy = False

    @abstractmethod
    def search_professors(self, query: SearchQuery) -> list[ProfessorRecord]:
        """Return professor records for one query. Return an empty list when offline."""

    @abstractmethod
    def get_professor_profile(self, profile_url: str) -> ProfessorRecord | None:
        """Return one professor profile, or None when it cannot be read."""

    @abstractmethod
    def get_papers(self, author_key: str) -> list[PaperRecord]:
        """Return papers already published by the source. Do not invent titles."""


def classify_public_url(url: str) -> str | None:
    """Label a URL when it is an official campus host or a known academic index.

    A result of None means this URL is not trusted yet. The function does not
    download the page.
    """

    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    host = parsed.hostname.casefold()
    path = parsed.path.casefold()
    if host.endswith(("openalex.org", "semanticscholar.org", "crossref.org", "doi.org")):
        return "reputable_academic_paper_source"
    if not host.endswith((".edu.cn", ".ac.cn")):
        return None
    if any(marker in path for marker in ("/teacher", "/profile", "/lab", "/people", "/persons", "/staff", "/faculty/", "/szdw")):
        return "official_professor_or_lab_page"
    if any(marker in path for marker in ("/department", "/school", "/college", "/faculty")):
        return "official_department_page"
    return "official_university_domain"


def is_generic_campus_page(url: str) -> bool:
    """True for a university front page or news item, not a person's profile.

    A homepage can name the university without identifying one professor.
    """

    parsed = urllib.parse.urlparse((url or "").strip())
    path = (parsed.path or "").casefold()
    trimmed = path.rstrip("/")
    if trimmed in {"", "/en", "/cn", "/home", "/main.psp", "/main.htm", "/default.html", "/default.htm"}:
        return True
    leaf = trimmed.rsplit("/", 1)[-1]
    if leaf.startswith("index"):
        return True
    if any(piece in path for piece in ("/news", "/news_events", "/factsandfigures", "/facts-and-figures")):
        return True
    return re.search(r"/20\d{2}[-/]\d{2}/", path) is not None


def apply_source_verification(record: ProfessorRecord, source_kind: str | None) -> ProfessorRecord:
    """Mark a record verified only when a trusted source supplied the identity."""

    trusted = source_kind in SOURCE_PRIORITY
    has_identity = bool(record.name.strip() and record.university.strip() and record.source_urls)
    record.source_kind = source_kind
    if trusted and has_identity:
        record.verified = True
        record.last_verified = date.today().isoformat()
    else:
        record.verified = False
        record.last_verified = None
    return record


def attach_recent_papers(record: ProfessorRecord, source: AcademicSource, *, today=None, limit: int = 5) -> ProfessorRecord:
    """Add papers OpenAlex actually returned. Missing abstracts stay null.

    Recent papers are preferred. If none are recent, older retrieved titles are
    kept and are not described as recent. This does not invent a paper.
    """

    author_url = _openalex_author_url(record.source_urls)
    if author_url is None or limit < 1:
        return record
    fetched = source.get_papers(author_url)
    titled = [paper for paper in fetched if paper.title.strip()]
    if not titled:
        return record
    current = today or date.today()
    cutoff = current.year - RECENT_PAPER_YEARS
    recent = [paper for paper in titled if paper.year is not None and paper.year >= cutoff]
    pool = recent or titled
    pool = sorted(pool, key=lambda paper: paper.year or 0, reverse=True)[:limit]
    updated = ProfessorRecord.from_dict(record.to_dict())
    known = {paper.title.casefold() for paper in updated.papers if paper.title}
    for paper in pool:
        key = paper.title.casefold()
        if key in known:
            continue
        updated.papers.append(paper)
        known.add(key)
    return updated


def _openalex_author_url(urls: list[str]) -> str | None:
    for url in urls:
        if isinstance(url, str) and "openalex.org" in url.casefold() and _openalex_id(url):
            return url
    return None


def abstract_from_inverted_index(index: object) -> str | None:
    """Rebuild an abstract that OpenAlex stored as an inverted index.

    A missing or empty index stays None. This does not write a substitute summary.
    """

    if not isinstance(index, dict) or not index:
        return None
    placed: list[tuple[int, str]] = []
    for word, positions in index.items():
        if not isinstance(word, str) or not isinstance(positions, list):
            return None
        for position in positions:
            if isinstance(position, bool) or not isinstance(position, int):
                return None
            placed.append((position, word))
    if not placed:
        return None
    placed.sort(key=lambda item: item[0])
    return " ".join(word for _, word in placed)


class OpenAlexSource(AcademicSource):
    """OpenAlex author and works API.

    Live requests stay off unless enable_network is true or an HTTP function
    is passed in. Tests can pass a fake HTTP function. No API key is required.
    """

    source_name = "openalex"
    source_kind = "reputable_academic_paper_source"
    trustworthy = True

    def __init__(
        self,
        *,
        http_get=None,
        enable_network: bool = False,
        mailto: str | None = None,
    ) -> None:
        if http_get is not None:
            self._http_get = http_get
        elif enable_network:
            self._http_get = fetch_json
        else:
            self._http_get = None
        self.mailto = mailto.strip() if isinstance(mailto, str) and mailto.strip() else None
        self.api_result_count = 0
        self.skipped_missing_name = 0
        self.skipped_missing_university = 0

    def search_professors(self, query: SearchQuery) -> list[ProfessorRecord]:
        if self._http_get is None:
            return []
        payload = self._http_get(self.build_search_url(query))
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            return []
        records = []
        for item in results:
            self.api_result_count += 1
            if not isinstance(item, dict):
                self.skipped_missing_university += 1
                continue
            if not _clean_text(item.get("display_name")):
                self.skipped_missing_name += 1
                continue
            institution = _first_dict(item.get("last_known_institutions"))
            if not _clean_text(institution.get("display_name")):
                self.skipped_missing_university += 1
                continue
            record = self.parse_author(item)
            if record is not None:
                records.append(record)
        return records

    def get_professor_profile(self, profile_url: str) -> ProfessorRecord | None:
        if self._http_get is None or not profile_url.strip():
            return None
        author_id = _openalex_id(profile_url)
        if not author_id:
            return None
        payload = self._http_get(self._url(f"https://api.openalex.org/authors/{author_id}"))
        if not isinstance(payload, dict):
            return None
        return self.parse_author(payload)

    def get_papers(self, author_key: str) -> list[PaperRecord]:
        if self._http_get is None:
            return []
        author_id = _openalex_id(author_key)
        if not author_id:
            return []
        url = self._url(
            OPENALEX_WORKS_URL,
            {"filter": f"authorships.author.id:{author_id}", "per-page": "25"},
        )
        payload = self._http_get(url)
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            return []
        papers = []
        for item in results:
            paper = self.parse_work(item)
            if paper is not None:
                papers.append(paper)
        return papers

    def build_search_url(self, query: SearchQuery) -> str:
        """Build an author search URL. City mode 'any' does not add a city."""

        search_terms = [query.research_area.strip()]
        if query.university_mode in {"one", "multiple"}:
            search_terms.extend(name.strip() for name in query.universities if name.strip())
        params = {"search": " ".join(search_terms), "per-page": "25"}
        filters = []
        country_code = _country_code(query.country)
        if country_code:
            filters.append(f"last_known_institutions.country_code:{country_code}")
        if filters:
            params["filter"] = ",".join(filters)
        return self._url(OPENALEX_AUTHORS_URL, params)

    def parse_author(self, payload: dict) -> ProfessorRecord | None:
        """Copy only fields the payload actually contains."""

        if not isinstance(payload, dict):
            return None
        name = _clean_text(payload.get("display_name"))
        institution = _first_dict(payload.get("last_known_institutions"))
        university = _clean_text(institution.get("display_name"))
        if not name or not university:
            return None
        source_url = _http_text(payload.get("id"))
        record = ProfessorRecord(
            name=name,
            title=None,
            university=university,
            department=None,
            country=_country_from_institution(institution),
            city=_city_from_institution(institution),
            university_website=_http_text(institution.get("homepage_url")),
            professor_profile_url=None,
            public_email=_email_from(payload),
            research_interests=_labels_from(payload, ("topics", "x_concepts")),
            papers=[],
            source_urls=[source_url] if source_url else [],
            notes="",
        )
        return apply_source_verification(record, self.source_kind if source_url else None)

    def parse_work(self, payload: dict) -> PaperRecord | None:
        """Copy one work. A missing abstract stays null."""

        if not isinstance(payload, dict):
            return None
        title = _clean_text(payload.get("title")) or _clean_text(payload.get("display_name"))
        if not title:
            return None
        year = payload.get("publication_year")
        if isinstance(year, bool) or not isinstance(year, int):
            year = None
        return PaperRecord(
            title=title,
            year=year,
            abstract=abstract_from_inverted_index(payload.get("abstract_inverted_index")),
            keywords=_labels_from(payload, ("concepts", "topics")),
            paper_url=_http_text(payload.get("doi")),
            source_url=_http_text(payload.get("id")),
        )

    def _url(self, base: str, params: dict[str, str] | None = None) -> str:
        query = dict(params or {})
        if self.mailto:
            query["mailto"] = self.mailto
        if not query:
            return base
        return f"{base}?{urllib.parse.urlencode(query)}"


class OfficialUniversitySource(AcademicSource):
    """Official university, department, and professor pages.

    Search is not connected yet, so these methods return no records.
    They do not download arbitrary websites.
    """

    source_name = "official-university"
    source_kind = "official_university_domain"
    trustworthy = True

    def search_professors(self, query: SearchQuery) -> list[ProfessorRecord]:
        return []

    def get_professor_profile(self, profile_url: str) -> ProfessorRecord | None:
        return None

    def get_papers(self, author_key: str) -> list[PaperRecord]:
        return []


def fetch_json(url: str) -> dict:
    """HTTP helper used only when a source is explicitly allowed to use the network."""

    # A short pause keeps a live test from bursting the OpenAlex API.
    time.sleep(0.35)
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "CSCProfessorResearchAgent/0.1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise ValueError(f"OpenAlex returned HTTP {exc.code}.") from exc
    except urllib.error.URLError as exc:
        raise OSError(f"OpenAlex request failed: {exc.reason}") from exc
    if not isinstance(payload, dict):
        raise ValueError("The academic source did not return a JSON object.")
    return payload


def _country_code(country: str | None) -> str | None:
    if not country:
        return None
    cleaned = " ".join(country.casefold().split())
    codes = {name.casefold(): code for code, name in COUNTRY_NAMES_BY_CODE.items()}
    return codes.get(cleaned)


def _country_from_institution(institution: dict) -> str | None:
    geo = institution.get("geo") if isinstance(institution.get("geo"), dict) else {}
    country = _clean_text(geo.get("country"))
    if country:
        return country
    code = _clean_text(institution.get("country_code"))
    if not code:
        return None
    return COUNTRY_NAMES_BY_CODE.get(code.upper())


def _city_from_institution(institution: dict) -> str | None:
    """Use a city only when the source's geo field includes one."""

    geo = institution.get("geo") if isinstance(institution.get("geo"), dict) else {}
    return _clean_text(geo.get("city"))


def _labels_from(payload: dict, keys: tuple[str, ...]) -> list[str]:
    labels: list[str] = []
    for key in keys:
        items = payload.get(key) or []
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            label = _clean_text(item.get("display_name"))
            if label and label not in labels:
                labels.append(label)
    return labels


def _first_dict(value: object) -> dict:
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return value[0]
    return {}


def _email_from(payload: dict) -> str | None:
    """Copy an email only when the payload contains one. Otherwise return null."""

    for key in ("public_email", "email"):
        value = payload.get(key)
        if isinstance(value, str) and "@" in value:
            return value.strip()
    return None


def _clean_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _http_text(value: object) -> str | None:
    text = _clean_text(value)
    if text and text.casefold().startswith(("http://", "https://")):
        return text
    return None


def _openalex_id(value: str) -> str | None:
    text = value.strip().rstrip("/")
    if not text:
        return None
    token = text.split("/")[-1]
    if token.casefold().startswith("https:") or token.casefold().startswith("http:"):
        return None
    return token or None

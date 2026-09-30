"""Official university pages used to verify a professor.

Each university can supply its own provider later. This module does not
hard-code one campus, does not crawl the web, and does not invent profile
URLs, emails, departments, or cities.

A provider may return only pages it has actually retrieved from that
university's own domain. The default provider returns nothing.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from app.models import ProfessorRecord
from app.sources import classify_public_url

OFFICIAL_SOURCE_KINDS = (
    "official_university_domain",
    "official_department_page",
    "official_professor_or_lab_page",
)


@dataclass
class OfficialPage:
    """Facts copied from one official page. Unknown facts stay null."""

    url: str
    name: str | None = None
    university: str | None = None
    department: str | None = None
    city: str | None = None
    email: str | None = None
    affiliation: str | None = None
    research_areas: list[str] = field(default_factory=list)
    paper_titles: list[str] = field(default_factory=list)
    department_url: str | None = None


class UniversitySource(ABC):
    """A reader for official pages at one university, or a group of universities.

    Subclasses may later search that university's own site. They must not turn
    a professor's name into a guessed URL.
    """

    source_name = "university-source"

    def candidate_urls(self, record: ProfessorRecord) -> list[str]:
        """Return official profile URLs already found for this candidate."""

        return []

    @abstractmethod
    def read_page(self, url: str) -> OfficialPage | None:
        """Return facts published at this URL, or None when the page was not read."""


class OfflineUniversitySource(UniversitySource):
    """No network and no invented URLs."""

    source_name = "offline-university"

    def read_page(self, url: str) -> OfficialPage | None:
        return None


@dataclass
class _UrlAssignment:
    name: str
    university: str | None
    urls: list[str]


class CatalogUniversitySource(UniversitySource):
    """Reads pages the caller already collected.

    This is the hook a future Peking University or Tsinghua provider can use
    in tests. It does not search the internet.
    """

    source_name = "catalog-university"

    def __init__(
        self,
        pages: list[OfficialPage] | None = None,
        assignments: list[tuple[str, str | None, list[str]]] | None = None,
    ) -> None:
        self._pages = {_url_key(page.url): page for page in pages or [] if page.url.strip()}
        self._assignments = [
            _UrlAssignment(name=name, university=university, urls=list(urls))
            for name, university, urls in (assignments or [])
        ]

    def candidate_urls(self, record: ProfessorRecord) -> list[str]:
        found: list[str] = []
        for assignment in self._assignments:
            if assignment.name != record.name:
                continue
            if assignment.university is not None and _label(assignment.university) != _label(record.university):
                continue
            found.extend(assignment.urls)
        return found

    def read_page(self, url: str) -> OfficialPage | None:
        return self._pages.get(_url_key(url))


class UniversitySourceRegistry(UniversitySource):
    """Combine providers. A new university is added here, not hard-coded."""

    source_name = "university-registry"

    def __init__(self, sources: list[UniversitySource] | None = None) -> None:
        self.sources = list(sources or [])

    def add(self, source: UniversitySource) -> None:
        self.sources.append(source)

    def candidate_urls(self, record: ProfessorRecord) -> list[str]:
        found: list[str] = []
        for source in self.sources:
            found.extend(source.candidate_urls(record))
        return found

    def read_page(self, url: str) -> OfficialPage | None:
        for source in self.sources:
            page = source.read_page(url)
            if page is not None:
                return page
        return None


def is_official_university_url(url: str | None) -> bool:
    """True only for a page hosted on an official .edu.cn or .ac.cn domain.

    OpenAlex, DOI, social media, blogs, and profile aggregators are not
    official university sources.
    """

    if not url or not url.strip():
        return False
    return classify_public_url(url) in OFFICIAL_SOURCE_KINDS


def _url_key(url: str) -> str:
    return url.strip().rstrip("/").casefold()


def _label(value: str) -> str:
    return " ".join(value.casefold().split())

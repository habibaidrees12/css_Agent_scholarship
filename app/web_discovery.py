"""Find official university pages for existing professor candidates.

The pilot searches the public web, keeps .edu.cn and .ac.cn results, fetches
those pages, and runs the existing identity check. It writes
data/verification_candidates.json for review.

It does not modify data/professors.json, invent URLs, or send email.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from app.config import get_data_dir, get_professors_path
from app.models import ProfessorRecord
from app.storage import ProfessorStore
from app.university_sources import CatalogUniversitySource, OfficialPage, is_official_university_url
from app.verification import SENDS_EMAIL as _VERIFICATION_SENDS_EMAIL
from app.verification import matched_display_name, published_email, verify_professor
from app.web_fetcher import USER_AGENT, FetchedPage, OfficialPageFetcher

SENDS_EMAIL = False
_TEXT_EXCERPT = 1500
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_UNIVERSITY_RE = re.compile(
    r"\b([A-Z][A-Za-z&'’.-]*(?:\s+[A-Z][A-Za-z&'’.-]*){0,6}\s+University)\b"
)
_CHINESE_UNIVERSITY_RE = re.compile(r"[\u4e00-\u9fff]{2,16}大学")
_DEPARTMENT_RE = re.compile(r"\b(Department\s+of\s+[A-Z][^,.;\n]{2,80})")
_FACULTY_RE = re.compile(r"\b((?:School|Faculty|College)\s+of\s+[A-Z][^,.;\n]{2,80})")
_CHINESE_UNIT_RE = re.compile(r"[\u4e00-\u9fff]{2,20}(?:学院|系)")
_CITY_RE = re.compile(r"(?:City|所在城市)\s*[:：]\s*([A-Za-z\u4e00-\u9fff .'-]{2,40})")
_INTEREST_RE = re.compile(r"Research interests?\s*[:：]\s*([^\n]+)", re.IGNORECASE)

_BLOCKED_HOSTS = (
    "researchgate.net",
    "scholar.google.com",
    "google.com",
    "linkedin.com",
    "facebook.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "weibo.com",
    "zhihu.com",
    "wikipedia.org",
    "medium.com",
    "blogspot.com",
    "wordpress.com",
    "academia.edu",
    "baidu.com",
    "bing.com",
    "duckduckgo.com",
    "youtube.com",
    "github.com",
)


@dataclass
class WebDiscoverySettings:
    """Limits for one review pilot. Defaults stay small on purpose."""

    max_candidates_per_run: int = 5
    max_urls_per_candidate: int = 2
    max_results_per_query: int = 5
    request_timeout_seconds: float = 12
    request_delay_seconds: float = 1.5


@dataclass
class SearchResult:
    query: str
    title: str
    url: str
    snippet: str = ""
    engine: str = ""


@dataclass
class SearchResponse:
    query: str
    results: list[SearchResult] = field(default_factory=list)
    error: str | None = None
    engine: str = ""


class SearchProvider(ABC):
    """A replaceable web search engine. Implementations must return real hits."""

    name = "search-provider"

    @abstractmethod
    def search(self, query: str, *, limit: int = 5) -> SearchResponse:
        """Return links the engine actually listed for this query."""


class PublicWebSearchProvider(SearchProvider):
    """Search through public DuckDuckGo HTML and, if needed, Bing RSS.

    No API key is required. If those endpoints block the request or return no
    links, the error is returned as-is. This provider does not invent URLs.
    A key-based Bing or Google search API can replace this class later.
    """

    name = "public-web-search"

    def __init__(self, *, timeout: float = 12, opener=None) -> None:
        self.timeout = timeout
        self._opener = opener or urllib.request.urlopen

    def search(self, query: str, *, limit: int = 5) -> SearchResponse:
        errors: list[str] = []
        for engine, fetch in (
            ("duckduckgo-html", self._fetch_duckduckgo),
            ("bing-rss", self._fetch_bing),
        ):
            try:
                body, status = fetch(query)
            except TimeoutError:
                errors.append(f"{engine} timed out.")
                continue
            except urllib.error.HTTPError as exc:
                errors.append(f"{engine} returned HTTP {getattr(exc, 'code', 'error')}.")
                continue
            except urllib.error.URLError as exc:
                errors.append(f"{engine} connection error: {getattr(exc, 'reason', exc)}.")
                continue
            except OSError as exc:
                errors.append(f"{engine} connection error: {exc}.")
                continue
            results = parse_search_results(body, query=query, engine=engine, limit=limit)
            if results:
                return SearchResponse(query=query, results=results, engine=engine)
            errors.append(f"{engine} returned no result links (HTTP {status}).")
        return SearchResponse(query=query, error=" ".join(errors), engine="")

    def _fetch_duckduckgo(self, query: str) -> tuple[str, int]:
        data = urllib.parse.urlencode({"q": query}).encode("utf-8")
        request = urllib.request.Request(
            "https://html.duckduckgo.com/html/",
            data=data,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        return self._read(request)

    def _fetch_bing(self, query: str) -> tuple[str, int]:
        target = "https://www.bing.com/search?" + urllib.parse.urlencode(
            {"q": query, "format": "rss", "count": "10"}
        )
        request = urllib.request.Request(
            target,
            headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/xml, text/xml"},
        )
        return self._read(request)

    def _read(self, request: urllib.request.Request) -> tuple[str, int]:
        with self._opener(request, timeout=self.timeout) as response:
            status = getattr(response, "status", 200)
            raw = response.read(1_000_000)
            charset = "utf-8"
            headers = getattr(response, "headers", None)
            if headers is not None and hasattr(headers, "get"):
                content_type = headers.get("Content-Type") or ""
                match = re.search(r"charset=([A-Za-z0-9._-]+)", content_type, flags=re.IGNORECASE)
                if match:
                    charset = match.group(1)
            try:
                text = raw.decode(charset, errors="replace")
            except LookupError:
                text = raw.decode("utf-8", errors="replace")
            return text, status


def build_search_queries(
    name: str,
    university: str,
    department: str | None = None,
    research_areas: list[str] | None = None,
) -> list[str]:
    """Build the targeted queries for one existing candidate."""

    queries = [
        f'"{name}" "{university}" professor',
        f'"{name}" "{university}" site:edu.cn',
        f'"{name}" "{university}" site:ac.cn',
    ]
    if department and department.strip():
        queries.append(f'"{name}" "{university}" "{department.strip()}"')
    areas = [area.strip() for area in (research_areas or []) if area and area.strip()]
    if areas and len(areas[0]) <= 120:
        queries.append(f'"{name}" "{university}" "{areas[0]}"')
    return queries


def reject_reason(url: str) -> str | None:
    """Return None when url is an official .edu.cn or .ac.cn page."""

    parsed = urllib.parse.urlparse(url.strip()) if url else None
    host = parsed.hostname.casefold() if parsed and parsed.hostname else ""
    if not host:
        return "missing host"
    for blocked in _BLOCKED_HOSTS:
        if host == blocked or host.endswith("." + blocked):
            return f"third-party host {blocked}"
    if is_official_university_url(url):
        return None
    return "not an official .edu.cn or .ac.cn domain"


def parse_search_results(body: str, *, query: str, engine: str, limit: int) -> list[SearchResult]:
    """Pull result links out of a search response. Links must appear in body."""

    if not body:
        return []
    found: list[SearchResult] = []
    seen: set[str] = set()

    def add(href: str, title: str, snippet: str = "") -> None:
        url = _result_target(href)
        if not url:
            return
        key = url.rstrip("/").casefold()
        if key in seen:
            return
        seen.add(key)
        found.append(
            SearchResult(
                query=query,
                title=_clean_markup(title) or url,
                url=url,
                snippet=_clean_markup(snippet),
                engine=engine,
            )
        )

    if engine == "bing-rss":
        for item in re.findall(r"<item\b[^>]*>(.*?)</item>", body, flags=re.IGNORECASE | re.DOTALL):
            link = _first_tag(item, "link")
            title = _first_tag(item, "title")
            snippet = _first_tag(item, "description")
            if link:
                add(link, title, snippet)
            if len(found) >= limit:
                break
        return found[:limit]

    anchor = re.compile(
        r"<a\b([^>]*\bclass=[\"'][^\"']*(?:result__a|result-link)[^\"']*[\"'][^>]*)>(.*?)</a>",
        flags=re.IGNORECASE | re.DOTALL,
    )
    for attrs, title in anchor.findall(body):
        href_match = re.search(r"\bhref=[\"']([^\"']+)[\"']", attrs, flags=re.IGNORECASE)
        if href_match:
            add(href_match.group(1), title)
        if len(found) >= limit:
            break
    return found[:limit]


def extract_page_facts(
    text: str,
    *,
    title: str,
    url: str,
    record: ProfessorRecord,
) -> dict[str, object]:
    """Copy facts that are written on the page. Do not build an email address."""

    haystack = "\n".join(part for part in (title, text) if part)
    department = _earliest_unit(haystack)
    faculty = None
    city_match = _CITY_RE.search(haystack)
    city = city_match.group(1).strip(" .") if city_match else None
    interests = _explicit_interests(haystack, record)
    papers = [paper.title for paper in record.papers if paper.title and _contains(haystack, paper.title)]
    emails = []
    for match in _EMAIL_RE.finditer(haystack):
        parsed = published_email(match.group(0).strip(".,;:<>()[]"))
        if parsed and parsed not in emails:
            emails.append(parsed)
    name = matched_display_name(haystack, record.name) if record.name else None
    return {
        "name": name,
        "university": _explicit_university(haystack, record.university),
        "department": department,
        "faculty": faculty,
        "city": city,
        "email": emails[0] if emails else None,
        "research_interests": interests,
        "paper_titles": papers,
        "profile_url": url if reject_reason(url) is None else None,
    }


def judge_fetched_text(record: ProfessorRecord, *, url: str, title: str, text: str) -> dict[str, object]:
    """Run extraction and the existing identity check on text that was already fetched."""

    facts = extract_page_facts(text, title=title, url=url, record=record)
    verified = verify_professor(
        record,
        CatalogUniversitySource(
            pages=[_page_from_facts(facts, url)],
            assignments=[(record.name, record.university, [url])],
        ),
    )
    return {
        "facts": facts,
        "verification_status": verified.verification_status,
        "notes": verified.verification_notes,
        "official_university": verified.official_university,
        "official_email": verified.official_email,
    }


def select_pilot_candidates(
    records: list[ProfessorRecord],
    *,
    limit: int = 5,
) -> list[ProfessorRecord]:
    """Take the first records in stored order. The order does not change."""

    if limit < 1:
        return []
    return list(records[:limit])


def run_web_discovery(
    *,
    records: list[ProfessorRecord] | None = None,
    settings: WebDiscoverySettings | None = None,
    search_provider: SearchProvider | None = None,
    fetcher: OfficialPageFetcher | None = None,
    output_path: Path | None = None,
    professors_path: Path | None = None,
    sleeper=time.sleep,
) -> dict:
    """Search and fetch for a few existing candidates. The professor file is read only."""

    if _VERIFICATION_SENDS_EMAIL or SENDS_EMAIL:
        raise RuntimeError("Web discovery must not send email.")
    options = settings or WebDiscoverySettings()
    stored_path = professors_path or get_professors_path()
    before = _file_sha256(stored_path)
    chosen_source = records if records is not None else ProfessorStore(stored_path).load()
    chosen = select_pilot_candidates(chosen_source, limit=options.max_candidates_per_run)
    provider = search_provider or PublicWebSearchProvider(timeout=options.request_timeout_seconds)
    page_fetcher = fetcher or OfficialPageFetcher(timeout=options.request_timeout_seconds)
    destination = output_path or (get_data_dir() / "verification_candidates.json")
    processed = []
    for index, record in enumerate(chosen):
        if index:
            sleeper(options.request_delay_seconds)
        processed.append(
            _review_candidate(
                record,
                settings=options,
                search_provider=provider,
                fetcher=page_fetcher,
                sleeper=sleeper,
            )
        )
        _write_review(destination, _report(processed, options, before, stored_path))
    report = _report(processed, options, before, stored_path)
    after = _file_sha256(stored_path)
    report["professors_json_sha256_after"] = after
    report["professors_json_modified"] = bool(before and after and before != after)
    if report["professors_json_modified"]:
        raise RuntimeError("data/professors.json changed during web discovery.")
    _write_review(destination, report)
    return report


def main() -> int:
    """Run the live pilot. This uses the public search provider, not a fixture."""

    print("Official university web-discovery pilot")
    print("Search provider: public DuckDuckGo HTML, then Bing RSS if needed.")
    print("data/professors.json is read only. Results go to data/verification_candidates.json.")
    report = run_web_discovery()
    summary = report["summary"]
    print(f"Candidates processed: {summary['candidates_processed']}/{report['max_candidates_per_run']}")
    print(f"Search results found: {summary['search_results_found']}")
    print(f"Official university URLs found: {summary['official_university_urls_found']}")
    print(f"Pages successfully fetched: {summary['pages_fetched']}")
    print(f"Pages blocked/failed: {summary['pages_failed']}")
    print(f"Identity matches: {summary['identity_matches']}")
    print(f"Potential official verifications: {summary['potential_official_verifications']}")
    print(f"Main professors.json modified: {'YES' if report['professors_json_modified'] else 'NO'}")
    print("Emails sent: NO")
    shown = 0
    for candidate in report["candidates"]:
        for url in candidate["official_candidate_urls"]:
            print(f"Official URL: {url}")
            shown += 1
            if shown >= 8:
                break
        if shown >= 8:
            break
        for item in candidate["search_results"][:2]:
            if shown >= 8:
                break
            print(f"Search result: {item['url']}")
            shown += 1
    if summary["search_results_found"] == 0:
        print(
            "No search links were returned. The public DuckDuckGo HTML and Bing RSS endpoints "
            "did not provide results from this environment. A replacement SearchProvider can use "
            "the Bing Web Search API or Google Programmable Search with an API key."
        )
    return 0


def _review_candidate(
    record: ProfessorRecord,
    *,
    settings: WebDiscoverySettings,
    search_provider: SearchProvider,
    fetcher: OfficialPageFetcher,
    sleeper,
) -> dict:
    queries = build_search_queries(
        record.name,
        record.university,
        record.department,
        record.research_interests,
    )
    search_results: list[dict] = []
    search_errors: list[str] = []
    official_urls: list[str] = []
    rejected: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for query_index, query in enumerate(queries):
        if query_index:
            sleeper(settings.request_delay_seconds)
        response = search_provider.search(query, limit=settings.max_results_per_query)
        if response.error:
            search_errors.append(response.error)
        for item in response.results:
            search_results.append(
                {
                    "query": item.query,
                    "title": item.title,
                    "url": item.url,
                    "snippet": item.snippet,
                    "engine": item.engine or response.engine,
                }
            )
            key = item.url.rstrip("/").casefold()
            if key in seen_urls:
                continue
            seen_urls.add(key)
            reason = reject_reason(item.url)
            if reason:
                rejected.append({"url": item.url, "reason": reason})
            else:
                official_urls.append(item.url)

    fetches = []
    pages: list[OfficialPage] = []
    for url_index, url in enumerate(official_urls[: settings.max_urls_per_candidate]):
        if url_index:
            sleeper(settings.request_delay_seconds)
        fetched = fetcher.fetch(url)
        facts = None
        usable = _usable_fetch(fetched)
        if usable:
            facts = extract_page_facts(
                fetched.text,
                title=fetched.title,
                url=fetched.final_url or fetched.requested_url,
                record=record,
            )
            pages.append(_page_from_facts(facts, fetched.final_url or url))
        fetches.append(_fetch_payload(fetched, facts))

    identity = {
        "accepted": False,
        "signals": [],
        "notes": "",
        "verification_status": "unverified",
    }
    if pages:
        verified = verify_professor(
            record,
            CatalogUniversitySource(
                pages=pages,
                assignments=[(record.name, record.university, [page.url for page in pages])],
            ),
        )
        identity = {
            "accepted": verified.verification_status == "officially_verified",
            "signals": _signals_from_notes(verified.verification_notes),
            "notes": verified.verification_notes,
            "verification_status": verified.verification_status,
            "official_university": verified.official_university,
            "official_profile_url": verified.official_profile_url,
            "official_email": verified.official_email,
            "official_department": verified.official_department,
            "official_city": verified.official_city,
            "verified_at": verified.verified_at,
        }
        decision = verified.verification_status
    elif official_urls:
        decision = "official_pages_not_read"
    elif search_results:
        decision = "no_official_url"
    else:
        decision = "no_search_results"

    return {
        "candidate_name": record.name,
        "candidate_university": record.university,
        "department": record.department,
        "research_areas": list(record.research_interests),
        "search_queries": queries,
        "search_results": search_results,
        "search_errors": search_errors,
        "official_candidate_urls": official_urls,
        "rejected_urls": rejected,
        "fetches": fetches,
        "identity_match": identity,
        "verification_decision": decision,
        "source_urls": list(record.source_urls),
        "errors": search_errors,
    }


def _page_from_facts(facts: dict[str, object], url: str) -> OfficialPage:
    department = facts.get("department") or facts.get("faculty")
    interests = facts.get("research_interests")
    papers = facts.get("paper_titles")
    return OfficialPage(
        url=url,
        name=_optional_text(facts.get("name")),
        university=_optional_text(facts.get("university")),
        department=_optional_text(department),
        city=_optional_text(facts.get("city")),
        email=_optional_text(facts.get("email")),
        research_areas=list(interests) if isinstance(interests, list) else [],
        paper_titles=list(papers) if isinstance(papers, list) else [],
    )


def _fetch_payload(fetched: FetchedPage, facts: dict[str, object] | None) -> dict:
    excerpt = fetched.text[:_TEXT_EXCERPT]
    if len(fetched.text) > _TEXT_EXCERPT:
        excerpt += "\n[truncated]"
    return {
        "requested_url": fetched.requested_url,
        "final_url": fetched.final_url or None,
        "status_code": fetched.status_code,
        "title": fetched.title or None,
        "text_excerpt": excerpt or None,
        "extracted": facts,
        "blocked": fetched.blocked,
        "error": fetched.error,
    }


def _usable_fetch(fetched: FetchedPage) -> bool:
    if fetched.error or fetched.blocked or not fetched.text.strip():
        return False
    final_url = fetched.final_url or fetched.requested_url
    return reject_reason(final_url) is None


def _explicit_university(text: str, candidate_university: str) -> str | None:
    found: list[str] = []
    if candidate_university and _contains(text, candidate_university):
        found.append(candidate_university.strip())
    for pattern in (_UNIVERSITY_RE, _CHINESE_UNIVERSITY_RE):
        for match in pattern.finditer(text):
            value = " ".join(match.group(0).split())
            if value and value not in found:
                found.append(value)
    if not found:
        return None
    others = [item for item in found if not _same(item, candidate_university)]
    if candidate_university and _contains(text, candidate_university) and not others:
        return candidate_university.strip()
    if candidate_university and _contains(text, candidate_university):
        return candidate_university.strip()
    return others[0] if others else found[0]


def _explicit_interests(text: str, record: ProfessorRecord) -> list[str]:
    found: list[str] = []
    for area in record.research_interests:
        if area and _contains(text, area) and area not in found:
            found.append(area)
    labeled = _INTEREST_RE.search(text)
    if labeled:
        for part in re.split(r"[,;]", labeled.group(1)):
            cleaned = part.strip(" .")
            if cleaned and cleaned not in found:
                found.append(cleaned)
    return found


def _report(
    processed: list[dict],
    settings: WebDiscoverySettings,
    before_hash: str | None,
    professors_path: Path,
) -> dict:
    search_results = sum(len(item["search_results"]) for item in processed)
    official_urls = sum(len(item["official_candidate_urls"]) for item in processed)
    fetched = 0
    failed = 0
    for item in processed:
        for page in item["fetches"]:
            if page["error"] or page["blocked"] or not page["text_excerpt"]:
                failed += 1
            else:
                fetched += 1
    identity_matches = sum(1 for item in processed if item["identity_match"]["accepted"])
    return {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "purpose": "Review file for the official-page pilot. The professor database was not updated.",
        "max_candidates_per_run": settings.max_candidates_per_run,
        "max_urls_per_candidate": settings.max_urls_per_candidate,
        "request_timeout_seconds": settings.request_timeout_seconds,
        "request_delay_seconds": settings.request_delay_seconds,
        "emails_sent": False,
        "professors_json": str(professors_path),
        "professors_json_sha256_before": before_hash,
        "professors_json_modified": False,
        "candidates": processed,
        "summary": {
            "candidates_processed": len(processed),
            "search_results_found": search_results,
            "official_university_urls_found": official_urls,
            "pages_fetched": fetched,
            "pages_failed": failed,
            "identity_matches": identity_matches,
            "potential_official_verifications": identity_matches,
        },
    }


def _write_review(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def _file_sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _signals_from_notes(notes: str) -> list[str]:
    match = re.search(r"Identity signals: ([^.]+)\.", notes)
    if not match:
        return []
    return [part.strip() for part in match.group(1).split(",") if part.strip()]


def _result_target(href: str) -> str | None:
    cleaned = html.unescape(href.strip())
    if cleaned.startswith("//"):
        cleaned = "https:" + cleaned
    parsed = urllib.parse.urlparse(cleaned)
    host = (parsed.hostname or "").casefold()
    if host.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        target = urllib.parse.parse_qs(parsed.query).get("uddg", [""])[0]
        target = urllib.parse.unquote(target)
        if "%" in target:
            target = urllib.parse.unquote(target)
        cleaned = target
        parsed = urllib.parse.urlparse(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    return urllib.parse.urlunparse(parsed)


def _first_tag(item: str, tag: str) -> str:
    match = re.search(
        rf"<{tag}\b[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{tag}>",
        item,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return html.unescape(match.group(1)).strip() if match else ""


def _clean_markup(value: str) -> str:
    text = re.sub(r"<[^>]+>", " ", value or "")
    return " ".join(html.unescape(text).split())


def _earliest_unit(text: str) -> str | None:
    """Use the first school or department label, and do not continue into the next sentence."""

    found: list[tuple[int, str]] = []
    for pattern in (_DEPARTMENT_RE, _FACULTY_RE, _CHINESE_UNIT_RE):
        match = pattern.search(text)
        if match:
            found.append((match.start(), " ".join(match.group(0 if pattern is _CHINESE_UNIT_RE else 1).split())))
    if not found:
        return None
    found.sort(key=lambda item: item[0])
    return found[0][1]


def _first_group(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    if not match:
        return None
    return " ".join(match.group(1).split())


def _contains(haystack: str, needle: str) -> bool:
    if not needle or not needle.strip():
        return False
    return _compact(needle) in _compact(haystack)


def _same(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return _compact(left) == _compact(right)


def _compact(value: str) -> str:
    return " ".join(value.casefold().split())


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


if __name__ == "__main__":
    raise SystemExit(main())

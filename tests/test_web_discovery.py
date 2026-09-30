"""Tests for web discovery and page fetching.

Fixtures stand in for search engines and HTTP. They are not saved as professors
and they do not contact a mail server.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import urllib.error
from email.message import Message
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_professors_path
from app.models import PaperRecord, ProfessorRecord
from app.web_discovery import (
    SENDS_EMAIL,
    PublicWebSearchProvider,
    SearchResponse,
    WebDiscoverySettings,
    build_search_queries,
    extract_page_facts,
    judge_fetched_text,
    reject_reason,
    run_web_discovery,
    select_pilot_candidates,
)
from app.web_fetcher import OfficialPageFetcher

OFFICIAL_URL = "https://cs.example.edu.cn/people/test"
AC_URL = "https://ict.example.ac.cn/people/test"
OPENALEX_URL = "https://openalex.org/A1"
DUCKDUCKGO_PAGE = """
<html><body>
<a class="result__a" href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fcs.example.edu.cn%2Fpeople%2Ftest&amp;rut=1">Example profile</a>
<a class="result__a" href="https://www.researchgate.net/profile/test">Directory</a>
</body></html>
"""


class _Body:
    def __init__(self, body: bytes, status: int = 200, url: str = OFFICIAL_URL) -> None:
        self._body = body
        self.status = status
        self._url = url
        self.headers = {"Content-Type": "text/html; charset=utf-8"}

    def read(self, limit: int = -1) -> bytes:
        if limit is None or limit < 0:
            return self._body
        return self._body[:limit]

    def geturl(self) -> str:
        return self._url

    def __enter__(self) -> "_Body":
        return self

    def __exit__(self, *args: object) -> bool:
        return False


class _FixtureSearch:
    name = "fixture"

    def __init__(self, results: list[tuple[str, str]] | None = None, error: str | None = None) -> None:
        self.results = results or []
        self.error = error
        self.queries: list[str] = []

    def search(self, query: str, *, limit: int = 5) -> SearchResponse:
        self.queries.append(query)
        from app.web_discovery import SearchResult

        hits = [
            SearchResult(query=query, title=title, url=url, engine="fixture")
            for title, url in self.results[:limit]
        ]
        return SearchResponse(query=query, results=hits, error=self.error, engine="fixture")


def _record(**kwargs: object) -> ProfessorRecord:
    data = {
        "name": "Test Professor",
        "university": "Test University",
        "country": "China",
        "public_email": None,
        "city": None,
        "department": None,
        "source_urls": [OPENALEX_URL],
    }
    data.update(kwargs)
    return ProfessorRecord(**data)  # type: ignore[arg-type]


class WebDiscoveryTests(unittest.TestCase):
    def test_web_search_provider_reads_links_from_the_response(self) -> None:
        def opener(request, timeout: float) -> _Body:
            return _Body(DUCKDUCKGO_PAGE.encode("utf-8"), url="https://html.duckduckgo.com/html/")

        response = PublicWebSearchProvider(opener=opener).search('"Test Professor" "Test University"', limit=5)

        self.assertEqual(response.engine, "duckduckgo-html")
        self.assertEqual(
            [item.url for item in response.results],
            [OFFICIAL_URL, "https://www.researchgate.net/profile/test"],
        )

    def test_query_includes_professor_name(self) -> None:
        queries = build_search_queries("Test Professor", "Test University")

        self.assertTrue(queries)
        self.assertTrue(all("Test Professor" in query for query in queries))

    def test_query_includes_university(self) -> None:
        queries = build_search_queries("Test Professor", "Test University")

        self.assertTrue(all("Test University" in query for query in queries))
        self.assertIn("site:edu.cn", queries[1])
        self.assertIn("site:ac.cn", queries[2])

    def test_official_edu_cn_url_is_accepted(self) -> None:
        self.assertIsNone(reject_reason(OFFICIAL_URL))

    def test_official_ac_cn_url_is_accepted(self) -> None:
        self.assertIsNone(reject_reason(AC_URL))

    def test_third_party_url_is_rejected(self) -> None:
        for url in (
            "https://www.researchgate.net/profile/test",
            "https://scholar.google.com/citations?user=abc",
            "https://www.linkedin.com/in/test",
            "https://example.com/blog/professor",
        ):
            self.assertIsNotNone(reject_reason(url))

    def test_search_results_are_stored(self) -> None:
        def opener(request, timeout: float) -> _Body:
            if str(request.full_url).endswith("/robots.txt"):
                raise urllib.error.HTTPError(request.full_url, 404, "missing", Message(), None)
            return _Body(b"<html><head><title></title></head><body></body></html>")

        search = _FixtureSearch(
            [
                ("Official page", OFFICIAL_URL),
                ("Directory", "https://www.researchgate.net/profile/test"),
            ]
        )
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "verification_candidates.json"
            report = run_web_discovery(
                records=[_record()],
                settings=WebDiscoverySettings(request_delay_seconds=0),
                search_provider=search,
                fetcher=OfficialPageFetcher(opener=opener),
                output_path=output,
                professors_path=get_professors_path(),
                sleeper=lambda seconds: None,
            )
            stored = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(stored["summary"]["search_results_found"], report["summary"]["search_results_found"])
        self.assertIn(OFFICIAL_URL, stored["candidates"][0]["official_candidate_urls"])
        self.assertIn(OFFICIAL_URL, [item["url"] for item in stored["candidates"][0]["search_results"]])
        self.assertGreater(len(search.queries), 0)

    def test_page_fetch_success(self) -> None:
        html = b"<html><head><title>Lab Page</title></head><body><p>Hello professor</p></body></html>"

        def opener(request, timeout: float) -> _Body:
            if str(request.full_url).endswith("/robots.txt"):
                raise urllib.error.HTTPError(request.full_url, 404, "missing", Message(), None)
            return _Body(html, url="https://cs.example.edu.cn/people/test")

        fetched = OfficialPageFetcher(opener=opener).fetch(OFFICIAL_URL)

        self.assertIsNone(fetched.error)
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.final_url, OFFICIAL_URL)
        self.assertEqual(fetched.title, "Lab Page")
        self.assertIn("Hello professor", fetched.text)

    def test_timeout_is_handled(self) -> None:
        def opener(request, timeout: float) -> _Body:
            if str(request.full_url).endswith("/robots.txt"):
                raise urllib.error.HTTPError(request.full_url, 404, "missing", Message(), None)
            raise TimeoutError("timed out")

        fetched = OfficialPageFetcher(opener=opener).fetch(OFFICIAL_URL)

        self.assertIsNone(fetched.status_code)
        self.assertIn("timed out", (fetched.error or "").casefold())

    def test_http_error_is_handled(self) -> None:
        def opener(request, timeout: float) -> _Body:
            if str(request.full_url).endswith("/robots.txt"):
                raise urllib.error.HTTPError(request.full_url, 404, "missing", Message(), None)
            raise urllib.error.HTTPError(request.full_url, 503, "unavailable", Message(), None)

        fetched = OfficialPageFetcher(opener=opener).fetch(OFFICIAL_URL)

        self.assertEqual(fetched.status_code, 503)
        self.assertIn("503", fetched.error or "")

    def test_empty_page_is_handled(self) -> None:
        def opener(request, timeout: float) -> _Body:
            if str(request.full_url).endswith("/robots.txt"):
                raise urllib.error.HTTPError(request.full_url, 404, "missing", Message(), None)
            return _Body(b"<html><head><title></title></head><body></body></html>")

        fetched = OfficialPageFetcher(opener=opener).fetch(OFFICIAL_URL)

        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.text, "")
        self.assertIn("empty", (fetched.error or "").casefold())

    def test_department_stops_before_the_following_sentence(self) -> None:
        facts = extract_page_facts(
            "Cui, Bin is a professor in the School of Computer Science, and has served as the Director.\n"
            "Earlier: Department of Chemistry, Capital Normal University.",
            title="Cui, Bin",
            url=OFFICIAL_URL,
            record=_record(name="Bin Cui", university="Peking University"),
        )

        self.assertEqual(facts["department"], "School of Computer Science")

    def test_email_is_extracted_only_when_present(self) -> None:
        present = extract_page_facts(
            "Test Professor, Test University. Email: public@example.edu.cn",
            title="Profile",
            url=OFFICIAL_URL,
            record=_record(),
        )
        absent = extract_page_facts(
            "Test Professor, Test University.",
            title="Profile",
            url=OFFICIAL_URL,
            record=_record(),
        )

        self.assertEqual(present["email"], "public@example.edu.cn")
        self.assertIsNone(absent["email"])

    def test_email_is_never_guessed(self) -> None:
        facts = extract_page_facts(
            "Ann Lee works at Test University.",
            title="Profile",
            url=OFFICIAL_URL,
            record=_record(name="Ann Lee"),
        )

        self.assertIsNone(facts["email"])
        self.assertNotIn("@", json.dumps({"email": facts["email"]}))

    def test_name_only_match_is_rejected(self) -> None:
        judged = judge_fetched_text(
            _record(),
            url=OFFICIAL_URL,
            title="Profile",
            text="Test Professor teaches a seminar.",
        )

        self.assertNotEqual(judged["verification_status"], "officially_verified")
        self.assertIn("Name alone", judged["notes"])

    def test_name_and_university_match_is_accepted(self) -> None:
        judged = judge_fetched_text(
            _record(),
            url=OFFICIAL_URL,
            title="Profile",
            text="Test Professor works at Test University.",
        )

        self.assertEqual(judged["verification_status"], "officially_verified")
        self.assertIn("university", judged["notes"])
        self.assertEqual(judged["official_university"], "Test University")

    def test_university_conflict_is_detected(self) -> None:
        record = _record(
            papers=[PaperRecord(title="Database Systems for Records", year=2020, abstract=None)]
        )
        judged = judge_fetched_text(
            record,
            url=OFFICIAL_URL,
            title="Profile",
            text="Test Professor published Database Systems for Records at Other University.",
        )

        self.assertIn("conflict", judged["notes"].casefold())
        self.assertEqual(judged["official_university"], "Other University")
        self.assertEqual(record.university, "Test University")
        self.assertEqual(record.verification_status, "unverified")

    def test_max_candidates_per_run_is_five(self) -> None:
        records = [
            _record(name=f"Test Professor {index}", university=f"Test University {index}")
            for index in range(6)
        ]
        self.assertEqual(WebDiscoverySettings().max_candidates_per_run, 5)
        self.assertEqual(
            [item.name for item in select_pilot_candidates(records, limit=5)],
            [f"Test Professor {index}" for index in range(5)],
        )
        with tempfile.TemporaryDirectory() as folder:
            report = run_web_discovery(
                records=records,
                settings=WebDiscoverySettings(max_candidates_per_run=5, request_delay_seconds=0),
                search_provider=_FixtureSearch(),
                output_path=Path(folder) / "review.json",
                professors_path=get_professors_path(),
                sleeper=lambda seconds: None,
            )

        self.assertEqual(report["summary"]["candidates_processed"], 5)
        self.assertEqual(report["candidates"][0]["candidate_name"], "Test Professor 0")
        self.assertEqual(report["candidates"][4]["candidate_name"], "Test Professor 4")

    def test_professors_json_is_not_modified(self) -> None:
        def opener(request, timeout: float) -> _Body:
            if str(request.full_url).endswith("/robots.txt"):
                raise urllib.error.HTTPError(request.full_url, 404, "missing", Message(), None)
            return _Body(b"<html><head><title></title></head><body></body></html>")

        path = get_professors_path()
        before = path.read_bytes()
        with tempfile.TemporaryDirectory() as folder:
            run_web_discovery(
                records=[_record()],
                settings=WebDiscoverySettings(request_delay_seconds=0),
                search_provider=_FixtureSearch(results=[("Official page", OFFICIAL_URL)]),
                fetcher=OfficialPageFetcher(opener=opener),
                output_path=Path(folder) / "review.json",
                professors_path=path,
                sleeper=lambda seconds: None,
            )

        self.assertEqual(path.read_bytes(), before)

    def test_no_email_is_sent(self) -> None:
        for relative in ("app/web_discovery.py", "app/web_fetcher.py"):
            text = (ROOT / relative).read_text(encoding="utf-8").casefold()
            self.assertNotIn("smtplib", text)
            self.assertNotIn("sendmail", text)
            self.assertNotIn("mailto:", text)
        self.assertFalse(SENDS_EMAIL)


if __name__ == "__main__":
    unittest.main()

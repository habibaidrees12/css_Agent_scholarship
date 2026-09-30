"""Tests for configured search, official-page research, and unsent drafts.

Fixtures stand in for the search API and for HTTP. They are not saved as real
professors, and no message is sent.
"""

from __future__ import annotations

import ast
import json
import sys
import tempfile
import unittest
import urllib.error
from datetime import date
from email.message import Message
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.agent_pipeline import (
    PipelineLimits,
    build_targeted_queries,
    distinctive_tokens,
    domain_matches,
    official_domain_from_url,
    page_supports_university,
    publish_review,
    qualifies_for_shortlist,
    research_and_draft,
    resolve_university_domain,
    run_pipeline,
)
from app.main import parse_args
from app.models import MatchResult, PaperRecord, ProfessorRecord, UserProfile
from app.outreach import (
    SENDS_EMAIL,
    TRACKER_STATUSES,
    approve_draft,
    draft_csc_email,
    medscribe_relevance,
    recent_papers,
    tracker_entry,
)
from app.profile import load_profile
from app.research import evaluate_professor, load_search_config
from app.search_api import (
    NO_SEARCH_API,
    BingSearchProvider,
    GoogleSearchProvider,
    MemorySearchCache,
    build_search_provider,
    read_search_settings,
)
from app.sources import OpenAlexSource
from app.storage import ProfessorStore, deduplicate_records, is_same_professor
from app.web_discovery import SearchResponse, SearchResult, extract_page_facts
from app.web_fetcher import OfficialPageFetcher

OFFICIAL_HOME = "https://www.example.edu.cn/"
OFFICIAL_PROFILE = "https://cs.example.edu.cn/people/test"
OPENALEX_URL = "https://openalex.org/A1"


class _Body:
    def __init__(self, body: bytes, status: int = 200, url: str = OFFICIAL_PROFILE) -> None:
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

    def __init__(self, homepage: str = OFFICIAL_HOME, profile: str = OFFICIAL_PROFILE) -> None:
        self.homepage = homepage
        self.profile = profile
        self.queries: list[str] = []

    def search(self, query: str, *, limit: int = 5) -> SearchResponse:
        self.queries.append(query)
        if "professor" in query or "faculty" in query or "email" in query or "Database Systems" in query:
            url = self.profile
            title = "Test Professor faculty page"
        else:
            url = self.homepage
            title = "Test University"
        hit = SearchResult(query=query, title=title, url=url, snippet="Test University", engine=self.name)
        return SearchResponse(query=query, results=[hit][:limit], engine=self.name)


def _html(body: str) -> bytes:
    return f"<html><head><title>Test Professor</title></head><body>{body}</body></html>".encode("utf-8")


def _opener(request, timeout: float) -> _Body:
    url = str(request.full_url)
    if url.endswith("/robots.txt"):
        raise urllib.error.HTTPError(url, 404, "missing", Message(), None)
    if url.rstrip("/") == "https://www.example.edu.cn":
        page = _html("Test University official website.")
        return _Body(page, url=OFFICIAL_HOME)
    if "captcha" in url:
        return _Body(_html("captcha"), url=url)
    page = _html(
        "Test Professor works at Test University. "
        "Research interests: Database Systems. "
        "Email: public@example.edu.cn"
    )
    return _Body(page, url=OFFICIAL_PROFILE)


def _record(**kwargs: object) -> ProfessorRecord:
    data = {
        "name": "Test Professor",
        "university": "Test University",
        "country": "China",
        "public_email": None,
        "city": None,
        "department": None,
        "research_interests": ["Database Systems"],
        "source_urls": [OPENALEX_URL],
        "papers": [
            PaperRecord(
                title="Database Systems for Records",
                year=date.today().year,
                abstract=None,
                keywords=["database systems"],
                source_url=OPENALEX_URL,
            )
        ],
    }
    data.update(kwargs)
    return ProfessorRecord(**data)  # type: ignore[arg-type]


class SearchApiTests(unittest.TestCase):
    def test_bing_settings_select_bing(self) -> None:
        settings = read_search_settings({"BING_SEARCH_API_KEY": "test-key"})
        provider = build_search_provider(settings, opener=lambda request, timeout: None)

        self.assertEqual(settings.selected_name(), "bing")
        self.assertIsInstance(provider, BingSearchProvider)
        self.assertEqual(settings.status_message(), "Bing Web Search API is configured.")

    def test_google_settings_need_a_key_and_engine_id(self) -> None:
        incomplete = read_search_settings({"GOOGLE_SEARCH_API_KEY": "test-key"})
        complete = read_search_settings(
            {"GOOGLE_SEARCH_API_KEY": "test-key", "GOOGLE_SEARCH_ENGINE_ID": "engine"}
        )

        self.assertIsNone(build_search_provider(incomplete))
        self.assertIsInstance(build_search_provider(complete), GoogleSearchProvider)
        self.assertEqual(incomplete.status_message(), NO_SEARCH_API)

    def test_missing_api_key_is_reported(self) -> None:
        report = run_pipeline(records=[_record()], use_configured_provider=False, write_outputs=False)

        self.assertFalse(report.api_configured)
        self.assertEqual(report.message, NO_SEARCH_API)
        self.assertEqual(report.candidates_processed, 0)
        self.assertEqual(report.emails_sent, 0)

    def test_bing_parser_reads_real_result_fields(self) -> None:
        payload = json.dumps(
            {"webPages": {"value": [{"name": "Faculty", "url": OFFICIAL_PROFILE, "snippet": "Test University"}]}}
        ).encode("utf-8")

        def opener(request, timeout: float) -> _Body:
            names = [name.casefold() for name, _value in request.header_items()]
            self.assertIn("ocp-apim-subscription-key", names)
            return _Body(payload, url="https://api.bing.microsoft.com/v7.0/search")

        response = BingSearchProvider("test-key", opener=opener).search("query", limit=5)

        self.assertEqual(response.results[0].url, OFFICIAL_PROFILE)
        self.assertEqual(response.results[0].title, "Faculty")

    def test_google_parser_reads_real_result_fields(self) -> None:
        payload = json.dumps(
            {"items": [{"title": "Faculty", "link": OFFICIAL_PROFILE, "snippet": "Test University"}]}
        ).encode("utf-8")

        def opener(request, timeout: float) -> _Body:
            return _Body(payload, url="https://www.googleapis.com/customsearch/v1")

        response = GoogleSearchProvider("test-key", "engine", opener=opener).search("query", limit=5)

        self.assertEqual(response.results[0].url, OFFICIAL_PROFILE)


class DomainAndQueryTests(unittest.TestCase):
    def test_university_domain_comes_from_a_fetched_page(self) -> None:
        domain = resolve_university_domain(
            "Test University",
            _FixtureSearch(),
            OfficialPageFetcher(opener=_opener, check_robots=True),
        )

        self.assertEqual(domain.official_domain, "example.edu.cn")
        self.assertEqual(domain.domain_source, OFFICIAL_HOME)
        self.assertEqual(domain.domain_confidence, "high")

    def test_domain_is_not_guessed_from_the_university_name(self) -> None:
        domain = resolve_university_domain(
            "Beijing Institute of Technology",
            _FixtureSearch(homepage="https://example.com/not-official"),
            OfficialPageFetcher(opener=_opener),
        )

        self.assertIsNone(domain.official_domain)
        self.assertEqual(domain.domain_confidence, "none")

    def test_official_domain_filter_rejects_other_hosts(self) -> None:
        self.assertTrue(domain_matches(OFFICIAL_PROFILE, "example.edu.cn"))
        self.assertTrue(domain_matches("https://pure.bit.edu.cn/en/persons/dinghua-li", "english.bit.edu.cn"))
        self.assertFalse(domain_matches("https://english.pku.edu.cn/", "english.bit.edu.cn"))
        self.assertFalse(domain_matches("https://faculty.cau.edu.cn/ljh_en_6333", "english.scau.edu.cn"))
        self.assertFalse(domain_matches("https://www.researchgate.net/profile/test", "example.edu.cn"))
        self.assertIsNone(official_domain_from_url("https://scholar.google.com/citations?user=abc"))
        self.assertEqual(official_domain_from_url(OFFICIAL_HOME), "example.edu.cn")

    def test_targeted_queries_use_name_university_and_domain(self) -> None:
        queries = build_targeted_queries(
            "Test Professor",
            "Test University",
            "Department of Computer Science",
            ["Database Systems", "Computer Science"],
            "example.edu.cn",
            limit=5,
        )

        self.assertTrue(all("Test Professor" in query and "Test University" in query for query in queries))
        self.assertTrue(any("faculty" in query for query in queries))
        self.assertTrue(any("site:example.edu.cn" in query for query in queries))
        self.assertTrue(any("Database Systems" in query for query in queries))
        self.assertTrue(any(query.endswith("email") for query in queries))
        self.assertLessEqual(len(queries), 5)

    def test_page_must_mention_the_university(self) -> None:
        self.assertTrue(page_supports_university("Welcome to Test University", "Test University"))
        self.assertFalse(page_supports_university("Unrelated college", "Test University"))
        self.assertIn("Test", distinctive_tokens("Test University"))


class ResearchPipelineTests(unittest.TestCase):
    def test_pipeline_verifies_extracts_email_and_does_not_send(self) -> None:
        search = _FixtureSearch()
        record = _record()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            report = run_pipeline(
                records=[record],
                provider=search,
                fetcher=OfficialPageFetcher(opener=_opener),
                limits=PipelineLimits(max_candidates_per_run=10),
                output_dir=path,
                cache=MemorySearchCache(),
                use_configured_provider=False,
                store=ProfessorStore(path / "professors.json"),
            )
            shortlist = json.loads((path / "professor_shortlist.json").read_text(encoding="utf-8"))
            drafts = json.loads((path / "email_drafts.json").read_text(encoding="utf-8"))
            tracker = json.loads((path / "application_tracker.json").read_text(encoding="utf-8"))

        self.assertEqual(report.emails_sent, 0)
        self.assertEqual(report.candidates_processed, 1)
        self.assertEqual(report.verified_professors, 1)
        self.assertEqual(report.official_profiles_found, 1)
        self.assertEqual(report.public_academic_emails_found, 1)
        self.assertEqual(report.email_drafts_generated, 1)
        self.assertEqual(shortlist[0]["public_email"], "public@example.edu.cn")
        self.assertEqual(shortlist[0]["verification_status"], "officially_verified")
        self.assertIn("Database Systems", shortlist[0]["matched_areas"])
        self.assertTrue(shortlist[0]["evidence"])
        self.assertEqual(drafts[0]["draft_status"], "draft")
        self.assertEqual(drafts[0]["recipient_email"], "public@example.edu.cn")
        self.assertEqual(tracker[0]["email_status"], "draft_ready")
        self.assertIsNone(tracker[0]["date_contacted"])
        self.assertGreater(len(search.queries), 1)

    def test_repeated_search_uses_the_cache(self) -> None:
        search = _FixtureSearch()
        cache = MemorySearchCache()
        fetcher = OfficialPageFetcher(opener=_opener)
        resolve_university_domain("Test University", search, fetcher, cache=cache)
        first = len(search.queries)
        resolve_university_domain("Test University", search, fetcher, cache=cache)

        self.assertGreater(first, 0)
        self.assertEqual(len(search.queries), first)

    def test_page_fetch_reports_a_block_without_bypass(self) -> None:
        def opener(request, timeout: float) -> _Body:
            url = str(request.full_url)
            if url.endswith("/robots.txt"):
                raise urllib.error.HTTPError(url, 404, "missing", Message(), None)
            return _Body(_html("Please complete the captcha"), url=OFFICIAL_PROFILE)

        fetched = OfficialPageFetcher(opener=opener).fetch(OFFICIAL_PROFILE)

        self.assertTrue(fetched.blocked)
        self.assertIn("anti-bot", fetched.error or "")

    def test_email_is_taken_only_from_page_text(self) -> None:
        facts = extract_page_facts(
            "Test Professor, Test University. No address is published.",
            title="Faculty",
            url=OFFICIAL_PROFILE,
            record=_record(),
        )

        self.assertIsNone(facts["email"])

    def test_generic_computer_science_is_not_a_strong_match(self) -> None:
        profile = load_profile()
        config = load_search_config()
        generic = _record(name="Generic Professor", research_interests=["Computer science"], papers=[])
        result = evaluate_professor(profile, config, generic)
        generic.verification_status = "officially_verified"
        generic.official_profile_url = OFFICIAL_PROFILE
        generic.official_source = "official_professor_or_lab_page"

        self.assertEqual(result.research_score, 0)
        self.assertEqual(result.research_confidence, "low")
        self.assertFalse(qualifies_for_shortlist(generic, result))

    def test_recent_paper_keeps_a_missing_abstract_null(self) -> None:
        current = _record()
        old = _record(
            papers=[
                PaperRecord(title="Old Database Systems Notes", year=date.today().year - 8, abstract=None)
            ]
        )

        self.assertEqual(recent_papers(current)[0]["abstract"], None)
        self.assertEqual(recent_papers(current)[0]["title"], "Database Systems for Records")
        self.assertEqual(recent_papers(old), [])

    def test_medscribe_is_project_experience(self) -> None:
        text = medscribe_relevance(["Database Systems", "Healthcare IT / Health Informatics"], load_profile())

        self.assertIsNotNone(text)
        assert text is not None
        self.assertIn("MedScribeAI", text)
        self.assertIn("software project", text)
        self.assertNotIn("medical researcher", text.casefold())

    def test_similar_names_stay_separate(self) -> None:
        bit = _record(name="Zhihong Deng", university="Beijing Institute of Technology")
        peking = _record(name="Zhi-Hong Deng", university="Peking University")
        unicode_name = _record(name="Zhi‐Hong Deng", university="Peking University")

        self.assertFalse(is_same_professor(bit, peking))
        self.assertEqual(len(deduplicate_records([bit, peking, unicode_name])), 3)

    def test_shortlist_keeps_a_strong_candidate_without_email(self) -> None:
        profile = load_profile()
        config = load_search_config()
        record = _record()
        record.verification_status = "officially_verified"
        record.official_profile_url = OFFICIAL_PROFILE
        record.official_source = "official_professor_or_lab_page"
        record.verified_at = "2026-09-28T10:00:00+00:00"
        result = evaluate_professor(profile, config, record)

        self.assertTrue(qualifies_for_shortlist(record, result))
        self.assertIsNone(record.public_email)
        with tempfile.TemporaryDirectory() as folder:
            report = publish_review([record], profile=profile, config=config, output_dir=Path(folder))
            drafts = json.loads((Path(folder) / "email_drafts.json").read_text(encoding="utf-8"))

        self.assertEqual(report.strong_matches, 1)
        self.assertIsNone(drafts[0]["recipient_email"])
        self.assertEqual(drafts[0]["draft_status"], "draft")
        self.assertIn("Test Professor", drafts[0]["body"])
        self.assertIn("Database Systems", drafts[0]["body"])
        self.assertIn("BS Software Engineering", drafts[0]["body"])
        self.assertIn("Chinese Government Scholarship (CSC)", drafts[0]["body"])
        self.assertIn("Database Systems for Records", drafts[0]["body"])
        self.assertIn("supervise", drafts[0]["body"])

    def test_email_drafts_are_personalized(self) -> None:
        profile = load_profile()
        config = load_search_config()
        first = _verified(_record(name="Ada Example", research_interests=["Database Systems"]))
        second = _verified(
            _record(
                name="Bea Example",
                research_interests=["Software Engineering"],
                papers=[
                    PaperRecord(
                        title="Software Engineering Studio",
                        year=date.today().year,
                        abstract=None,
                    )
                ],
            )
        )
        first_draft = draft_csc_email(profile, first, evaluate_professor(profile, config, first))
        second_draft = draft_csc_email(profile, second, evaluate_professor(profile, config, second))

        self.assertIsNotNone(first_draft)
        self.assertIsNotNone(second_draft)
        assert first_draft is not None and second_draft is not None
        self.assertNotEqual(first_draft["body"], second_draft["body"])
        self.assertIn("Ada Example", str(first_draft["body"]))
        self.assertIn("Bea Example", str(second_draft["body"]))
        self.assertIn("supervise", str(first_draft["body"]))
        self.assertEqual(first_draft["draft_status"], "draft")
        self.assertNotEqual(first_draft["matched_research_areas"], second_draft["matched_research_areas"])

        obfuscated = _verified(_record(name="Bin Cui", public_email="bin.cuipku.edu.cn"))
        missing = draft_csc_email(profile, obfuscated, evaluate_professor(profile, config, obfuscated))
        self.assertIsNotNone(missing)
        assert missing is not None
        self.assertIsNone(missing["recipient_email"])

    def test_approval_does_not_send(self) -> None:
        record = _verified(_record())
        match = MatchResult(
            professor_name=record.name,
            university=record.university,
            passed_filters=True,
            filter_notes=[],
            research_score=50,
            matched_interests=["Database Systems"],
            matched_fields=["Database Systems"],
            paper_notes=[],
            summary="Specific overlap.",
            matched_areas=["Database Systems"],
            evidence=["Database Systems found in research interests."],
            research_confidence="medium",
        )
        entry = approve_draft(tracker_entry(record, match, has_draft=True))

        self.assertFalse(SENDS_EMAIL)
        self.assertEqual(entry.email_status, "approved")
        self.assertIn(entry.email_status, TRACKER_STATUSES)
        self.assertIsNone(entry.date_contacted)
        self.assertNotEqual(entry.email_status, "sent")

    def test_new_modules_do_not_import_smtplib(self) -> None:
        for relative in ("app/search_api.py", "app/agent_pipeline.py", "app/outreach.py", "app/main.py"):
            tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
            imported = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.extend(alias.name for alias in node.names)
                if isinstance(node, ast.ImportFrom) and node.module:
                    imported.append(node.module)
            self.assertNotIn("smtplib", imported)

    def test_limits_default_to_ten_candidates(self) -> None:
        self.assertEqual(PipelineLimits().max_candidates_per_run, 10)


def _verified(record: ProfessorRecord) -> ProfessorRecord:
    record.verification_status = "officially_verified"
    record.official_profile_url = OFFICIAL_PROFILE
    record.official_source = "official_professor_or_lab_page"
    record.verified_at = "2026-09-28T10:00:00+00:00"
    return record


class UnusedProfileGuard(unittest.TestCase):
    def test_blank_profile_name_is_not_invented_in_a_draft(self) -> None:
        profile = UserProfile()
        record = _verified(_record())
        # The real profile is used by the pipeline. This guard checks the empty
        # personal name path with the loaded academic profile's education.
        loaded = load_profile()
        loaded.personal.full_name = ""
        draft = draft_csc_email(loaded, record, evaluate_professor(loaded, load_search_config(), record))

        self.assertIsNotNone(draft)
        assert draft is not None
        self.assertNotIn("Dear Professor ,", str(draft["body"]))
        self.assertTrue(profile.personal.full_name == "")


class ResearchAndDraftTests(unittest.TestCase):
    def test_command_defaults_to_five_without_searching(self) -> None:
        args = parse_args(["research-and-draft"])

        self.assertEqual(args.command, "research-and-draft")
        self.assertEqual(args.limit, 5)

    def test_batch_collects_real_papers_and_saves_an_unsent_draft(self) -> None:
        calls: list[str] = []

        def http_get(url: str) -> dict:
            calls.append(url)
            return {
                "results": [
                    {
                        "title": "Query Processing for Database Systems",
                        "publication_year": date.today().year,
                        "abstract_inverted_index": {
                            "Database": [0],
                            "Systems": [1],
                            "store": [2],
                            "structured": [3],
                            "records": [4],
                        },
                        "id": "https://openalex.org/W9",
                        "doi": "https://doi.org/10.1000/example",
                    },
                    {
                        "title": "Index Structures",
                        "publication_year": date.today().year,
                        "abstract_inverted_index": None,
                        "id": "https://openalex.org/W8",
                    },
                    {
                        "title": "Old Database Notes",
                        "publication_year": date.today().year - 12,
                        "id": "https://openalex.org/W7",
                    },
                ]
            }

        records = [_record(papers=[], public_email="bin.cuipku.edu.cn")]
        records.extend(
            _record(name=f"Other {index}", papers=[], source_urls=[], public_email=None)
            for index in range(5)
        )
        with tempfile.TemporaryDirectory() as folder:
            store = ProfessorStore(Path(folder) / "professors.json")
            report = research_and_draft(
                limit=5,
                records=records,
                provider=_FixtureSearch(),
                fetcher=OfficialPageFetcher(opener=_opener, check_robots=True),
                paper_source=OpenAlexSource(http_get=http_get),
                store=store,
                output_dir=Path(folder),
                enable_openalex=False,
            )
            drafts = json.loads((Path(folder) / "email_drafts.json").read_text(encoding="utf-8"))
            saved = store.load()

        self.assertEqual(report.candidates_processed, 5)
        self.assertEqual(report.emails_sent, 0)
        self.assertEqual(len(calls), 1)
        self.assertIn("works", calls[0])
        self.assertEqual(len(drafts), 1)
        draft = drafts[0]
        self.assertEqual(draft["professor_name"], "Test Professor")
        self.assertIsNone(draft["recipient_email"])
        self.assertEqual(draft["draft_status"], "draft")
        self.assertFalse(draft["gmail_preparation"]["sent"])
        self.assertEqual(draft["gmail_preparation"]["provider"], "gmail")
        titles = [item["title"] for item in draft["paper_evidence"]]
        self.assertIn("Query Processing for Database Systems", titles)
        self.assertIn("Index Structures", titles)
        self.assertNotIn("Old Database Notes", titles)
        retrieved = next(item for item in draft["paper_evidence"] if item["title"].startswith("Query"))
        self.assertEqual(retrieved["abstract"], "Database Systems store structured records")
        self.assertEqual(retrieved["doi"], "https://doi.org/10.1000/example")
        self.assertIn("Database Systems", retrieved["connections"])
        missing = next(item for item in draft["paper_evidence"] if item["title"] == "Index Structures")
        self.assertIsNone(missing["abstract"])
        body = str(draft["body"])
        self.assertIn("Query Processing for Database Systems", body)
        self.assertIn("Chinese Government Scholarship (CSC)", body)
        self.assertNotIn("I read", body)
        self.assertNotIn("I am not claiming", body)
        self.assertTrue(saved)
        self.assertTrue(any(paper.title == "Query Processing for Database Systems" for paper in saved[0].papers))

    def test_missing_paper_is_not_described_as_read(self) -> None:
        profile = load_profile()
        config = load_search_config()
        record = _verified(_record(papers=[]))
        draft = draft_csc_email(profile, record, evaluate_professor(profile, config, record))

        self.assertIsNotNone(draft)
        assert draft is not None
        self.assertEqual(draft["paper_evidence"], [])
        self.assertNotIn("I read", str(draft["body"]))
        self.assertNotIn("Your paper", str(draft["body"]))
        self.assertNotIn("Your 20", str(draft["body"]))


if __name__ == "__main__":
    unittest.main()

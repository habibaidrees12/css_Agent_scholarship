"""Tests for discovery, storage, and source parsing.

Professor names in this file are test fixtures. They are not written to
data/professors.json.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import BROAD_ACADEMIC_FIELDS, get_professors_path, get_search_config_path
from app.discovery import run_discovery
from app.models import CityFilter, PaperRecord, ProfessorRecord, UniversityFilter
from app.profile import load_profile
from app.research import build_queries, evaluate_professor, load_search_config
from app.sources import (
    AcademicSource,
    OfficialUniversitySource,
    OpenAlexSource,
    SearchQuery,
    classify_public_url,
)
from app.storage import ProfessorStore, is_same_professor, record_errors


class FixtureSource(AcademicSource):
    """In-memory source used only by tests. It does not represent a real directory."""

    source_name = "fixture"
    source_kind = "reputable_academic_paper_source"
    trustworthy = True

    def __init__(self, records: list[ProfessorRecord]) -> None:
        self.records = records

    def search_professors(self, query: SearchQuery) -> list[ProfessorRecord]:
        if query.research_area != "Database Systems":
            return []
        return list(self.records)

    def get_professor_profile(self, profile_url: str) -> ProfessorRecord | None:
        return None

    def get_papers(self, author_key: str) -> list[PaperRecord]:
        return []


def _record(**kwargs: object) -> ProfessorRecord:
    data = {
        "name": "Test Professor",
        "university": "Test University",
        "country": "China",
        "department": "School of Computer Science and Technology",
        "research_interests": ["Database Systems"],
    }
    data.update(kwargs)
    return ProfessorRecord(**data)  # type: ignore[arg-type]


class DiscoveryTests(unittest.TestCase):
    def test_professor_file_stays_within_the_live_cap(self) -> None:
        raw = json.loads(get_professors_path().read_text(encoding="utf-8"))
        self.assertIsInstance(raw, list)
        self.assertLessEqual(len(raw), 30)
        for item in raw:
            self.assertTrue(item["name"].strip())
            self.assertTrue(item["university"].strip())
            self.assertTrue(item["source_urls"])
            self.assertNotIn("Test Professor", item["name"])
            self.assertNotEqual(item["name"], "Parser Fixture")
            self.assertIn("public_email", item)
            self.assertIn("city", item)

    def test_search_config_is_china_and_any_city(self) -> None:
        config = load_search_config()
        text = get_search_config_path().read_text(encoding="utf-8").casefold()

        self.assertEqual(config.country.mode, "one")
        self.assertEqual(config.country.countries, ["China"])
        self.assertEqual(config.cities.mode, "any")
        self.assertEqual(config.cities.cities, [])
        self.assertEqual(config.universities.mode, "any")
        self.assertEqual(config.universities.universities, [])
        self.assertEqual(set(config.research_areas.areas), set(BROAD_ACADEMIC_FIELDS))
        self.assertGreater(len(config.research_areas.areas), 1)
        self.assertEqual(config.minimum_research_confidence, "medium")
        self.assertNotIn("beijing", text)
        self.assertNotIn("shanghai", text)
        self.assertNotIn("wuhan", text)

    def test_queries_cover_broad_areas_for_china(self) -> None:
        queries = build_queries(load_search_config())
        areas = {query.research_area for query in queries}

        self.assertEqual(areas, set(BROAD_ACADEMIC_FIELDS))
        self.assertTrue(all(query.country == "China" for query in queries))
        self.assertTrue(all(query.city_mode == "any" for query in queries))
        self.assertIn("Database Systems", areas)
        self.assertIn("Healthcare IT / Health Informatics", areas)

    def test_city_filter_supports_any_one_and_multiple(self) -> None:
        profile = load_profile()
        config = load_search_config()
        professor = _record(city=None)
        any_city = evaluate_professor(profile, config, professor)
        self.assertTrue(any_city.passed_filters)
        self.assertIsNone(professor.city)

        config.cities = CityFilter(mode="one", cities=["City One"])
        missing = evaluate_professor(profile, config, _record(city=None))
        matched = evaluate_professor(profile, config, _record(city="City One"))
        other = evaluate_professor(profile, config, _record(city="City Two"))
        self.assertFalse(missing.passed_filters)
        self.assertTrue(matched.passed_filters)
        self.assertFalse(other.passed_filters)

        config.cities = CityFilter(mode="multiple", cities=["City One", "City Two"])
        self.assertTrue(evaluate_professor(profile, config, _record(city="City Two")).passed_filters)
        self.assertFalse(evaluate_professor(profile, config, _record(city="City Three")).passed_filters)

    def test_country_and_university_filters(self) -> None:
        profile = load_profile()
        config = load_search_config()
        outside = evaluate_professor(profile, config, _record(country="Germany"))
        inside = evaluate_professor(profile, config, _record(country="China"))
        missing = evaluate_professor(profile, config, _record(country=None))

        self.assertFalse(outside.passed_filters)
        self.assertTrue(inside.passed_filters)
        self.assertFalse(missing.passed_filters)
        self.assertIsNone(_record(country=None).country)

        config.universities = UniversityFilter(mode="one", universities=["Test University"])
        self.assertTrue(evaluate_professor(profile, config, _record()).passed_filters)
        self.assertFalse(
            evaluate_professor(profile, config, _record(university="Other University")).passed_filters
        )

    def test_research_area_filter_is_not_exact_software_engineering(self) -> None:
        profile = load_profile()
        config = load_search_config()
        computer_science = _record(
            department="School of Computer Science and Technology",
            research_interests=["Database Systems"],
        )
        unrelated = _record(department="Physics", research_interests=["Optics"], papers=[])

        matched = evaluate_professor(profile, config, computer_science)
        rejected = evaluate_professor(profile, config, unrelated)

        self.assertNotEqual(computer_science.department, "Software Engineering")
        self.assertTrue(matched.passed_filters)
        self.assertGreater(matched.research_score, 0)
        self.assertFalse(rejected.passed_filters)

        from app.research import ResearchAreaFilter

        config.research_areas = ResearchAreaFilter(mode="one", areas=["Database Systems"])
        machine_learning = _record(department="Computer Science", research_interests=["Machine Learning"])
        self.assertFalse(evaluate_professor(profile, config, machine_learning).passed_filters)
        self.assertTrue(evaluate_professor(profile, config, computer_science).passed_filters)

    def test_deduplication_requires_a_strong_key(self) -> None:
        same_person = _record(name="Test Professor", university="Test University")
        same_again = _record(name="test  professor", university="Test University", public_email=None)
        similar_name = _record(name="Test Professor Jr", university="Test University")
        other_university = _record(name="Test Professor", university="Second Test University")
        shared_mailbox = _record(
            name="Another Test Professor",
            university="Second Test University",
            public_email="shared@example.edu",
        )
        mailbox_owner = _record(
            name="Test Professor",
            university="Second Test University",
            public_email="shared@example.edu",
        )
        same_page_left = _record(
            professor_profile_url="https://cs.example.edu.cn/people/test",
            university="Test University",
        )
        same_page_right = _record(
            name="Test Professor",
            professor_profile_url="https://cs.example.edu.cn/people/test/",
            university="Another University",
        )

        self.assertTrue(is_same_professor(same_person, same_again))
        self.assertFalse(is_same_professor(same_person, similar_name))
        self.assertFalse(is_same_professor(same_person, other_university))
        self.assertFalse(is_same_professor(shared_mailbox, mailbox_owner))
        self.assertTrue(is_same_professor(same_page_left, same_page_right))
        self.assertTrue(
            is_same_professor(
                mailbox_owner,
                _record(name="Test Professor", university="Elsewhere University", public_email="shared@example.edu"),
            )
        )

        with tempfile.TemporaryDirectory() as folder:
            store = ProfessorStore(Path(folder) / "professors.json")
            store.add(same_person)
            store.add(same_again)
            store.add(similar_name)
            store.add(other_university)
            self.assertEqual(len(store.all()), 3)

    def test_missing_email_city_and_abstract_stay_null(self) -> None:
        before = get_professors_path().read_text(encoding="utf-8")
        source = OpenAlexSource()
        author = source.parse_author(
            {
                "id": "https://openalex.org/A1",
                "display_name": "Parser Fixture",
                "last_known_institutions": [
                    {
                        "display_name": "Beijing Normal University",
                        "country_code": "CN",
                        "homepage_url": "https://example.edu.cn",
                    }
                ],
            }
        )
        paper = source.parse_work(
            {
                "id": "https://openalex.org/W1",
                "title": "Database Systems for Records",
                "publication_year": date.today().year,
                "abstract_inverted_index": None,
            }
        )

        self.assertIsNotNone(author)
        assert author is not None
        self.assertIsNone(author.public_email)
        self.assertIsNone(author.city)
        self.assertEqual(author.country, "China")
        self.assertEqual(author.university, "Beijing Normal University")
        self.assertTrue(author.verified)
        self.assertEqual(author.last_verified, date.today().isoformat())
        self.assertIsNotNone(paper)
        assert paper is not None
        self.assertIsNone(paper.abstract)
        self.assertEqual(get_professors_path().read_text(encoding="utf-8"), before)

        decoded = source.parse_work(
            {
                "title": "Stored Abstract",
                "abstract_inverted_index": {"Stored": [0], "Abstract": [1]},
            }
        )
        assert decoded is not None
        self.assertEqual(decoded.abstract, "Stored Abstract")

    def test_valid_record_round_trip_keeps_nulls(self) -> None:
        record = _record(
            public_email=None,
            city=None,
            papers=[PaperRecord(title="Database Systems for Records", year=None, abstract=None)],
        )
        self.assertEqual(record_errors(record), [])

        with tempfile.TemporaryDirectory() as folder:
            store = ProfessorStore(Path(folder) / "professors.json")
            stored = store.add(record)
            loaded = store.all()[0]

        self.assertTrue(stored.professor_id.startswith("prof_"))
        self.assertIsNone(loaded.public_email)
        self.assertIsNone(loaded.city)
        self.assertIsNone(loaded.papers[0].abstract)
        self.assertEqual(loaded.name, "Test Professor")

    def test_store_filters_country_city_and_research_area(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = ProfessorStore(Path(folder) / "professors.json")
            store.add(_record(name="Test Professor A", country="China", city="City One"))
            store.add(
                _record(
                    name="Test Professor B",
                    university="Second Test University",
                    country="Germany",
                    city="City Two",
                    department="Physics",
                    research_interests=["Optics"],
                )
            )
            china = store.filtered(country="China")
            city = store.filtered(city="City One")
            area = store.filtered(research_area="Database Systems")

        self.assertEqual([item.name for item in china], ["Test Professor A"])
        self.assertEqual([item.name for item in city], ["Test Professor A"])
        self.assertEqual([item.name for item in area], ["Test Professor A"])

    def test_match_integration_saves_a_broad_cs_match(self) -> None:
        professor = _record(
            public_email=None,
            city=None,
            department="School of Computer Science and Technology",
            research_interests=["Database Systems"],
            papers=[
                PaperRecord(
                    title="Database Systems for Records",
                    year=date.today().year,
                    abstract=None,
                    source_url="https://openalex.org/W1",
                )
            ],
            source_urls=["https://openalex.org/A1"],
            source_kind="reputable_academic_paper_source",
            verified=True,
            last_verified=date.today().isoformat(),
        )
        before = get_professors_path().read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "professors.json"
            saved = run_discovery(store_path=path, sources=[FixtureSource([professor])])
            stored = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(len(saved), 1)
        self.assertEqual(stored[0]["public_email"], None)
        self.assertEqual(stored[0]["city"], None)
        self.assertEqual(stored[0]["papers"][0]["abstract"], None)
        self.assertNotEqual(stored[0]["department"], "Software Engineering")
        self.assertTrue(stored[0]["match_result"]["passed_filters"])
        self.assertGreater(stored[0]["match_result"]["research_score"], 0)
        self.assertIn("Database Systems", stored[0]["match_result"]["summary"])
        self.assertEqual(get_professors_path().read_text(encoding="utf-8"), before)

    def test_low_confidence_candidates_can_be_filtered(self) -> None:
        profile = load_profile()
        config = load_search_config()
        generic = _record(
            name="Generic Topic Only",
            department=None,
            research_interests=["Computer science", "Chemistry"],
            papers=[],
        )
        specific = _record(
            name="Data Researcher",
            department=None,
            research_interests=["Data Science"],
            papers=[],
        )

        self.assertEqual(config.minimum_research_confidence, "medium")
        generic_result = evaluate_professor(profile, config, generic)
        specific_result = evaluate_professor(profile, config, specific)
        self.assertEqual(generic_result.research_confidence, "low")
        self.assertFalse(generic_result.passed_filters)
        self.assertEqual(specific_result.research_confidence, "medium")
        self.assertTrue(specific_result.passed_filters)

        config.minimum_research_confidence = "high"
        raised = evaluate_professor(profile, config, specific)
        self.assertEqual(raised.research_confidence, "medium")
        self.assertFalse(raised.passed_filters)

    def test_similar_names_at_different_universities_stay_separate(self) -> None:
        zhihong = _record(
            name="Zhihong Deng",
            university="Beijing Institute of Technology",
            public_email=None,
            professor_profile_url=None,
        )
        zhi_hong = _record(
            name="Zhi-Hong Deng",
            university="Peking University",
            public_email=None,
            professor_profile_url=None,
        )
        zhi_hong_unicode = _record(
            name="Zhi\u2010Hong Deng",
            university="Peking University",
            public_email=None,
            professor_profile_url=None,
        )

        self.assertFalse(is_same_professor(zhihong, zhi_hong))
        self.assertFalse(is_same_professor(zhihong, zhi_hong_unicode))
        self.assertFalse(is_same_professor(zhi_hong, zhi_hong_unicode))

    def test_default_discovery_does_not_require_a_key_or_network(self) -> None:
        source = OpenAlexSource()
        query = SearchQuery(research_area="Database Systems", country="China", city_mode="any")
        url = source.build_search_url(query)
        before = get_professors_path().read_text(encoding="utf-8")

        self.assertEqual(source.search_professors(query), [])
        self.assertIsNone(source.get_professor_profile("https://openalex.org/A1"))
        self.assertEqual(source.get_papers("A1"), [])
        self.assertEqual(OfficialUniversitySource().search_professors(query), [])
        self.assertIn("country_code", url)
        self.assertIn("CN", url)
        self.assertNotIn("beijing", url.casefold())
        self.assertIsNone(classify_public_url("https://example.com/blog/professor"))
        self.assertEqual(
            classify_public_url("https://cs.example.edu.cn/people/test"),
            "official_professor_or_lab_page",
        )

        with tempfile.TemporaryDirectory() as folder:
            saved = run_discovery(store_path=Path(folder) / "professors.json")
        self.assertEqual(saved, [])
        self.assertEqual(get_professors_path().read_text(encoding="utf-8"), before)

    def test_max_saved_stops_before_writing_more_records(self) -> None:
        records = [
            _record(
                name=f"Test Professor {index}",
                university=f"Test University {index}",
                source_urls=[f"https://openalex.org/A{index}"],
            )
            for index in range(5)
        ]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "professors.json"
            saved = run_discovery(store_path=path, sources=[FixtureSource(records)], max_saved=2)
            stored = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(len(saved), 2)
        self.assertEqual(len(stored), 2)

    def test_update_can_clear_an_email_back_to_null(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = ProfessorStore(Path(folder) / "professors.json")
            stored = store.add(_record(public_email="name@example.edu"))
            updated = store.update(stored.professor_id, {"public_email": None, "notes": "Email removed."})

        self.assertIsNone(updated.public_email)
        self.assertEqual(updated.notes, "Email removed.")


if __name__ == "__main__":
    unittest.main()

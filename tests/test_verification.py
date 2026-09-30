"""Tests for official university verification.

The professor names here are fixtures for identity rules. They are not written
to data/professors.json, and no message is sent.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_professors_path
from app.models import VERIFICATION_STATUSES, PaperRecord, ProfessorRecord
from app.storage import ProfessorStore, is_same_professor
from app.university_sources import CatalogUniversitySource, OfficialPage, is_official_university_url
from app.verification import SENDS_EMAIL, matched_display_name, names_match, published_email, verify_candidates, verify_professor

TIMESTAMP = "2026-09-28T10:00:00+00:00"
OPENALEX_URL = "https://openalex.org/A100"
OFFICIAL_URL = "https://cs.example.edu.cn/people/test"
DEPARTMENT_URL = "https://cs.example.edu.cn/school/computer"


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


def _page(**kwargs: object) -> OfficialPage:
    data = {
        "url": OFFICIAL_URL,
        "name": "Test Professor",
        "university": "Test University",
        "department": None,
        "city": None,
        "email": None,
    }
    data.update(kwargs)
    return OfficialPage(**data)  # type: ignore[arg-type]


def _catalog(page: OfficialPage, names: list[str] | None = None) -> CatalogUniversitySource:
    chosen = names or [page.name or ""]
    assignments = [(name, None, [page.url]) for name in chosen]
    return CatalogUniversitySource(pages=[page], assignments=assignments)


class VerificationTests(unittest.TestCase):
    def test_official_university_url_is_accepted(self) -> None:
        page = _page(email="published@example.edu.cn", department_url=DEPARTMENT_URL)
        result = verify_professor(_record(), _catalog(page), verified_at=TIMESTAMP)

        self.assertTrue(is_official_university_url(OFFICIAL_URL))
        self.assertEqual(result.verification_status, "officially_verified")
        self.assertEqual(result.official_profile_url, OFFICIAL_URL)
        self.assertEqual(result.official_department_url, DEPARTMENT_URL)
        self.assertEqual(result.official_source, "official_professor_or_lab_page")
        self.assertEqual(result.official_email, "published@example.edu.cn")
        self.assertEqual(result.official_field_sources["official_email"], OFFICIAL_URL)
        self.assertIsNone(result.public_email)

    def test_non_official_url_is_rejected(self) -> None:
        rejected_urls = [
            "https://twitter.com/professor",
            "https://www.researchgate.net/profile/test",
            "https://example.com/blog/professor",
            "https://openalex.org/A100",
        ]
        for url in rejected_urls:
            self.assertFalse(is_official_university_url(url))

        page = OfficialPage(
            url="https://twitter.com/professor",
            name="Test Professor",
            university="Test University",
            email="hidden@twitter.com",
        )
        result = verify_professor(_record(source_urls=[OPENALEX_URL]), _catalog(page), verified_at=TIMESTAMP)

        self.assertIsNone(result.official_profile_url)
        self.assertIsNone(result.official_email)
        self.assertNotEqual(result.verification_status, "officially_verified")
        self.assertNotIn("https://twitter.com/professor", result.source_urls)

    def test_missing_city_remains_null(self) -> None:
        result = verify_professor(
            _record(university="Beijing Institute of Technology", city=None),
            _catalog(_page(university="Beijing Institute of Technology", city=None)),
            verified_at=TIMESTAMP,
        )

        self.assertIsNone(result.city)
        self.assertIsNone(result.official_city)

    def test_missing_department_remains_null(self) -> None:
        result = verify_professor(
            _record(department=None, research_interests=["Database Systems"]),
            _catalog(_page(department=None, research_areas=["Database Systems"])),
            verified_at=TIMESTAMP,
        )

        self.assertIsNone(result.department)
        self.assertIsNone(result.official_department)

    def test_missing_email_remains_null(self) -> None:
        result = verify_professor(_record(public_email=None), _catalog(_page(email=None)), verified_at=TIMESTAMP)

        self.assertIsNone(result.public_email)
        self.assertIsNone(result.official_email)

    def test_email_is_never_guessed(self) -> None:
        result = verify_professor(
            _record(name="Ann Lee", university="Test University"),
            _catalog(_page(name="Ann Lee", email=None)),
            verified_at=TIMESTAMP,
        )

        self.assertIsNone(published_email(None))
        self.assertIsNone(published_email("ann.lee"))
        self.assertIsNone(result.public_email)
        self.assertIsNone(result.official_email)
        self.assertNotIn("@", result.verification_notes)

    def test_identity_mismatch_is_rejected(self) -> None:
        page = _page(name="Other Person", email="other@example.edu.cn", city="Nanjing")
        result = verify_professor(_record(), _catalog(page, names=["Test Professor"]), verified_at=TIMESTAMP)

        self.assertEqual(result.verification_status, "verification_failed")
        self.assertIsNone(result.official_profile_url)
        self.assertIsNone(result.official_email)
        self.assertIsNone(result.official_city)
        self.assertEqual(result.university, "Test University")
        self.assertIn("Identity mismatch", result.verification_notes)

    def test_similar_names_remain_separate(self) -> None:
        page = OfficialPage(
            url="https://cs.example.edu.cn/people/zhi-hong",
            name="Zhi-Hong Deng",
            university="Peking University",
        )
        zhihong = _record(
            name="Zhihong Deng",
            university="Beijing Institute of Technology",
            source_urls=["https://openalex.org/A1"],
        )
        zhi_hong = _record(
            name="Zhi-Hong Deng",
            university="Peking University",
            source_urls=["https://openalex.org/A2"],
        )
        zhi_hong_unicode = _record(
            name="Zhi\u2010Hong Deng",
            university="Peking University",
            source_urls=["https://openalex.org/A3"],
        )
        source = CatalogUniversitySource(
            pages=[page],
            assignments=[
                ("Zhihong Deng", "Beijing Institute of Technology", [page.url]),
                ("Zhi-Hong Deng", "Peking University", [page.url]),
                ("Zhi\u2010Hong Deng", "Peking University", [page.url]),
            ],
        )

        results = verify_candidates([zhihong, zhi_hong, zhi_hong_unicode], source, verified_at=TIMESTAMP)

        self.assertEqual(len(results), 3)
        self.assertEqual(results[0].verification_status, "verification_failed")
        self.assertIsNone(results[0].official_profile_url)
        self.assertEqual(results[1].verification_status, "officially_verified")
        self.assertEqual(results[1].official_profile_url, page.url)
        self.assertEqual(results[2].verification_status, "officially_verified")
        self.assertEqual(results[2].official_profile_url, page.url)
        self.assertFalse(is_same_professor(results[0], results[1]))
        self.assertFalse(is_same_professor(results[1], results[2]))
        self.assertFalse(is_same_professor(results[0], results[2]))

    def test_official_university_overrides_conflict_without_erasing_openalex(self) -> None:
        openalex_url = "https://openalex.org/A-openalex"
        page = _page(
            university="University B",
            paper_titles=["Database Systems for Records"],
        )
        original = _record(
            university="University A",
            source_urls=[openalex_url],
            papers=[PaperRecord(title="Database Systems for Records", year=2020, abstract=None)],
        )

        result = verify_professor(original, _catalog(page), verified_at=TIMESTAMP)

        self.assertEqual(result.official_university, "University B")
        self.assertEqual(result.university, "University A")
        self.assertIn(openalex_url, result.source_urls)
        self.assertIn(OFFICIAL_URL, result.source_urls)
        self.assertIn("University A", result.verification_notes)
        self.assertIn("University B", result.verification_notes)
        self.assertEqual(result.official_field_sources["official_university"], OFFICIAL_URL)
        self.assertEqual(original.university, "University A")
        self.assertEqual(original.verification_status, "unverified")

    def test_existing_data_is_preserved_when_verification_fails(self) -> None:
        original = _record(
            university="University A",
            research_interests=["Database Systems"],
            papers=[PaperRecord(title="Database Systems for Records", abstract=None, source_url=OPENALEX_URL)],
            source_urls=[OPENALEX_URL],
        )
        page = _page(name="Different Person", university="University B")

        result = verify_professor(original, _catalog(page, names=["Test Professor"]), verified_at=TIMESTAMP)

        self.assertEqual(result.verification_status, "verification_failed")
        self.assertEqual(result.university, "University A")
        self.assertEqual(result.research_interests, ["Database Systems"])
        self.assertEqual(result.papers[0].title, "Database Systems for Records")
        self.assertEqual(result.papers[0].abstract, None)
        self.assertEqual(result.source_urls, [OPENALEX_URL])
        self.assertIsNone(result.official_university)
        self.assertIsNone(result.city)
        self.assertIsNone(result.department)
        self.assertIsNone(result.public_email)

    def test_verification_status_and_verified_at_round_trip(self) -> None:
        verified = verify_professor(_record(), _catalog(_page()), verified_at=TIMESTAMP)
        failed = verify_professor(
            _record(name="Other Professor", university="Second Test University"),
            _catalog(_page(name="Wrong Person"), names=["Other Professor"]),
            verified_at=TIMESTAMP,
        )

        self.assertEqual(set(VERIFICATION_STATUSES), {
            "unverified",
            "partially_verified",
            "officially_verified",
            "verification_failed",
        })
        self.assertEqual(verified.verification_status, "officially_verified")
        self.assertEqual(failed.verification_status, "verification_failed")
        self.assertEqual(verified.verified_at, TIMESTAMP)
        self.assertEqual(failed.verified_at, TIMESTAMP)

        with tempfile.TemporaryDirectory() as folder:
            store = ProfessorStore(Path(folder) / "professors.json")
            store.add(verified)
            store.add(failed)
            loaded = {item.name: item for item in store.all()}

        self.assertEqual(loaded["Test Professor"].verification_status, "officially_verified")
        self.assertEqual(loaded["Test Professor"].verified_at, TIMESTAMP)
        self.assertEqual(loaded["Test Professor"].official_profile_url, OFFICIAL_URL)
        self.assertEqual(loaded["Other Professor"].verification_status, "verification_failed")
        self.assertEqual(loaded["Other Professor"].verified_at, TIMESTAMP)
        self.assertIsNone(loaded["Other Professor"].official_profile_url)

    def test_failed_merge_does_not_erase_official_values(self) -> None:
        verified = verify_professor(_record(), _catalog(_page()), verified_at=TIMESTAMP)
        failed = ProfessorRecord.from_dict(verified.to_dict())
        failed.verification_status = "verification_failed"
        failed.verification_notes = "A later attempt failed."
        failed.official_university = None
        failed.official_profile_url = None
        failed.verified_at = TIMESTAMP

        with tempfile.TemporaryDirectory() as folder:
            store = ProfessorStore(Path(folder) / "professors.json")
            store.add(verified)
            stored = store.add(failed)

        self.assertEqual(stored.verification_status, "officially_verified")
        self.assertEqual(stored.official_university, "Test University")
        self.assertEqual(stored.official_profile_url, OFFICIAL_URL)
        self.assertIn("A later attempt failed.", stored.verification_notes)

    def test_name_and_research_area_is_verified(self) -> None:
        page = _page(name="Test Professor", university=None, research_areas=["Database Systems"])
        result = verify_professor(
            _record(research_interests=["Database Systems"]),
            _catalog(page),
            verified_at=TIMESTAMP,
        )

        self.assertEqual(result.verification_status, "officially_verified")
        self.assertIn("research", result.verification_notes)

    def test_name_alone_is_rejected(self) -> None:
        page = _page(name="Test Professor", university=None, research_areas=[], paper_titles=[])
        result = verify_professor(_record(research_interests=[]), _catalog(page), verified_at=TIMESTAMP)

        self.assertEqual(result.verification_status, "verification_failed")
        self.assertIn("Name alone", result.verification_notes)
        self.assertIsNone(result.official_profile_url)

    def test_similar_name_is_rejected(self) -> None:
        page = _page(
            url="https://pure.bit.edu.cn/en/persons/fan-li",
            name="Fan Li",
            university="Beijing Institute of Technology",
        )
        result = verify_professor(
            _record(name="Dinghua Li", university="Beijing Institute of Technology", research_interests=[]),
            _catalog(page, names=["Dinghua Li"]),
            verified_at=TIMESTAMP,
        )

        self.assertEqual(result.verification_status, "verification_failed")
        self.assertIsNone(result.official_profile_url)
        self.assertIn("Identity mismatch", result.verification_notes)

    def test_hyphen_and_unicode_name_variation_matches(self) -> None:
        page = _page(
            url="https://sai.pku.edu.cn/szdw/zzjs/dzh.htm",
            name="Zhi-Hong Deng",
            university="Peking University",
        )
        unicode_name = "Zhi\u2010Hong Deng"
        result = verify_professor(
            _record(name=unicode_name, university="Peking University", research_interests=[]),
            _catalog(page, names=[unicode_name]),
            verified_at=TIMESTAMP,
        )

        self.assertTrue(names_match("Zhihong Deng", "Zhi-Hong Deng"))
        self.assertTrue(names_match(unicode_name, "Zhi-Hong Deng"))
        self.assertFalse(names_match("Fan Li", "Dinghua Li"))
        self.assertEqual(result.verification_status, "officially_verified")
        self.assertEqual(result.official_profile_url, page.url)
        self.assertEqual(
            matched_display_name("Profile of Zhi‐Hong Deng at Peking University", unicode_name),
            "Zhi‐Hong Deng",
        )

    def test_reversed_name_order_matches(self) -> None:
        page = _page(
            url="https://cs.pku.edu.cn/info/1155/1815.htm",
            name="Cui, Bin",
            university="Peking University",
        )
        result = verify_professor(
            _record(name="Bin Cui", university="Peking University", research_interests=[]),
            _catalog(page, names=["Bin Cui"]),
            verified_at=TIMESTAMP,
        )

        self.assertTrue(names_match("Bin Cui", "Cui, Bin"))
        self.assertEqual(result.verification_status, "officially_verified")
        self.assertEqual(
            matched_display_name("Cui, Bin. Peking University School of Computer Science.", "Bin Cui"),
            "Cui, Bin",
        )

    def test_generic_homepage_is_not_identity_proof(self) -> None:
        page = _page(
            url="https://english.pku.edu.cn/",
            name="Bin Cui",
            university="Peking University",
        )
        result = verify_professor(
            _record(name="Bin Cui", university="Peking University", research_interests=[]),
            _catalog(page, names=["Bin Cui"]),
            verified_at=TIMESTAMP,
        )

        self.assertNotEqual(result.verification_status, "officially_verified")
        self.assertIsNone(result.official_profile_url)
        self.assertIn("Generic university page", result.verification_notes)

    def test_no_email_is_sent(self) -> None:
        for relative in ("app/verification.py", "app/university_sources.py"):
            text = (ROOT / relative).read_text(encoding="utf-8").casefold()
            self.assertNotIn("smtplib", text)
            self.assertNotIn("sendmail", text)
            self.assertNotIn("mailto:", text)
        self.assertFalse(SENDS_EMAIL)
        result = verify_professor(_record(), _catalog(_page(email=None)), verified_at=TIMESTAMP)
        self.assertIsNone(result.public_email)
        self.assertIsNone(result.official_email)

    def test_saved_professor_file_is_not_rewritten_or_officially_verified(self) -> None:
        path = get_professors_path()
        before = path.read_bytes()
        records = ProfessorStore(path).load()
        self.assertGreater(len(records), 0)
        self.assertEqual(records[0].verification_status, "unverified")

        checked = verify_candidates(records, verified_at=TIMESTAMP)

        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(records[0].verification_status, "unverified")
        self.assertEqual(len(checked), len(records))
        self.assertTrue(all(item.verification_status != "officially_verified" for item in checked))
        self.assertTrue(all(item.official_profile_url is None for item in checked))
        self.assertTrue(all(item.official_email is None for item in checked))
        self.assertTrue(all(item.official_city is None for item in checked))
        self.assertTrue(all(item.official_department is None for item in checked))


if __name__ == "__main__":
    unittest.main()

"""Tests for importing officially verified professor records.

Fixtures are not written to data/verified_import.json or data/professors.json.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_data_dir, get_professors_path
from app.import_verified import SENDS_EMAIL, run_import, validate_import_item
from app.models import PaperRecord, ProfessorRecord
from app.storage import ProfessorStore, is_same_professor

OFFICIAL_URL = "https://cs.example.edu.cn/people/test"
OTHER_URL = "https://cs.example.edu.cn/people/other"
OPENALEX_URL = "https://openalex.org/A1"


def _payload(**kwargs: object) -> dict[str, object]:
    data: dict[str, object] = {
        "name": "Test Professor",
        "university": "Test University",
        "country": "China",
        "city": None,
        "department": "Department of Computer Science",
        "public_email": None,
        "official_profile_url": OFFICIAL_URL,
        "official_department_url": "https://cs.example.edu.cn/school/computer",
        "official_source": "official_professor_or_lab_page",
        "research_interests": ["Database Systems"],
        "papers": [{"title": "Database Systems for Records", "year": 2024, "abstract": None}],
        "verification_status": "officially_verified",
        "verification_notes": "Read on the official profile page.",
        "official_field_sources": {"official_profile_url": OFFICIAL_URL},
    }
    data.update(kwargs)
    return data


def _existing(**kwargs: object) -> ProfessorRecord:
    data = {
        "name": "Test Professor",
        "university": "Test University",
        "country": "China",
        "department": "Chemistry",
        "public_email": None,
        "city": None,
        "source_urls": [OPENALEX_URL],
        "research_interests": ["Chemistry"],
        "papers": [PaperRecord(title="OpenAlex Paper", year=2020, abstract=None, source_url=OPENALEX_URL)],
        "professor_id": "prof_existing",
    }
    data.update(kwargs)
    return ProfessorRecord(**data)  # type: ignore[arg-type]


class ImportVerifiedTests(unittest.TestCase):
    def test_empty_import_file(self) -> None:
        project_file = json.loads((get_data_dir() / "verified_import.json").read_text(encoding="utf-8"))
        self.assertEqual(project_file, [])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "verified_import.json"
            path.write_text("[]\n", encoding="utf-8")
            result = run_import(
                import_path=path,
                professors_path=Path(folder) / "professors.json",
                apply=False,
            )

        self.assertEqual(result["found"], 0)
        self.assertEqual(result["valid"], 0)
        self.assertEqual(result["invalid"], 0)
        self.assertEqual(result["would_add"], 0)
        self.assertFalse(result["applied"])

    def test_valid_officially_verified_record(self) -> None:
        record, errors = validate_import_item(_payload(), 0)

        self.assertEqual(errors, [])
        assert record is not None
        self.assertEqual(record.verification_status, "officially_verified")
        self.assertEqual(record.official_profile_url, OFFICIAL_URL)
        self.assertEqual(record.name, "Test Professor")

    def test_missing_name_is_rejected(self) -> None:
        record, errors = validate_import_item(_payload(name=""), 0)

        self.assertIsNone(record)
        self.assertTrue(any("name" in error for error in errors))

    def test_missing_university_is_rejected(self) -> None:
        record, errors = validate_import_item(_payload(university=""), 1)

        self.assertIsNone(record)
        self.assertTrue(any("university" in error for error in errors))

    def test_missing_official_profile_url_is_rejected(self) -> None:
        record, errors = validate_import_item(_payload(official_profile_url=""), 2)

        self.assertIsNone(record)
        self.assertTrue(any("official_profile_url" in error for error in errors))

    def test_unverified_record_is_rejected(self) -> None:
        record, errors = validate_import_item(
            _payload(verification_status="unverified", official_profile_url="https://example.com/professor"),
            3,
        )

        self.assertIsNone(record)
        self.assertTrue(any("officially_verified" in error for error in errors))

    def test_arbitrary_url_is_not_trusted(self) -> None:
        record, errors = validate_import_item(
            _payload(official_profile_url="https://www.researchgate.net/profile/test"),
            4,
        )

        self.assertIsNone(record)
        self.assertTrue(any("official_profile_url" in error for error in errors))

    def test_missing_email_is_allowed_and_not_generated(self) -> None:
        record, errors = validate_import_item(_payload(name="Ann Lee", public_email=None), 5)

        self.assertEqual(errors, [])
        assert record is not None
        self.assertIsNone(record.public_email)
        self.assertIsNone(record.official_email)
        self.assertNotIn("@", record.name)

    def test_duplicate_professor_is_updated_not_copied(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            professors = Path(folder) / "professors.json"
            ProfessorStore(professors).save([_existing()])
            result = self._apply(folder, professors, [_payload()])
            stored = ProfessorStore(professors).all()

        self.assertEqual(result["would_add"], 0)
        self.assertEqual(result["would_update"], 1)
        self.assertEqual(result["duplicates"], 1)
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].professor_id, "prof_existing")
        self.assertEqual(stored[0].official_profile_url, OFFICIAL_URL)

    def test_similar_names_at_different_universities_remain_separate(self) -> None:
        left = _payload(
            name="Zhihong Deng",
            university="Beijing Institute of Technology",
            official_profile_url="https://cs.example.edu.cn/people/zhihong",
            official_field_sources={
                "official_profile_url": "https://cs.example.edu.cn/people/zhihong"
            },
        )
        right = _payload(
            name="Zhi-Hong Deng",
            university="Peking University",
            official_profile_url="https://cs.example.edu.cn/people/zhi-hong",
            official_field_sources={
                "official_profile_url": "https://cs.example.edu.cn/people/zhi-hong"
            },
        )
        with tempfile.TemporaryDirectory() as folder:
            professors = Path(folder) / "professors.json"
            self._apply(folder, professors, [left, right])
            stored = ProfessorStore(professors).all()

        self.assertEqual(len(stored), 2)
        self.assertFalse(is_same_professor(stored[0], stored[1]))
        self.assertEqual(
            {item.name for item in stored},
            {"Zhihong Deng", "Zhi-Hong Deng"},
        )

    def test_provenance_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            professors = Path(folder) / "professors.json"
            ProfessorStore(professors).save([_existing()])
            self._apply(folder, professors, [_payload()])
            stored = ProfessorStore(professors).all()[0]

        self.assertIn(OPENALEX_URL, stored.source_urls)
        self.assertIn(OFFICIAL_URL, stored.source_urls)
        self.assertEqual(stored.papers[0].title, "OpenAlex Paper")
        self.assertIn("Chemistry", stored.research_interests)
        self.assertEqual(stored.official_field_sources["official_profile_url"], OFFICIAL_URL)
        self.assertIn("official profile page", stored.verification_notes)

    def test_conflicts_are_recorded_without_erasing_openalex_values(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            professors = Path(folder) / "professors.json"
            ProfessorStore(professors).save([_existing(department="Chemistry")])
            self._apply(folder, professors, [_payload(department="Department of Computer Science")])
            stored = ProfessorStore(professors).all()[0]

        self.assertEqual(stored.department, "Chemistry")
        self.assertEqual(stored.official_department, "Department of Computer Science")
        self.assertEqual(stored.university, "Test University")
        self.assertIn("conflicts", stored.verification_notes.casefold())
        self.assertIn("Chemistry", stored.verification_notes)
        self.assertIn("Department of Computer Science", stored.verification_notes)

    def test_dry_run_does_not_modify_professors_json(self) -> None:
        real_path = get_professors_path()
        before = real_path.read_bytes()
        result = run_import(apply=False)
        self.assertEqual(real_path.read_bytes(), before)
        self.assertFalse(result["applied"])
        self.assertFalse(SENDS_EMAIL)

        with tempfile.TemporaryDirectory() as folder:
            professors = Path(folder) / "professors.json"
            ProfessorStore(professors).save([_existing()])
            before_temp = professors.read_bytes()
            import_path = Path(folder) / "verified_import.json"
            import_path.write_text(json.dumps([_payload()]), encoding="utf-8")
            preview = run_import(import_path=import_path, professors_path=professors, apply=False)
            self.assertEqual(professors.read_bytes(), before_temp)

        self.assertEqual(preview["would_update"], 1)
        self.assertFalse(preview["applied"])

    def test_apply_imports_valid_records_only(self) -> None:
        valid = _payload(name="Valid Professor", official_profile_url=OFFICIAL_URL)
        invalid = _payload(name="", official_profile_url=OTHER_URL)
        with tempfile.TemporaryDirectory() as folder:
            professors = Path(folder) / "professors.json"
            result = self._apply(folder, professors, [valid, invalid])
            stored = ProfessorStore(professors).all()

        self.assertEqual(result["valid"], 1)
        self.assertEqual(result["invalid"], 1)
        self.assertEqual(result["would_add"], 1)
        self.assertTrue(result["applied"])
        self.assertEqual([item.name for item in stored], ["Valid Professor"])
        self.assertEqual(stored[0].verification_status, "officially_verified")

    def _apply(self, folder: str, professors: Path, items: list[dict[str, object]]) -> dict[str, object]:
        import_path = Path(folder) / "verified_import.json"
        import_path.write_text(json.dumps(items), encoding="utf-8")
        return run_import(import_path=import_path, professors_path=professors, apply=True)


if __name__ == "__main__":
    unittest.main()

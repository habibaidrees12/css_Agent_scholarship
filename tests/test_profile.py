"""Tests for the starter profile file and profile helpers."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import BROAD_ACADEMIC_FIELDS, get_profile_path
from app.main import main
from app.models import CityFilter, UserProfile
from app.profile import filled_education, load_profile, save_profile, validate_profile


class ProfileTests(unittest.TestCase):
    def test_profile_stays_broad_and_does_not_rank_ai_first(self) -> None:
        profile = load_profile(get_profile_path())
        fields = set(profile.preferred_fields)
        interests = {item.name: item for item in profile.research_interests}

        self.assertTrue(set(BROAD_ACADEMIC_FIELDS).issubset(fields))
        self.assertTrue(set(BROAD_ACADEMIC_FIELDS).issubset(interests))
        self.assertGreater(len(fields), 1)
        self.assertEqual(interests["Software Engineering"].priority, "high")
        self.assertEqual(interests["Computer Science"].priority, "high")
        self.assertEqual(interests["Data Science"].priority, "high")
        self.assertEqual(interests["Database Systems"].priority, "high")
        self.assertEqual(interests["Artificial Intelligence"].priority, "medium")
        self.assertEqual(interests["Machine Learning"].priority, "medium")
        self.assertEqual(interests["Natural Language Processing"].priority, "medium")
        self.assertNotIn("Artificial Intelligence", profile.priority_fields)
        self.assertNotIn("Natural Language Processing", profile.priority_fields)
        self.assertNotIn("Healthcare IT / Health Informatics", profile.priority_fields)
        self.assertIn("Software Engineering", profile.priority_fields)

    def test_starter_location_filters_accept_any_city(self) -> None:
        profile = load_profile(get_profile_path())

        self.assertEqual(profile.preferred_cities.mode, "any")
        self.assertEqual(profile.preferred_cities.cities, [])
        self.assertEqual(profile.preferred_countries.mode, "any")
        self.assertEqual(profile.preferred_universities.mode, "any")
        self.assertEqual(profile.department_filter.mode, "any")
        self.assertEqual(profile.field_filter.mode, "any")

    def test_starter_scholarship_focus_is_csc(self) -> None:
        profile = load_profile(get_profile_path())
        by_id = {item.id: item for item in profile.scholarship_targets}

        self.assertTrue(by_id["csc"].active)
        self.assertFalse(by_id["gks"].active)
        self.assertFalse(by_id["erasmus"].active)
        self.assertEqual(by_id["csc"].degree_level, "master")
        self.assertEqual(by_id["csc"].funding, "fully_funded")

    def test_education_is_recorded_without_invented_scores(self) -> None:
        profile = load_profile(get_profile_path())
        education = filled_education(profile)

        self.assertEqual(len(education), 1)
        self.assertEqual(education[0].degree, "BS Software Engineering")
        self.assertEqual(education[0].university, "Jinnah University for Women (JUW)")
        self.assertEqual(education[0].country, "Pakistan")
        self.assertEqual(education[0].graduation_year, 2026)
        self.assertIsNone(education[0].gpa)
        self.assertEqual(profile.personal.full_name, "")
        self.assertEqual(profile.personal.email, "")
        self.assertIn("Python", profile.technical_skills)
        self.assertIn("Basic Machine Learning / AI concepts", profile.technical_skills)
        self.assertGreater(len(profile.technical_skills), 1)

    def test_one_city_mode_requires_exactly_one_city(self) -> None:
        profile = UserProfile(preferred_cities=CityFilter(mode="one", cities=["City One", "City Two"]))
        errors, _warnings = validate_profile(profile)

        self.assertTrue(any("exactly one" in error for error in errors))

    def test_multiple_city_mode_requires_two_cities(self) -> None:
        profile = UserProfile(preferred_cities=CityFilter(mode="multiple", cities=["Only One"]))
        errors, _warnings = validate_profile(profile)

        self.assertTrue(any("at least two" in error for error in errors))

    def test_save_and_load_round_trip(self) -> None:
        profile = load_profile(get_profile_path())
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "user_profile.json"
            save_profile(profile, path)
            saved = json.loads(path.read_text(encoding="utf-8"))
            loaded = load_profile(path)

        self.assertNotIn("load_warnings", saved)
        self.assertEqual(loaded.preferred_fields, profile.preferred_fields)
        self.assertEqual(loaded.preferred_cities.mode, "any")
        self.assertEqual(loaded.education[0].graduation_year, 2026)
        self.assertIsNone(loaded.education[0].gpa)
        self.assertEqual(loaded.activities[0].name, profile.activities[0].name)

    def test_program_prints_the_broad_profile(self) -> None:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main([])

        text = buffer.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("Computer Science", text)
        self.assertIn("City filter: any (no limit).", text)
        self.assertIn("Chinese Government Scholarship (CSC)", text)
        self.assertIn("matching module is ready", text)


if __name__ == "__main__":
    unittest.main()

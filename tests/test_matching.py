"""Tests for filter rules and research overlap scoring.

Professor objects in this file are test inputs for the matching function.
They are not a professor directory.
"""

from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import BROAD_ACADEMIC_FIELDS
from app.matching import match_professor
from app.profile import load_profile
from app.models import (
    CityFilter,
    DepartmentFilter,
    FieldFilter,
    Professor,
    ResearchInterest,
    ResearchPaper,
    UniversityFilter,
    UserProfile,
)


def _professor(**kwargs: object) -> Professor:
    return Professor(**kwargs)  # type: ignore[arg-type]


class MatchingTests(unittest.TestCase):
    def test_any_city_mode_matches_every_city(self) -> None:
        profile = UserProfile(
            research_interests=[ResearchInterest("Software Engineering")],
            preferred_cities=CityFilter(mode="any", cities=[]),
        )
        first = match_professor(profile, _professor(city="City One", research_areas=["Software Engineering"]))
        second = match_professor(profile, _professor(city="City Two", research_areas=["Software Engineering"]))

        self.assertTrue(first.passed_filters)
        self.assertTrue(second.passed_filters)

    def test_one_city_mode_matches_only_that_city(self) -> None:
        profile = UserProfile(
            research_interests=[ResearchInterest("Software Engineering")],
            preferred_cities=CityFilter(mode="one", cities=["City One"]),
        )
        inside = match_professor(profile, _professor(city="city one", research_areas=["Software Engineering"]))
        outside = match_professor(profile, _professor(city="City Two", research_areas=["Software Engineering"]))

        self.assertTrue(inside.passed_filters)
        self.assertFalse(outside.passed_filters)
        self.assertIn("City Two", " ".join(outside.filter_notes))

    def test_multiple_cities_match_any_selected_city(self) -> None:
        profile = UserProfile(
            research_interests=[ResearchInterest("Database Systems")],
            preferred_cities=CityFilter(mode="multiple", cities=["City One", "City Two"]),
        )
        matched = match_professor(profile, _professor(city="City Two", research_areas=["Database Systems"]))
        missed = match_professor(profile, _professor(city="City Three", research_areas=["Database Systems"]))

        self.assertTrue(matched.passed_filters)
        self.assertFalse(missed.passed_filters)

    def test_university_filter_uses_the_full_name(self) -> None:
        profile = UserProfile(
            research_interests=[ResearchInterest("Information Systems")],
            preferred_universities=UniversityFilter(mode="one", universities=["Example University"]),
        )
        result = match_professor(
            profile,
            _professor(university="Other University", research_areas=["Information Systems"]),
        )

        self.assertFalse(result.passed_filters)

    def test_department_phrase_can_match_a_longer_department_name(self) -> None:
        profile = UserProfile(
            research_interests=[ResearchInterest("Software Engineering")],
            department_filter=DepartmentFilter(mode="one", departments=["Computer Science"]),
        )
        result = match_professor(
            profile,
            _professor(
                department="School of Computer Science and Technology",
                research_areas=["Software Engineering"],
            ),
        )

        self.assertTrue(result.passed_filters)

    def test_field_filter_limits_results_when_it_is_active(self) -> None:
        profile = UserProfile(
            research_interests=[ResearchInterest("Data Science")],
            field_filter=FieldFilter(mode="multiple", fields=["Data Science", "Database Systems"]),
        )
        outside = match_professor(profile, _professor(department="Physics", research_areas=["Optics"]))
        inside = match_professor(profile, _professor(research_areas=["Applied Data Science"]))

        self.assertFalse(outside.passed_filters)
        self.assertTrue(inside.passed_filters)

    def test_extra_fields_do_not_lower_a_real_match(self) -> None:
        professor = _professor(research_areas=["Software Engineering"])
        narrow = UserProfile(research_interests=[ResearchInterest("Software Engineering")])
        broad = UserProfile(
            research_interests=[ResearchInterest(name) for name in BROAD_ACADEMIC_FIELDS],
            preferred_fields=list(BROAD_ACADEMIC_FIELDS),
        )

        narrow_result = match_professor(narrow, professor)
        broad_result = match_professor(broad, professor)

        self.assertEqual(narrow_result.research_score, 25)
        self.assertEqual(broad_result.research_score, narrow_result.research_score)
        self.assertIn("Software Engineering", broad_result.matched_interests)
        self.assertIn("Software Engineering", broad_result.matched_fields)

    def test_second_area_and_recent_paper_raise_the_score(self) -> None:
        current_year = date.today().year
        profile = UserProfile(
            research_interests=[
                ResearchInterest("Software Engineering"),
                ResearchInterest("Database Systems", priority="high"),
            ]
        )
        professor = _professor(
            research_areas=["Software Engineering", "Database Systems"],
            papers=[
                ResearchPaper(
                    title="A Study of Database Systems",
                    year=current_year - 1,
                    abstract="Database systems for software records.",
                )
            ],
        )

        result = match_professor(profile, professor)

        # High-priority hit is 50, the second area adds 15, and a recent paper adds 10.
        self.assertEqual(result.research_score, 75)
        self.assertTrue(any("A Study of Database Systems" in note for note in result.paper_notes))

    def test_old_paper_is_listed_without_the_recent_bonus(self) -> None:
        profile = UserProfile(research_interests=[ResearchInterest("Cloud Computing")])
        professor = _professor(
            research_areas=["Cloud Computing"],
            papers=[
                ResearchPaper(title="Old Cloud Computing Notes", year=date.today().year - 10)
            ],
        )

        result = match_professor(profile, professor)

        self.assertEqual(result.research_score, 25)
        self.assertTrue(any("Old Cloud Computing Notes" in note for note in result.paper_notes))

    def test_short_keyword_matches_exactly_and_not_inside_another_word(self) -> None:
        profile = UserProfile(
            research_interests=[ResearchInterest("Artificial Intelligence", keywords=["AI"])]
        )
        exact = match_professor(profile, _professor(keywords=["AI"]))
        inside_word = match_professor(profile, _professor(research_areas=["Available systems"]))

        self.assertGreater(exact.research_score, 0)
        self.assertEqual(inside_word.research_score, 0)

    def test_data_keyword_does_not_match_database(self) -> None:
        profile = UserProfile(research_interests=[ResearchInterest("Data", keywords=["data"])])
        result = match_professor(profile, _professor(research_areas=["Database Systems"]))

        self.assertEqual(result.research_score, 0)

    def test_failed_city_filter_still_reports_the_research_score(self) -> None:
        profile = UserProfile(
            research_interests=[ResearchInterest("Human-Computer Interaction")],
            preferred_cities=CityFilter(mode="one", cities=["City One"]),
        )
        result = match_professor(
            profile,
            _professor(city="City Two", research_areas=["Human-Computer Interaction"]),
        )

        self.assertFalse(result.passed_filters)
        self.assertEqual(result.research_score, 25)
        self.assertIn("Does not pass your filters.", result.summary)

    def test_generic_computer_science_alone_is_not_a_strong_match(self) -> None:
        profile = load_profile()
        professor = _professor(
            name="Dinghua Li",
            university="Beijing Institute of Technology",
            department="School of Computer Science and Technology",
            research_areas=[
                "Computer science",
                "Theoretical computer science",
                "Materials science",
                "Chemistry",
            ],
            keywords=["Computer science"],
            papers=[
                ResearchPaper(
                    title="Polymer synthesis notes",
                    abstract="",
                    keywords=["Computer science", "Polymorphism (computer science)", "Organic chemistry"],
                )
            ],
        )

        result = match_professor(profile, professor)

        self.assertEqual(result.research_score, 0)
        self.assertEqual(result.research_confidence, "low")
        self.assertEqual(result.matched_areas, [])
        self.assertEqual(result.evidence, [])

    def test_data_and_database_research_is_a_stronger_match(self) -> None:
        profile = load_profile()
        generic = match_professor(
            profile,
            _professor(research_areas=["Computer science"], keywords=["Computer science"]),
        )
        specific = match_professor(
            profile,
            _professor(
                research_areas=["Data Science", "Database Systems"],
                source_urls=["https://openalex.org/A100"],
            ),
        )

        self.assertGreater(specific.research_score, generic.research_score)
        self.assertIn("Data Science", specific.matched_areas)
        self.assertIn("Database Systems", specific.matched_areas)
        self.assertIn(specific.research_confidence, {"medium", "high"})

    def test_software_engineering_research_is_a_stronger_match(self) -> None:
        profile = load_profile()
        generic = match_professor(profile, _professor(research_areas=["Computer science"]))
        specific = match_professor(
            profile,
            _professor(research_areas=["Software Engineering"]),
        )

        self.assertGreater(specific.research_score, generic.research_score)
        self.assertEqual(specific.matched_areas, ["Software Engineering"])
        self.assertIn(specific.research_confidence, {"medium", "high"})

    def test_multiple_related_papers_increase_confidence(self) -> None:
        profile = UserProfile(research_interests=[ResearchInterest("Database Systems", priority="high")])
        one_paper = match_professor(
            profile,
            _professor(
                papers=[ResearchPaper(title="A Survey of Database Systems", year=2012, abstract="")]
            ),
        )
        two_papers = match_professor(
            profile,
            _professor(
                papers=[
                    ResearchPaper(title="A Survey of Database Systems", year=2012, abstract=""),
                    ResearchPaper(title="Query Processing in Database Systems", year=2014, abstract=""),
                ]
            ),
        )

        self.assertEqual(one_paper.research_score, two_papers.research_score)
        self.assertEqual(one_paper.research_confidence, "medium")
        self.assertEqual(two_papers.research_confidence, "high")
        self.assertEqual(two_papers.matched_areas, ["Database Systems"])

    def test_missing_abstract_does_not_create_fake_evidence(self) -> None:
        profile = load_profile()
        result = match_professor(
            profile,
            _professor(
                research_areas=["Data Science"],
                papers=[
                    ResearchPaper(
                        title="Notes on laboratory samples",
                        year=2020,
                        abstract="",
                        source_url="https://openalex.org/Wmissing",
                    )
                ],
            ),
        )

        self.assertIn("Data Science", result.matched_areas)
        self.assertTrue(all("paper abstract" not in line for line in result.evidence))
        self.assertNotIn("https://openalex.org/Wmissing", "\n".join(result.evidence))
        self.assertTrue(all("laboratory" not in line.casefold() for line in result.evidence))

    def test_match_explanation_uses_only_stored_evidence(self) -> None:
        profile = load_profile()
        interests = ["Database Systems"]
        title = "A Study of Database Systems"
        paper_keyword = "database management"
        unmatched = "Organic chemistry"
        source = "https://openalex.org/A200"
        paper_source = "https://openalex.org/W200"
        result = match_professor(
            profile,
            _professor(
                research_areas=interests,
                source_urls=[source],
                papers=[
                    ResearchPaper(
                        title=title,
                        abstract="",
                        year=2021,
                        keywords=[paper_keyword, unmatched],
                        source_url=paper_source,
                    )
                ],
            ),
        )

        stored = " ".join([*interests, title, paper_keyword]).casefold()
        self.assertEqual(result.matched_areas, ["Database Systems"])
        self.assertTrue(result.evidence)
        for line in result.evidence:
            quoted = line.split('"')[1::2]
            self.assertTrue(quoted)
            for snippet in quoted:
                self.assertIn(snippet.casefold(), stored)
            if "(" in line:
                self.assertTrue(
                    line.endswith(f"({source})") or line.endswith(f"({paper_source})")
                )
        self.assertNotIn("chemistry", " ".join(result.evidence).casefold())
        self.assertNotIn("https://example.com", " ".join(result.evidence))


if __name__ == "__main__":
    unittest.main()

"""Project settings and the starter academic scope.

This module knows where the project files live and which broad fields the
starter profile includes. It does not store personal details or professors.
"""

from __future__ import annotations

import os
from pathlib import Path

# app/config.py -> project root (the csc-professor-agent folder).
PROJECT_ROOT = Path(__file__).resolve().parents[1]

APP_NAME = "CSC Professor Research Agent"
SCHEMA_VERSION = 1

# any  = do not limit this filter
# one  = the profile must list exactly one value, and only that value matches
# multiple = the profile must list at least two values, and any of them can match
FILTER_MODES = ("any", "one", "multiple")

PRIORITY_LEVELS = ("high", "medium", "low")

# Papers from this many years ago, through the current year, count as recent.
RECENT_PAPER_YEARS = 5

# The starter profile is intentionally broad. This is not a ranking.
# Add other closely related CS/IT areas in user_profile.json when you want them.
BROAD_ACADEMIC_FIELDS = (
    "Computer Science",
    "Software Engineering",
    "Information Technology",
    "Data Science",
    "Data Analytics",
    "Artificial Intelligence",
    "Machine Learning",
    "Natural Language Processing",
    "Information Systems",
    "Database Systems",
    "Software Systems",
    "Web/Mobile Application Development",
    "Cloud Computing",
    "Human-Computer Interaction",
    "Healthcare IT / Health Informatics",
)

# Programs this project is meant to grow into.
# Only CSC is active in the starter profile.
SCHOLARSHIP_CATALOG = (
    {
        "id": "csc",
        "name": "Chinese Government Scholarship (CSC)",
        "initial_focus": True,
    },
    {
        "id": "gks",
        "name": "Global Korea Scholarship (GKS)",
        "initial_focus": False,
    },
    {
        "id": "erasmus",
        "name": "Erasmus Mundus",
        "initial_focus": False,
    },
)


def load_environment() -> None:
    """Load a local .env file when python-dotenv is installed.

    The project still runs if the package is missing. Real environment
    variables are left as they are.
    """

    try:
        from dotenv import load_dotenv
    except ImportError:
        return

    load_dotenv(PROJECT_ROOT / ".env")


def get_data_dir() -> Path:
    """Return the data folder, honoring DATA_DIR when it is set."""

    load_environment()
    data_dir = Path(os.environ.get("DATA_DIR", "data"))
    if not data_dir.is_absolute():
        data_dir = PROJECT_ROOT / data_dir
    return data_dir


def get_profile_path() -> Path:
    """Return the user profile path, honoring DATA_DIR and PROFILE_FILENAME."""

    filename = os.environ.get("PROFILE_FILENAME", "user_profile.json")
    return get_data_dir() / filename


def get_search_config_path() -> Path:
    """Return the discovery search configuration path."""

    filename = os.environ.get("SEARCH_CONFIG_FILENAME", "search_config.json")
    return get_data_dir() / filename


def get_professors_path() -> Path:
    """Return the JSON file that stores discovered professor records."""

    filename = os.environ.get("PROFESSORS_FILENAME", "professors.json")
    return get_data_dir() / filename


def selection_errors(label: str, mode: str, values: list[str]) -> list[str]:
    """Check that a location or department filter has a usable mode and list."""

    cleaned_mode = mode.strip().casefold()
    chosen = [item.strip() for item in values if item and item.strip()]
    if cleaned_mode not in FILTER_MODES:
        return [f"{label}.mode must be any, one, or multiple."]
    if cleaned_mode == "one" and len(chosen) != 1:
        return [f"{label}.mode is one, so provide exactly one value."]
    if cleaned_mode == "multiple" and len(chosen) < 2:
        return [f"{label}.mode is multiple, so provide at least two values."]
    return []

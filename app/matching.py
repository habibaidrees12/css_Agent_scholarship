"""Compare a user profile with one professor record.

Pass in a Professor object when you have a real record. This module does
not ship professor names, does not scrape websites, and does not send email.

A broad profile is not penalized. The score comes from the areas that
actually overlap. Extra areas that do not overlap are ignored, not used as
a divisor.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from app.config import RECENT_PAPER_YEARS
from app.models import MatchResult, Professor, ResearchPaper, UserProfile

# Points for the strongest overlapping area. Further overlaps add a smaller step.
# The total stops at MAX_SCORE.
PRIORITY_POINTS = {
    "high": 50,
    "medium": 35,
    "low": 25,
}
UNSPECIFIED_POINTS = 25
EXTRA_MATCH_POINTS = 15
RECENT_PAPER_BONUS = 10
MAX_SCORE = 100
# The profile label "computer science" is too broad to count as a specialty.
_GENERIC_CS = "computer science"
CONFIDENCE_RANK = {"low": 1, "medium": 2, "high": 3}
_PRIORITY_RANK = {None: 0, "low": 1, "medium": 2, "high": 3}


@dataclass
class _Evidence:
    """One stored snippet that supports a profile area."""

    area: str
    kind: str
    text: str
    source_url: str = ""
    paper_year: int | None = None
    paper_key: str = ""


@dataclass
class _Term:
    """One profile area, after duplicate names have been combined."""

    name: str
    priority: str | None = None
    keywords: list[str] = field(default_factory=list)
    from_interest: bool = False
    from_field: bool = False


def match_professor(profile: UserProfile, professor: Professor) -> MatchResult:
    """Score one professor against the profile and apply the active filters."""

    filter_notes, passed_filters = _apply_filters(profile, professor)
    terms = _profile_terms(profile)
    matched_terms, paper_notes, evidence, supporting_papers = _find_overlaps(terms, professor)
    used_recent_paper = any(
        item.paper_year is not None and item.paper_year >= date.today().year - RECENT_PAPER_YEARS
        for item in evidence
        if item.kind.startswith("paper")
    )
    score = _score(profile, matched_terms, used_recent_paper, has_terms=bool(terms))
    confidence = _confidence(matched_terms, supporting_papers)
    matched_interests = [item.name for item in matched_terms if item.from_interest]
    matched_fields = [item.name for item in matched_terms if item.from_field]
    matched_areas = [item.name for item in matched_terms]
    summary = _summary(
        passed_filters=passed_filters,
        score=score,
        matched_names=matched_areas,
        has_terms=bool(terms),
        confidence=confidence,
    )
    display_name = professor.name or "Unnamed professor"
    return MatchResult(
        professor_name=display_name,
        university=professor.university,
        passed_filters=passed_filters,
        filter_notes=filter_notes,
        research_score=score,
        matched_interests=matched_interests,
        matched_fields=matched_fields,
        paper_notes=paper_notes,
        summary=summary,
        matched_areas=matched_areas,
        evidence=[_format_evidence(item) for item in evidence],
        research_confidence=confidence,
    )


def _apply_filters(profile: UserProfile, professor: Professor) -> tuple[list[str], bool]:
    checks = [
        _check_exact_filter(
            "Country",
            profile.preferred_countries.mode,
            profile.preferred_countries.countries,
            professor.country,
        ),
        _check_exact_filter(
            "City",
            profile.preferred_cities.mode,
            profile.preferred_cities.cities,
            professor.city,
        ),
        _check_exact_filter(
            "University",
            profile.preferred_universities.mode,
            profile.preferred_universities.universities,
            professor.university,
        ),
        _check_contains_filter(
            "Department",
            profile.department_filter.mode,
            profile.department_filter.departments,
            [professor.department],
        ),
        _check_contains_filter(
            "Field",
            profile.field_filter.mode,
            profile.field_filter.fields,
            [professor.department, *professor.research_areas, *professor.keywords],
        ),
    ]
    notes = [note for _, note in checks]
    passed = all(ok for ok, _ in checks)
    return notes, passed


def _check_exact_filter(
    label: str, mode: str, selected: list[str], actual: str
) -> tuple[bool, str]:
    """Match a city, country, or university by the same spelling, ignoring case."""

    cleaned_mode = mode.strip().casefold()
    chosen = [item.strip() for item in selected if item and item.strip()]
    if cleaned_mode == "any":
        return True, f"{label} filter: any (no limit)."
    if cleaned_mode not in {"one", "multiple"}:
        return False, f"{label} filter: mode '{mode}' is not supported."
    if cleaned_mode == "one" and len(chosen) != 1:
        return False, f"{label} filter: mode one needs exactly one value in the profile."
    if cleaned_mode == "multiple" and len(chosen) < 2:
        return False, f"{label} filter: mode multiple needs at least two values in the profile."
    if not actual.strip():
        return False, f"{label} filter is active, and this professor record has no {label.lower()}."

    nice = ", ".join(chosen)
    matched = any(_place_key(actual) == _place_key(item) for item in chosen)
    if cleaned_mode == "one" and matched:
        return True, f"{label} filter: matched the selected value ({chosen[0]})."
    if cleaned_mode == "one":
        return False, (
            f"{label} filter: {actual} is not the selected {label.lower()} ({chosen[0]})."
        )
    if matched:
        return True, f"{label} filter: {actual} is in the selected list ({nice})."
    return False, f"{label} filter: {actual} is not in the selected list ({nice})."


def _check_contains_filter(
    label: str, mode: str, selected: list[str], haystacks: list[str]
) -> tuple[bool, str]:
    """Match a department or field when the selected phrase appears in the record."""

    cleaned_mode = mode.strip().casefold()
    chosen = [item.strip() for item in selected if item and item.strip()]
    if cleaned_mode == "any":
        return True, f"{label} filter: any (no limit)."
    if cleaned_mode not in {"one", "multiple"}:
        return False, f"{label} filter: mode '{mode}' is not supported."
    if cleaned_mode == "one" and len(chosen) != 1:
        return False, f"{label} filter: mode one needs exactly one value in the profile."
    if cleaned_mode == "multiple" and len(chosen) < 2:
        return False, f"{label} filter: mode multiple needs at least two values in the profile."

    usable = [item for item in haystacks if item and item.strip()]
    if not usable:
        return False, f"{label} filter is active, and this professor record has no {label.lower()}."

    for selected_value in chosen:
        for candidate in usable:
            if _contains_term(candidate, selected_value):
                return True, (
                    f"{label} filter: {candidate} matches the selected value ({selected_value})."
                )
    shown = ", ".join(usable)
    wanted = ", ".join(chosen)
    return False, f"{label} filter: {shown} does not match the selected list ({wanted})."


def _profile_terms(profile: UserProfile) -> list[_Term]:
    """Combine interests, preferred fields, and priority fields without double-counting."""

    combined: dict[str, _Term] = {}

    def add(name: str, priority: str | None, keywords: list[str], source: str) -> None:
        if not name.strip():
            return
        key = _phrase(name) or _place_key(name)
        current = combined.get(key)
        if current is None:
            current = _Term(name=name, priority=priority, keywords=list(keywords))
            combined[key] = current
        else:
            current.priority = _higher_priority(current.priority, priority)
            for keyword in keywords:
                if keyword not in current.keywords:
                    current.keywords.append(keyword)
        if source == "interest":
            current.from_interest = True
        if source == "field":
            current.from_field = True

    for interest in profile.research_interests:
        add(interest.name, interest.priority, interest.keywords, "interest")
    for name in profile.preferred_fields:
        add(name, None, [], "field")
    for name in profile.priority_fields:
        add(name, "high", [], "field")
    return list(combined.values())


def _find_overlaps(
    terms: list[_Term], professor: Professor
) -> tuple[list[_Term], list[str], list[_Evidence], int]:
    """Collect specific evidence. A bare computer-science label does not count."""

    matched: list[_Term] = []
    evidence: list[_Evidence] = []
    paper_notes: list[str] = []
    matched_a_paper = False
    seen_papers: set[str] = set()
    supporting_paper_keys: set[str] = set()
    author_url = professor.source_urls[0] if professor.source_urls else ""

    for term in terms:
        term_evidence = _evidence_for_term(term, professor, author_url)
        if not term_evidence:
            continue
        matched.append(term)
        evidence.extend(term_evidence)
        for item in term_evidence:
            if not item.kind.startswith("paper"):
                continue
            matched_a_paper = True
            if item.paper_key:
                supporting_paper_keys.add(item.paper_key)
            label = _paper_label(
                ResearchPaper(title=item.text, year=item.paper_year),
                term.name,
            )
            if item.kind != "paper title":
                continue
            if label not in seen_papers:
                seen_papers.add(label)
                paper_notes.append(label)

    if not professor.papers:
        paper_notes.append(
            "No papers were included on this professor record, so recent-paper analysis had nothing to read."
        )
    elif not matched_a_paper:
        paper_notes.append(
            "Papers were included, and none of their titles, abstracts, or keywords matched your areas."
        )
    return matched, paper_notes, evidence, len(supporting_paper_keys)


def _score(
    profile: UserProfile,
    matched_terms: list[_Term],
    used_recent_paper: bool,
    has_terms: bool,
) -> int:
    if not has_terms or not matched_terms:
        return 0
    weights = sorted((_term_weight(profile, term) for term in matched_terms), reverse=True)
    total = weights[0] + EXTRA_MATCH_POINTS * (len(weights) - 1)
    if used_recent_paper:
        total += RECENT_PAPER_BONUS
    return min(MAX_SCORE, total)


def _term_weight(profile: UserProfile, term: _Term) -> int:
    points = PRIORITY_POINTS.get(term.priority or "", UNSPECIFIED_POINTS)
    if term.priority is None:
        points = UNSPECIFIED_POINTS
    priority_names = {_phrase(name) for name in profile.priority_fields}
    if _phrase(term.name) in priority_names:
        points = max(points, PRIORITY_POINTS["high"])
    return points


def _summary(
    passed_filters: bool,
    score: int,
    matched_names: list[str],
    has_terms: bool,
    confidence: str,
) -> str:
    filter_text = "Passes your filters." if passed_filters else "Does not pass your filters."
    if not has_terms:
        research = "Add research interests or preferred fields before comparing professors."
    elif score == 0:
        research = "No specific research-area overlap was found."
    else:
        shown = ", ".join(matched_names[:5])
        extra = ""
        if len(matched_names) > 5:
            extra = f" and {len(matched_names) - 5} more"
        research = (
            f"Research overlap score: {score} out of 100. "
            f"Confidence: {confidence}. Matched areas: {shown}{extra}."
        )
    return f"{filter_text} {research}"


def _evidence_for_term(term: _Term, professor: Professor, author_url: str) -> list[_Evidence]:
    """Collect stored snippets for one profile area.

    The profile area named Computer Science is generic. It does not add
    overlap by itself, including when it appears in a qualifier such as
    "Polymorphism (computer science)". Specific areas still match on their
    own phrases.
    """

    if _phrase(term.name) == _GENERIC_CS:
        return []
    phrases = [term.name, *term.keywords]
    found: list[_Evidence] = []
    seen: set[tuple[str, str]] = set()

    def add(
        kind: str,
        text: str,
        source_url: str,
        paper_year: int | None = None,
        paper_key: str = "",
    ) -> None:
        if not text or not text.strip():
            return
        if not any(_term_matches_parts(_phrase(text), [text], phrase) for phrase in phrases):
            return
        key = (kind, text.strip())
        if key in seen:
            return
        seen.add(key)
        found.append(
            _Evidence(
                area=term.name,
                kind=kind,
                text=text.strip(),
                source_url=source_url,
                paper_year=paper_year,
                paper_key=paper_key,
            )
        )

    if professor.department.strip():
        add("department", professor.department, author_url)
    for area in professor.research_areas:
        add("research interests", area, author_url)
    for keyword in professor.keywords:
        add("research interests", keyword, author_url)
    for paper in professor.papers:
        paper_url = paper.source_url or paper.url
        paper_key = f"{paper.title}|{paper.year}"
        add("paper title", paper.title, paper_url, paper.year, paper_key)
        if paper.abstract.strip():
            quote = _first_matching_phrase(phrases, paper.abstract)
            if quote:
                add("paper abstract", quote, paper_url, paper.year, paper_key)
        for keyword in paper.keywords:
            add("paper keywords", keyword, paper_url, paper.year, paper_key)
    return found


def _first_matching_phrase(phrases: list[str], text: str) -> str:
    for phrase in phrases:
        if _term_matches_parts(_phrase(text), [text], phrase):
            return phrase
    return ""


def _confidence(matched_terms: list[_Term], supporting_papers: int) -> str:
    """Specific areas and repeated papers raise confidence. A generic label does not."""

    if not matched_terms:
        return "low"
    if len(matched_terms) >= 2 or supporting_papers >= 2:
        return "high"
    return "medium"


def _format_evidence(item: _Evidence) -> str:
    if item.kind == "paper title":
        line = f'paper title contains "{item.text}"'
    elif item.kind == "paper abstract":
        line = f'paper abstract contains "{item.text}"'
    elif item.kind == "paper keywords":
        line = f'"{item.text}" found in paper keywords'
    elif item.kind == "department":
        line = f'"{item.text}" found in department'
    else:
        line = f'"{item.text}" found in research interests'
    if item.source_url:
        return f"{line} ({item.source_url})"
    return line


def _paper_label(paper: ResearchPaper, area: str) -> str:
    year = str(paper.year) if paper.year is not None else "Undated"
    title = paper.title or "Untitled paper"
    return f"{year}: {title} (matched {area})"


def _is_recent(paper: ResearchPaper) -> bool:
    if paper.year is None:
        return False
    return paper.year >= date.today().year - RECENT_PAPER_YEARS


def _raw_parts(professor: Professor) -> list[str]:
    parts = [professor.department, *professor.research_areas, *professor.keywords]
    for paper in professor.papers:
        parts.extend(_paper_parts(paper))
    return parts


def _paper_parts(paper: ResearchPaper) -> list[str]:
    return [paper.title, paper.abstract, *paper.keywords]


def _term_matches(professor: Professor, haystack: str, term: str) -> bool:
    return _term_matches_parts(haystack, _raw_parts(professor), term)


def _term_matches_parts(haystack: str, raw_parts: list[str], term: str) -> bool:
    """Match a full phrase on word boundaries.

    Very short labels such as "AI" match only when a research area or keyword
    is exactly that label. They do not match just because those letters appear
    inside a longer word.
    """

    if _phrase_in_text(haystack, term):
        return True
    if len(_phrase(term)) < 3:
        return any(_place_key(part) == _place_key(term) for part in raw_parts if part.strip())
    return False


def _contains_term(haystack: str, selected: str) -> bool:
    if _place_key(haystack) == _place_key(selected):
        return True
    return _phrase_in_text(_phrase(haystack), selected)


def _phrase_haystack(parts: list[str]) -> str:
    return " | ".join(_phrase(part) for part in parts if part and part.strip())


def _phrase(value: str) -> str:
    return " ".join(re.findall(r"[0-9a-z]+", value.casefold()))


def _phrase_in_text(haystack: str, term: str) -> bool:
    needle = _phrase(term)
    if len(needle) < 3:
        return False
    pattern = rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])"
    return re.search(pattern, haystack) is not None


def _place_key(value: str) -> str:
    return " ".join(value.casefold().split())


def _higher_priority(left: str | None, right: str | None) -> str | None:
    if _PRIORITY_RANK.get(left, 0) >= _PRIORITY_RANK.get(right, 0):
        return left
    return right

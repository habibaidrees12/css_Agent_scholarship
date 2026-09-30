"""Personalized CSC email drafts and an application tracker.

Drafts are written for a person to review. Nothing in this module sends mail.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app.config import RECENT_PAPER_YEARS, get_data_dir
from app.models import MatchResult, PaperRecord, ProfessorRecord, UserProfile
from app.profile import filled_education, filled_projects, load_profile
from app.verification import published_email

SENDS_EMAIL = False
TRACKER_STATUSES = (
    "not_contacted",
    "draft_ready",
    "approved",
    "sent",
    "replied",
    "interested",
    "not_interested",
    "no_response",
    "follow_up_due",
)

_MEDSCRIBE_AREAS = {
    "Software Engineering": "software engineering",
    "Web/Mobile Application Development": "mobile application development",
    "Database Systems": "databases and data processing",
    "Data Science": "data processing",
    "Data Analytics": "data processing",
    "Information Systems": "information systems",
    "Software Systems": "software systems",
    "Artificial Intelligence": "applied AI features inside a software project",
    "Machine Learning": "applied machine-learning features inside a software project",
    "Natural Language Processing": "speech, transcription, and medical text processing",
    "Healthcare IT / Health Informatics": "healthcare software and clinical documentation",
}


@dataclass
class TrackerEntry:
    professor: str
    university: str
    email: str | None
    match_score: int
    email_status: str = "not_contacted"
    date_contacted: str | None = None
    follow_up_date: str | None = None
    response_status: str | None = None
    notes: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "professor": self.professor,
            "university": self.university,
            "email": self.email,
            "match_score": self.match_score,
            "email_status": self.email_status,
            "date_contacted": self.date_contacted,
            "follow_up_date": self.follow_up_date,
            "response_status": self.response_status,
            "notes": self.notes,
        }


def recent_papers(record: ProfessorRecord, *, today: date | None = None) -> list[dict[str, object]]:
    """Return papers already stored from the last five years.

    Missing abstracts, venues, and DOIs stay null. This does not create them.
    """

    current = today or date.today()
    cutoff = current.year - RECENT_PAPER_YEARS
    papers: list[dict[str, object]] = []
    for paper in record.papers:
        if paper.year is None or paper.year < cutoff:
            continue
        papers.append(
            {
                "title": paper.title,
                "year": paper.year,
                "venue": None,
                "abstract": paper.abstract,
                "keywords": list(paper.keywords),
                "paper_url": paper.paper_url,
                "doi": paper.paper_url if paper.paper_url and "doi.org" in paper.paper_url.casefold() else None,
                "source_url": paper.source_url,
            }
        )
    return papers


def medscribe_relevance(matched_areas: list[str], profile: UserProfile | None = None) -> str | None:
    """Describe MedScribeAI only as project experience that overlaps this professor."""

    if profile is not None and not any(item.title == "MedScribeAI" for item in filled_projects(profile)):
        return None
    concepts = []
    for area in matched_areas:
        concept = _MEDSCRIBE_AREAS.get(area)
        if concept and concept not in concepts:
            concepts.append(concept)
    if not concepts:
        return None
    joined = _join_concepts(concepts)
    return (
        "My final-year project, MedScribeAI, is relevant as project experience with "
        f"{joined}. It is a software project that uses Python, FastAPI, React Native, "
        "TypeScript, and a database. I am not presenting it as research expertise in "
        "medicine or as a claim that I am an artificial-intelligence researcher."
    )


_GENERIC_INTERESTS = {
    "computer science",
    "artificial intelligence",
    "mathematics",
    "physics",
    "algorithm",
    "engineering",
    "chemistry",
    "biology",
    "medicine",
    "quantum mechanics",
}
_STUDENT_HINTS = ("data", "database", "software", "information", "web", "mining", "retriev")
_QUOTE_RE = re.compile(r'"([^"]+)"|“([^”]+)”')
_URL_RE = re.compile(r"https?://[^\s)]+")


def draft_csc_email(
    profile: UserProfile,
    record: ProfessorRecord,
    match: MatchResult,
    *,
    today: date | None = None,
) -> dict[str, object] | None:
    """Write one unsent CSC draft from stored evidence. No address is invented."""

    areas = _select_research_areas(record, match)
    if not areas:
        return None
    education = filled_education(profile)
    degree = education[0].degree if education else "BS Software Engineering"
    school = education[0].university if education else "Jinnah University for Women (JUW)"
    year = education[0].graduation_year if education else None
    year_text = f", with expected graduation in {year}" if year else ""
    department = (record.department or record.official_department or "").strip()
    department_text = f", {department}" if department else ""
    shown = _join_concepts(areas)
    kind = _focus_kind(areas)
    papers = _paper_evidence(record, match, today=today)
    paper_text = _paper_paragraph(papers)
    project_text = _project_sentence(kind, areas, profile)
    project_block = f"\n\n{project_text}" if project_text else ""
    paper_block = f"\n\n{paper_text}" if paper_text else ""
    recipient = published_email(record.public_email)
    body = (
        f"Dear Professor {record.name},\n\n"
        f"I am writing to you at {record.university}{department_text}. "
        "I am interested in applying for the Chinese Government Scholarship (CSC), and I would like "
        "to ask whether you would be willing to supervise or support my application if a proposed "
        "project aligns with your group's work.\n\n"
        f"I am completing a {degree} at {school}{year_text}. "
        f"{_connection_sentence(kind, shown)}"
        f"{paper_block}"
        f"{project_block}\n\n"
        "I can send my curriculum vitae and transcripts if that would be useful.\n\n"
        "Sincerely,"
    )
    if profile.personal.full_name.strip():
        body += f"\n{profile.personal.full_name.strip()}"
    subject_area = areas[0]
    subject = f"CSC scholarship inquiry: {subject_area} at {record.university}"
    urls = _evidence_urls(record, match, papers)
    return {
        "professor_name": record.name,
        "university": record.university,
        "recipient_email": recipient,
        "subject": subject,
        "body": body,
        "paper_evidence": papers,
        "matched_research_areas": areas,
        "evidence_urls": urls,
        "source_urls": urls,
        "draft_status": "draft",
        "gmail_preparation": {
            "provider": "gmail",
            "to": recipient,
            "subject": subject,
            "sent": False,
        },
    }


def generate_shortlist_drafts(
    *,
    shortlist_file: Path | None = None,
    destination: Path | None = None,
    profile: UserProfile | None = None,
) -> list[dict[str, object]]:
    """Draft from the saved shortlist. This does not read or write professors.json."""

    source = shortlist_file or shortlist_path()
    rows = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError("professor_shortlist.json must contain a list.")
    user = profile or load_profile()
    drafts: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("verification_status") != "officially_verified":
            continue
        record = ProfessorRecord(
            name=str(row.get("name") or ""),
            university=str(row.get("university") or ""),
            department=row.get("department") if isinstance(row.get("department"), str) else None,
            public_email=row.get("public_email") if isinstance(row.get("public_email"), str) else None,
            research_interests=[item for item in row.get("research_interests") or [] if isinstance(item, str)],
            official_profile_url=row.get("official_profile_url") if isinstance(row.get("official_profile_url"), str) else None,
            official_department_url=(
                row.get("official_department_url") if isinstance(row.get("official_department_url"), str) else None
            ),
            source_urls=[item for item in row.get("source_urls") or [] if isinstance(item, str)],
            papers=[
                PaperRecord.from_dict(item)
                for item in (row.get("papers") or [])
                if isinstance(item, dict) and item.get("title")
            ],
            verification_status="officially_verified",
        )
        match = MatchResult(
            professor_name=record.name,
            university=record.university,
            passed_filters=True,
            filter_notes=[],
            research_score=int(row.get("match_score") or 0),
            matched_interests=[],
            matched_fields=[],
            paper_notes=[],
            summary="",
            matched_areas=[item for item in row.get("matched_areas") or [] if isinstance(item, str)],
            evidence=[item for item in row.get("evidence") or [] if isinstance(item, str)],
            research_confidence=str(row.get("research_confidence") or "low"),
        )
        draft = draft_csc_email(user, record, match)
        if draft is not None:
            drafts.append(draft)
    write_json(destination or drafts_path(), drafts)
    return drafts


def tracker_entry(record: ProfessorRecord, match: MatchResult, *, has_draft: bool) -> TrackerEntry:
    """Build a tracker row. A new draft is ready for review and is not marked sent."""

    status = "draft_ready" if has_draft else "not_contacted"
    if status not in TRACKER_STATUSES:
        status = "not_contacted"
    return TrackerEntry(
        professor=record.name,
        university=record.university,
        email=record.public_email,
        match_score=match.research_score,
        email_status=status,
        notes="Draft is waiting for explicit approval. It has not been sent.",
    )


def approve_draft(entry: TrackerEntry) -> TrackerEntry:
    """Record a person's approval. Approval still does not send the message."""

    if entry.email_status == "draft_ready":
        entry.email_status = "approved"
        entry.notes = "Approved for a later send step. No message has been sent."
    return entry


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def shortlist_path(folder: Path | None = None) -> Path:
    return (folder or get_data_dir()) / "professor_shortlist.json"


def drafts_path(folder: Path | None = None) -> Path:
    return (folder or get_data_dir()) / "email_drafts.json"


def tracker_path(folder: Path | None = None) -> Path:
    return (folder or get_data_dir()) / "application_tracker.json"


def _join_concepts(concepts: list[str]) -> str:
    if len(concepts) == 1:
        return concepts[0]
    if len(concepts) == 2:
        return f"{concepts[0]} and {concepts[1]}"
    return ", ".join(concepts[:-1]) + f", and {concepts[-1]}"


def _select_research_areas(record: ProfessorRecord, match: MatchResult) -> list[str]:
    """Pick one or two areas that the stored record or its evidence actually names."""

    quoted = _quoted_evidence(match.evidence)
    specific = [phrase for phrase in quoted if phrase.casefold() not in _GENERIC_INTERESTS]
    chosen = specific[:2]
    if len(chosen) < 2:
        for interest in _ranked_interests(record.research_interests):
            if interest.casefold() in _GENERIC_INTERESTS or interest in chosen:
                continue
            chosen.append(interest)
            if len(chosen) == 2:
                break
    if not chosen:
        for area in match.matched_areas:
            if area.casefold() == "computer science" or area in chosen:
                continue
            chosen.append(area)
            if len(chosen) == 2:
                break
    return chosen


def _quoted_evidence(evidence: list[str]) -> list[str]:
    found: list[str] = []
    for line in evidence:
        for left, right in _QUOTE_RE.findall(line):
            phrase = (left or right).strip()
            if phrase and phrase not in found:
                found.append(phrase)
    return found


def _ranked_interests(interests: list[str]) -> list[str]:
    indexed = list(enumerate(interests))

    def rank(item: tuple[int, str]) -> tuple[int, int]:
        text = item[1].casefold()
        hinted = 0 if any(hint in text for hint in _STUDENT_HINTS) else 1
        return (hinted, item[0])

    return [interest for _, interest in sorted(indexed, key=rank)]


def _focus_kind(areas: list[str]) -> str:
    blob = " ".join(areas).casefold()
    if any(token in blob for token in ("navigation", "sensor", "robot", "localization", "inertial", "tracking")):
        return "navigation"
    if any(token in blob for token in ("database", "data management", "data mining", "information retrieval", "web data")):
        return "data"
    if any(token in blob for token in ("software engineering", "software system")):
        return "software"
    return "general"


def _connection_sentence(kind: str, shown: str) -> str:
    if kind == "navigation":
        return (
            f"Your stored research interests include {shown}. "
            "My degree is in software engineering, including data-handling coursework, "
            "which is the background I would bring to that work."
        )
    if kind == "data":
        if "database" in shown.casefold():
            return (
                f"The overlap I can describe from your stored evidence is {shown}. "
                "My software engineering coursework includes databases and data handling, "
                "which is the part of this work I can speak about directly."
            )
        return (
            f"The overlap I can describe from your stored record is {shown}. "
            "My background is software engineering with data-handling experience."
        )
    if kind == "software":
        return (
            f"Your stored record points to {shown}. "
            "That sits next to my BS Software Engineering coursework."
        )
    return (
        f"Your stored record points to {shown}. "
        "I can connect that to my software engineering coursework."
    )


def _project_sentence(kind: str, areas: list[str], profile: UserProfile) -> str | None:
    if kind == "navigation":
        return None
    if kind not in {"data", "software"} and medscribe_relevance(areas, profile) is None:
        return None
    if not any(item.title == "MedScribeAI" for item in filled_projects(profile)):
        return None
    if kind == "data" and "database" in " ".join(areas).casefold():
        return (
            "My final-year project, MedScribeAI, is software that stores structured records from speech "
            "and clinical text. It uses Python, FastAPI, React Native, TypeScript, and a database."
        )
    if kind == "data":
        return (
            "My final-year project, MedScribeAI, is project experience with text and structured data. "
            "It uses Python, FastAPI, React Native, TypeScript, and a database."
        )
    if kind == "software":
        return (
            "My final-year project, MedScribeAI, is a software engineering project using Python, FastAPI, "
            "React Native, TypeScript, and a database."
        )
    text = medscribe_relevance(areas, profile)
    return text


def _paper_evidence(
    record: ProfessorRecord,
    match: MatchResult,
    *,
    today: date | None,
) -> list[dict[str, object]]:
    """Keep retrieved paper fields and only the overlaps that appear in those fields."""

    rows: list[dict[str, object]] = []
    current = today or date.today()
    cutoff = current.year - RECENT_PAPER_YEARS
    titled = [paper for paper in record.papers if paper.title.strip()]
    recent = [paper for paper in titled if paper.year is not None and paper.year >= cutoff]
    chosen = sorted(recent or titled, key=lambda paper: paper.year or 0, reverse=True)[:3]
    for paper in chosen:
        abstract = paper.abstract if isinstance(paper.abstract, str) and paper.abstract.strip() else None
        doi = paper.paper_url if paper.paper_url and "doi.org" in paper.paper_url.casefold() else None
        payload = {
            "title": paper.title,
            "year": paper.year,
            "abstract": abstract,
            "keywords": list(paper.keywords),
            "doi": doi,
            "source_url": paper.source_url or paper.paper_url,
        }
        rows.append(
            {
                **payload,
                "connections": _paper_connections(payload, match),
                "recent": paper.year is not None and paper.year >= cutoff,
            }
        )
    return rows


def _paper_connections(paper: dict[str, object], match: MatchResult) -> list[str]:
    parts = [str(paper.get("title") or ""), str(paper.get("abstract") or "")]
    keywords = paper.get("keywords")
    if isinstance(keywords, list):
        parts.extend(str(item) for item in keywords)
    haystack = " ".join(parts).casefold()
    found: list[str] = []
    for phrase in [*match.matched_areas, *_quoted_evidence(match.evidence)]:
        cleaned = phrase.strip()
        if not cleaned or cleaned.casefold() == "computer science":
            continue
        if cleaned.casefold() in haystack and cleaned not in found:
            found.append(cleaned)
        if len(found) == 3:
            break
    return found


def _paper_paragraph(papers: list[dict[str, object]]) -> str:
    sentences = []
    for paper in papers[:2]:
        title = paper["title"]
        year = paper.get("year")
        connections = paper.get("connections") or []
        if paper.get("recent") and year:
            opening = f'Your {year} paper "{title}"'
        elif year:
            opening = f'Your paper "{title}" ({year})'
        else:
            opening = f'Your paper "{title}"'
        if connections:
            sentences.append(
                f"{opening} overlaps with my background in {_join_concepts(list(connections))}."
            )
        else:
            sentences.append(f"{opening} is one of the publications retrieved for this draft.")
    return " ".join(sentences)


def _evidence_urls(
    record: ProfessorRecord,
    match: MatchResult,
    papers: list[dict[str, object]] | None = None,
) -> list[str]:
    urls: list[str] = []
    for url in (record.official_profile_url, record.official_department_url, *record.source_urls):
        if url and url not in urls:
            urls.append(url)
    for paper in papers or []:
        for key in ("doi", "source_url"):
            value = paper.get(key)
            if isinstance(value, str) and value and value not in urls:
                urls.append(value)
    for line in match.evidence:
        for found in _URL_RE.findall(line):
            if found not in urls:
                urls.append(found)
    return urls

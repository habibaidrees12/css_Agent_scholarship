"""Local Streamlit dashboard for the existing CSC research agent.

This file reads stored JSON and calls the existing pipeline when the Search
button is pressed. Gmail sends only after Connect Gmail and an explicit
Approve & Send or a confirmed automatic batch.
"""

from __future__ import annotations

import json
from pathlib import Path

import streamlit as st

from app.gmail_send import (
    approve_and_send,
    automatic_sending_enabled,
    batch_preview,
    client_for_user_action,
    connect_gmail,
    disconnect_gmail,
    gmail_connection_status,
    send_automatic_batch,
    set_automatic_sending,
)
from app.agent_pipeline import (
    PipelineLimits,
    research_candidate,
    resolve_university_domain,
)
from app.config import BROAD_ACADEMIC_FIELDS, get_professors_path, get_search_config_path
from app.outreach import drafts_path, shortlist_path, tracker_path, write_json
from app.profile import load_profile, save_profile, validate_profile
from app.research import attach_match, evaluate_professor, load_search_config
from app.search_api import MemorySearchCache, build_search_provider
from app.storage import ProfessorStore
from app.web_fetcher import OfficialPageFetcher

PAGES = (
    "🏠 Dashboard",
    "🔎 Discover Professors",
    "👨‍🏫 Professors",
    "⭐ Shortlist",
    "✉️ Email Drafts",
    "📋 Application Tracker",
    "👤 My Profile",
)
TRACKER_CHOICES = (
    "Not Contacted",
    "Draft",
    "Sent",
    "Replied",
    "Follow-up",
    "Interested",
    "Rejected",
)
_STATUS_TO_LABEL = {
    "not_contacted": "Not Contacted",
    "draft_ready": "Draft",
    "draft": "Draft",
    "approved": "Draft",
    "sent": "Sent",
    "replied": "Replied",
    "follow_up_due": "Follow-up",
    "follow_up": "Follow-up",
    "interested": "Interested",
    "not_interested": "Rejected",
    "rejected": "Rejected",
    "no_response": "Not Contacted",
}
_LABEL_TO_STATUS = {
    "Not Contacted": "not_contacted",
    "Draft": "draft_ready",
    "Sent": "sent",
    "Replied": "replied",
    "Follow-up": "follow_up_due",
    "Interested": "interested",
    "Rejected": "not_interested",
}


def main() -> None:
    st.set_page_config(page_title="CSC Professor Research Agent", page_icon="🎓", layout="wide")
    _style()
    st.sidebar.title("CSC Professor Research Agent")
    st.sidebar.caption("Drafts stay unsent until you choose Approve & Send.")
    page = st.sidebar.radio("Pages", PAGES, label_visibility="collapsed")
    if page == PAGES[0]:
        page_dashboard()
    elif page == PAGES[1]:
        page_discover()
    elif page == PAGES[2]:
        page_professors()
    elif page == PAGES[3]:
        page_shortlist()
    elif page == PAGES[4]:
        page_drafts()
    elif page == PAGES[5]:
        page_tracker()
    else:
        page_profile()


def page_dashboard() -> None:
    st.header("Dashboard")
    gmail_section()
    professors = _merged_professors()
    shortlist = _read_json(shortlist_path(), [])
    drafts = _read_json(drafts_path(), [])
    tracker = _read_json(tracker_path(), [])
    verified = sum(1 for row in professors if row["verification_status"] == "officially_verified")
    emails = sum(1 for row in professors if row["public_email"])
    cards = st.columns(6)
    cards[0].metric("Total Professors", len(professors))
    cards[1].metric("Verified Professors", verified)
    cards[2].metric("Shortlisted Professors", len(shortlist) if isinstance(shortlist, list) else 0)
    cards[3].metric("Public Emails Found", emails)
    cards[4].metric("Email Drafts", len(drafts) if isinstance(drafts, list) else 0)
    cards[5].metric("Applications / Contacts", len(tracker) if isinstance(tracker, list) else 0)
    st.subheader("Stored professors")
    st.dataframe(
        [
            {
                "Name": row["name"],
                "University": row["university"],
                "Match Score": "" if row["match_score"] is None else str(row["match_score"]),
                "Verification": row["verification_status"] or "",
                "Email": row["public_email"] or "",
            }
            for row in professors
        ],
        width="stretch",
        hide_index=True,
    )


def page_discover() -> None:
    st.header("Discover Professors")
    st.caption("Runs the existing pipeline on stored candidates. Nothing is sent, and current drafts are not regenerated.")
    areas = ["Any", *BROAD_ACADEMIC_FIELDS]
    area = st.selectbox("Research area", areas)
    country = st.text_input("Country", value="", placeholder="Optional")
    university = st.text_input("University", value="", placeholder="Optional")
    limit = st.number_input("Number of candidates", min_value=1, max_value=5, value=5, step=1)
    if st.button("🔎 Search Professors", type="primary"):
        matches = _filter_stored(str(area), country.strip(), university.strip())
        if not matches:
            st.warning("No stored professors match these filters. The search was not run.")
        else:
            with st.spinner("Running the existing discovery pipeline. This does not send email."):
                outcome = _run_existing_pipeline(matches, int(limit))
            st.session_state["discovery_outcome"] = outcome
    outcome = st.session_state.get("discovery_outcome")
    if not outcome:
        st.info("Search has not been run in this session.")
        return
    if outcome.get("error"):
        st.error(outcome["error"])
        return
    st.success(
        f"Processed {outcome['processed']} stored candidates. "
        f"Verified in this run: {outcome['verified']}. Emails were not sent."
    )
    st.dataframe(outcome["rows"], width="stretch", hide_index=True)


def page_professors() -> None:
    st.header("Professors")
    rows = _merged_professors()
    query = st.text_input("Search name, university, or research area")
    status = st.selectbox(
        "Verification",
        ["Any", "officially_verified", "partially_verified", "verification_failed", "unverified"],
    )
    shown = []
    needle = query.strip().casefold()
    for row in rows:
        blob = " ".join(
            [row["name"], row["university"], row["department"], row["research_areas"], row["city"]]
        ).casefold()
        if needle and needle not in blob:
            continue
        if status != "Any" and row["verification_status"] != status:
            continue
        shown.append(row)
    st.dataframe(
        [
            {
                "Professor": row["name"],
                "University": row["university"],
                "City": row["city"],
                "Department": row["department"],
                "Research Areas": row["research_areas"],
                "Match Score": "" if row["match_score"] is None else str(row["match_score"]),
                "Research Confidence": row["research_confidence"],
                "Verification Status": row["verification_status"],
                "Public Email": row["public_email"],
            }
            for row in shown
        ],
        width="stretch",
        hide_index=True,
    )
    if not shown:
        st.info("No professors match this filter.")
        return
    selected = st.selectbox("Professor details", [row["name"] for row in shown])
    row = next(item for item in shown if item["name"] == selected)
    with st.expander(row["name"], expanded=True):
        st.write(f"**University:** {row['university'] or '—'}")
        st.write(f"**Department:** {row['department'] or '—'}")
        st.write(f"**City:** {row['city'] or '—'}")
        st.write(f"**Research interests:** {row['research_areas'] or '—'}")
        st.write(f"**Match score:** {row['match_score'] if row['match_score'] is not None else '—'}")
        st.write(f"**Matched areas:** {row['matched_areas'] or '—'}")
        st.write("**Research evidence**")
        if row["evidence"]:
            for item in row["evidence"]:
                st.write(f"- {item}")
        else:
            st.write("—")
        st.write("**Papers / source URLs**")
        links = row["papers"] + row["source_urls"]
        if links:
            for item in links:
                st.write(item)
        else:
            st.write("—")
        if row["official_profile_url"]:
            st.link_button("Official profile", row["official_profile_url"])
        else:
            st.write("**Official profile URL:** —")
        st.write(f"**Verification notes:** {row['verification_notes'] or '—'}")
        st.write(f"**Public academic email:** {row['public_email'] or '—'}")


def page_shortlist() -> None:
    st.header("Shortlist")
    rows = _read_json(shortlist_path(), [])
    if not isinstance(rows, list) or not rows:
        st.info("The shortlist file has no professors.")
        return
    st.dataframe(
        [
            {
                "Professor": row.get("name") or "",
                "University": row.get("university") or "",
                "Research Match": ", ".join(row.get("matched_areas") or []),
                "Match Score": "" if row.get("match_score") is None else str(row.get("match_score")),
                "Verification Status": row.get("verification_status") or "",
                "Public Email": row.get("public_email") or "",
                "Official Profile": row.get("official_profile_url") or "",
            }
            for row in rows
            if isinstance(row, dict)
        ],
        width="stretch",
        hide_index=True,
    )
    for row in rows:
        if not isinstance(row, dict):
            continue
        url = row.get("official_profile_url")
        if isinstance(url, str) and url:
            st.link_button(f"Open profile: {row.get('name')}", url)


def gmail_section() -> None:
    st.subheader("Gmail")
    status = gmail_connection_status()
    if status.connected:
        st.write("**Gmail status:** Connected")
        if status.account:
            st.write(f"**Authorized account:** {status.account}")
        if st.button("Disconnect Gmail"):
            disconnect_gmail()
            st.rerun()
    else:
        st.write("**Gmail status:** Not connected")
        if st.button("Connect Gmail"):
            result = connect_gmail()
            if result.error:
                st.error(result.error)
            elif result.connected:
                st.success("Gmail connected. No email was sent.")
                st.rerun()
    enabled = automatic_sending_enabled()
    choice = st.checkbox(
        "Automatic sending",
        value=enabled,
        help="Off by default. A confirmed batch sends at most 5 saved drafts.",
    )
    if choice != enabled:
        set_automatic_sending(choice)
        st.rerun()
    if not enabled:
        st.caption("Automatic sending is off. Only Approve & Send can send an email.")
        return
    preview = batch_preview(limit=5)
    st.write(f"**Emails ready:** {len(preview)}")
    if not preview:
        st.caption("No unsent drafts currently meet the send rules.")
        return
    for item in preview:
        st.write(f"- {item['professor_name']} — {item['recipient_email']}")
    st.caption("Nothing is sent until you confirm this list.")
    if st.button("Confirm and send these drafts"):
        client, error = client_for_user_action()
        if client is None:
            st.error(error or "Gmail is not connected.")
            return
        result = send_automatic_batch(confirmed=True, client=client, limit=5)
        if result.blocked_reason:
            st.error(result.blocked_reason)
        elif result.sent_count:
            st.success(f"Sent {result.sent_count} email(s) through Gmail.")
        for message in result.errors:
            st.error(message)


def page_drafts() -> None:
    st.header("Email Drafts")
    st.info("Nothing is sent when this page opens.")
    path = drafts_path()
    drafts = _read_json(path, [])
    if not isinstance(drafts, list) or not drafts:
        st.info("No drafts are stored.")
        return
    for index, draft in enumerate(drafts):
        if not isinstance(draft, dict):
            continue
        name = str(draft.get("professor_name") or "Professor")
        with st.expander(f"{name} — {draft.get('university') or ''}", expanded=index == 0):
            recipient = draft.get("recipient_email")
            preparation = draft.get("gmail_preparation") if isinstance(draft.get("gmail_preparation"), dict) else {}
            sent = draft.get("draft_status") == "sent" or preparation.get("sent") is True
            st.write(f"**Professor:** {name}")
            st.write(f"**University:** {draft.get('university') or ''}")
            if recipient:
                st.write(f"**Recipient:** {recipient}")
            else:
                st.write("**Recipient:** Official email not available")
            st.write("**Research match:** " + ", ".join(draft.get("matched_research_areas") or []))
            st.write("**Paper evidence**")
            papers = draft.get("paper_evidence") or []
            if papers:
                for paper in papers:
                    if not isinstance(paper, dict):
                        continue
                    year = paper.get("year")
                    year_text = f" ({year})" if year else ""
                    abstract = paper.get("abstract")
                    abstract_text = abstract if isinstance(abstract, str) and abstract.strip() else "Abstract not available"
                    st.write(f"- {paper.get('title') or 'Untitled'}{year_text}")
                    st.write(f"  {abstract_text}")
            else:
                st.write("No paper was stored with this draft.")
                for url in draft.get("evidence_urls") or []:
                    st.write(url)
            editing = st.session_state.get(f"editing-{index}", False)
            if editing:
                subject = st.text_input("Subject", value=str(draft.get("subject") or ""), key=f"subject-{index}")
                body = st.text_area("Draft body", value=str(draft.get("body") or ""), height=280, key=f"body-{index}")
            else:
                st.write(f"**Subject:** {draft.get('subject') or ''}")
                st.text_area(
                    "Draft body",
                    value=str(draft.get("body") or ""),
                    height=280,
                    disabled=True,
                    key=f"body-view-{index}",
                )
            st.write(f"**Status:** {'Sent' if sent else (draft.get('draft_status') or 'draft')}")
            if sent and preparation.get("gmail_message_id"):
                st.caption(f"Gmail message id: {preparation.get('gmail_message_id')}")
            edit_col, send_col = st.columns(2)
            if edit_col.button("Edit", key=f"edit-{index}"):
                st.session_state[f"editing-{index}"] = True
                st.rerun()
            if editing and edit_col.button("Save draft", key=f"save-draft-{index}"):
                current = _read_json(path, [])
                if isinstance(current, list) and index < len(current) and isinstance(current[index], dict):
                    current[index]["subject"] = subject
                    current[index]["body"] = body
                    write_json(path, current)
                    st.session_state[f"editing-{index}"] = False
                    st.success("Draft saved. No email was sent.")
                    st.rerun()
            if not sent and send_col.button("Approve & Send", key=f"send-{index}"):
                client, error = client_for_user_action()
                if client is None:
                    st.error(error or "Gmail is not connected.")
                else:
                    outcome = approve_and_send(index, client=client)
                    if outcome.ok:
                        st.success("Sent through Gmail.")
                        st.rerun()
                    else:
                        st.error(outcome.error or "The draft was not sent.")


def page_tracker() -> None:
    st.header("Application Tracker")
    st.caption("Status changes are manual. This page does not send email.")
    path = tracker_path()
    if not path.exists():
        write_json(path, [])
    rows = _read_json(path, [])
    if not isinstance(rows, list):
        rows = []
    if not rows:
        st.info("No applications are stored yet.")
        return
    st.dataframe(
        [
            {
                "Professor": row.get("professor") or "",
                "University": row.get("university") or "",
                "Email": row.get("email") or "",
                "Match Score": "" if row.get("match_score") is None else str(row.get("match_score")),
                "Status": _STATUS_TO_LABEL.get(str(row.get("email_status") or ""), str(row.get("email_status") or "")),
                "Notes": row.get("notes") or "",
            }
            for row in rows
            if isinstance(row, dict)
        ],
        width="stretch",
        hide_index=True,
    )
    labels = [str(row.get("professor") or f"Row {index + 1}") for index, row in enumerate(rows) if isinstance(row, dict)]
    choice = st.selectbox("Update", labels)
    index = labels.index(choice)
    row = rows[index]
    current = _STATUS_TO_LABEL.get(str(row.get("email_status") or ""), "Not Contacted")
    if current not in TRACKER_CHOICES:
        current = "Not Contacted"
    status = st.selectbox("Status", TRACKER_CHOICES, index=TRACKER_CHOICES.index(current))
    notes = st.text_area("Notes", value=str(row.get("notes") or ""))
    if st.button("Save tracker row"):
        fresh = _read_json(path, [])
        if isinstance(fresh, list) and index < len(fresh) and isinstance(fresh[index], dict):
            fresh[index]["email_status"] = _LABEL_TO_STATUS[status]
            fresh[index]["notes"] = notes
            write_json(path, fresh)
            st.success("Tracker updated. No email was sent.")


def page_profile() -> None:
    st.header("My Profile")
    profile = load_profile()
    education = profile.education[0] if profile.education else None
    with st.form("profile-form"):
        degree = st.text_input("Degree", value=education.degree if education else "")
        field = st.text_input("Field of study", value=education.field_of_study if education else "")
        university = st.text_input("University", value=education.university if education else "")
        year_value = education.graduation_year if education and education.graduation_year else 2026
        year = st.number_input("Graduation year", min_value=1900, max_value=2100, value=int(year_value), step=1)
        country = st.text_input("Education country", value=education.country if education else "")
        skills = st.text_area("Skills", value="\n".join(profile.technical_skills), height=160)
        st.markdown("**Projects**")
        project_titles = []
        project_descriptions = []
        project_technologies = []
        for index, project in enumerate(profile.projects):
            project_titles.append(st.text_input(f"Project {index + 1} title", value=project.title, key=f"pt-{index}"))
            project_descriptions.append(
                st.text_area(f"Project {index + 1} description", value=project.description, key=f"pd-{index}")
            )
            project_technologies.append(
                st.text_input(
                    f"Project {index + 1} technologies",
                    value=", ".join(project.technologies),
                    key=f"pk-{index}",
                )
            )
        st.markdown("**Research interests**")
        st.caption("The existing broad CS/IT interests stay in the profile. Priorities and notes can be edited.")
        priorities = []
        keywords = []
        notes = []
        for index, interest in enumerate(profile.research_interests):
            st.text_input(f"Interest {index + 1}", value=interest.name, disabled=True, key=f"in-{index}")
            current = interest.priority if interest.priority in {"high", "medium", "low"} else "medium"
            priorities.append(
                st.selectbox(
                    f"Priority {index + 1}",
                    ["high", "medium", "low"],
                    index=["high", "medium", "low"].index(current),
                    key=f"ip-{index}",
                )
            )
            keywords.append(st.text_input(f"Keywords {index + 1}", value=", ".join(interest.keywords), key=f"ik-{index}"))
            notes.append(st.text_area(f"Notes {index + 1}", value=interest.notes, key=f"ino-{index}"))
        priority_names = [item.name for item in profile.research_interests]
        selected_priorities = st.multiselect(
            "Research priorities",
            priority_names,
            default=[name for name in profile.priority_fields if name in priority_names],
        )
        active_scholarships = st.multiselect(
            "Scholarship targets",
            [item.name for item in profile.scholarship_targets],
            default=[item.name for item in profile.scholarship_targets if item.active],
        )
        preferred_countries = st.text_area(
            "Preferred countries",
            value="\n".join(profile.preferred_countries.countries),
            help="One country per line. Leave blank to keep the current any-country setting.",
        )
        country_mode = st.selectbox(
            "Preferred countries mode",
            ["any", "one", "multiple"],
            index=["any", "one", "multiple"].index(profile.preferred_countries.mode)
            if profile.preferred_countries.mode in {"any", "one", "multiple"}
            else 0,
        )
        preferred_universities = st.text_area(
            "Preferred universities",
            value="\n".join(profile.preferred_universities.universities),
        )
        university_mode = st.selectbox(
            "Preferred universities mode",
            ["any", "one", "multiple"],
            index=["any", "one", "multiple"].index(profile.preferred_universities.mode)
            if profile.preferred_universities.mode in {"any", "one", "multiple"}
            else 0,
        )
        submitted = st.form_submit_button("Save profile")
    if not submitted:
        return
    original_names = [item.name for item in load_profile().research_interests]
    if education is not None:
        education.degree = degree.strip()
        education.field_of_study = field.strip()
        education.university = university.strip()
        education.country = country.strip()
        education.graduation_year = int(year)
    profile.technical_skills = [line.strip() for line in skills.splitlines() if line.strip()]
    for project, title, description, techs in zip(
        profile.projects, project_titles, project_descriptions, project_technologies
    ):
        project.title = title.strip()
        project.description = description.strip()
        project.technologies = [part.strip() for part in techs.split(",") if part.strip()]
    for interest, priority, words, note in zip(profile.research_interests, priorities, keywords, notes):
        interest.priority = priority
        interest.keywords = [part.strip() for part in words.split(",") if part.strip()]
        interest.notes = note.strip()
    missing = [name for name in original_names if name not in {item.name for item in profile.research_interests}]
    broad_missing = [name for name in BROAD_ACADEMIC_FIELDS if name not in {item.name for item in profile.research_interests}]
    if missing or broad_missing:
        st.error("The existing broad CS/IT research interests were not saved because a required area was missing.")
        return
    profile.priority_fields = list(selected_priorities)
    for name in selected_priorities:
        if name not in profile.preferred_fields:
            profile.preferred_fields.append(name)
    chosen = {item.strip() for item in active_scholarships}
    for target in profile.scholarship_targets:
        target.active = target.name in chosen
    profile.preferred_countries.mode = country_mode
    profile.preferred_countries.countries = [line.strip() for line in preferred_countries.splitlines() if line.strip()]
    profile.preferred_universities.mode = university_mode
    profile.preferred_universities.universities = [
        line.strip() for line in preferred_universities.splitlines() if line.strip()
    ]
    errors, warnings = validate_profile(profile)
    for warning in warnings:
        st.warning(warning)
    if errors:
        for error in errors:
            st.error(error)
        return
    save_profile(profile)
    st.success("Profile saved.")


def _run_existing_pipeline(records: list, limit: int) -> dict:
    provider = build_search_provider()
    if provider is None:
        return {"error": "No configured search API."}
    cache = MemorySearchCache()
    fetcher = OfficialPageFetcher(timeout=15)
    user = load_profile()
    config = load_search_config(get_search_config_path())
    bounds = PipelineLimits(max_candidates_per_run=min(limit, 5))
    chosen = list(records[: bounds.max_candidates_per_run])
    domains: dict[str, object] = {}
    rows = []
    verified = 0
    for record in chosen:
        key = " ".join(record.university.casefold().split())
        domain = domains.get(key)
        if domain is None:
            domain = resolve_university_domain(
                record.university,
                provider,
                fetcher,
                cache=cache,
                result_limit=bounds.max_results_per_query,
            )
            domains[key] = domain
        checked = research_candidate(
            record,
            provider=provider,
            fetcher=fetcher,
            domain=domain,
            limits=bounds,
            cache=cache,
        )
        matched = attach_match(user, config, checked)
        result = evaluate_professor(user, config, matched)
        if matched.verification_status == "officially_verified":
            verified += 1
        rows.append(
            {
                "Name": matched.name,
                "University": matched.university,
                "Verification": matched.verification_status,
                "Match Score": str(result.research_score),
                "Confidence": result.research_confidence,
                "Public Email": matched.public_email or "",
                "Official Profile": matched.official_profile_url or "",
            }
        )
    return {"error": None, "processed": len(chosen), "verified": verified, "rows": rows}


def _filter_stored(area: str, country: str, university: str) -> list:
    chosen = []
    for record in ProfessorStore(get_professors_path()).load():
        if country and country.casefold() != (record.country or "").casefold():
            continue
        if university and university.casefold() not in (record.university or "").casefold():
            continue
        if area and area != "Any":
            text = " ".join(record.research_interests + [record.department or ""]).casefold()
            if area.casefold() not in text:
                continue
        chosen.append(record)
    return chosen


def _merged_professors() -> list[dict]:
    stored = _read_json(get_professors_path(), [])
    shortlist = _read_json(shortlist_path(), [])
    by_key = {}
    if isinstance(shortlist, list):
        for row in shortlist:
            if isinstance(row, dict):
                by_key[(str(row.get("name") or ""), str(row.get("university") or ""))] = row
    merged = []
    if not isinstance(stored, list):
        return merged
    for row in stored:
        if not isinstance(row, dict):
            continue
        extra = by_key.get((str(row.get("name") or ""), str(row.get("university") or "")), {})
        match = row.get("match_result") if isinstance(row.get("match_result"), dict) else {}
        interests = row.get("research_interests") or extra.get("research_interests") or []
        papers = []
        for paper in row.get("papers") or []:
            if isinstance(paper, dict) and paper.get("title"):
                year = paper.get("year")
                papers.append(f"{paper.get('title')}" + (f" ({year})" if year else ""))
            elif isinstance(paper, str):
                papers.append(paper)
        score = match.get("research_score")
        if score is None:
            score = extra.get("match_score")
        verification = extra.get("verification_status") or row.get("verification_status") or ""
        email = row.get("public_email") or extra.get("public_email") or ""
        merged.append(
            {
                "name": row.get("name") or "",
                "university": row.get("university") or "",
                "city": row.get("city") or extra.get("city") or "",
                "department": row.get("department") or extra.get("department") or "",
                "research_areas": ", ".join(interests) if isinstance(interests, list) else str(interests),
                "match_score": score,
                "research_confidence": match.get("research_confidence") or extra.get("research_confidence") or "",
                "verification_status": verification,
                "public_email": email or "",
                "matched_areas": ", ".join(extra.get("matched_areas") or match.get("matched_areas") or []),
                "evidence": list(extra.get("evidence") or match.get("evidence") or []),
                "papers": papers,
                "source_urls": list(row.get("source_urls") or extra.get("source_urls") or []),
                "official_profile_url": extra.get("official_profile_url") or row.get("official_profile_url") or "",
                "verification_notes": extra.get("verification_notes") or row.get("verification_notes") or "",
            }
        )
    return merged


def _read_json(path: Path, fallback: object) -> object:
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def _style() -> None:
    st.markdown(
        """
        <style>
        .block-container { padding-top: 1.4rem; }
        div[data-testid="stMetric"] {
            background: #f6f8fb;
            border: 1px solid #e4e8ee;
            border-radius: 10px;
            padding: 0.7rem 0.9rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()

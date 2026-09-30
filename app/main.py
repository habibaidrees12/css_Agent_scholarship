"""Command-line entry for the research agent.

With no subcommand, this shows the current profile, academic scope, and filters.
discover, verify, shortlist, emails, and status run the research pipeline.
Email drafts stay unsent until a person explicitly approves a later send step.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.config import APP_NAME, get_profile_path
from app.models import UserProfile
from app.profile import (
    filled_activities,
    filled_certifications,
    filled_education,
    filled_internships,
    filled_projects,
    load_profile,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Show your scholarship profile, or run the research pipeline."
    )
    parser.add_argument(
        "--profile",
        type=Path,
        help="Path to a user profile JSON file. Defaults to data/user_profile.json.",
    )
    commands = parser.add_subparsers(dest="command")
    for name, help_text in (
        ("discover", "Search official pages for stored candidates and prepare drafts."),
        ("verify", "Verify stored candidates from their official university pages."),
        ("shortlist", "Rebuild the shortlist from records that already qualify."),
        ("emails", "Write personalized CSC drafts. This does not send them."),
        ("status", "Show pipeline counts and whether a search API is configured."),
        (
            "research-and-draft",
            "Discover, verify, collect papers, match, shortlist, and save unsent drafts.",
        ),
    ):
        command = commands.add_parser(name, help=help_text)
        if name in {"discover", "verify", "research-and-draft"}:
            command.add_argument(
                "--limit",
                type=int,
                default=5 if name == "research-and-draft" else None,
                help="Maximum candidates for this run. research-and-draft defaults to 5.",
            )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command:
        return _run_command(args)
    profile_path = args.profile or get_profile_path()
    try:
        profile = load_profile(profile_path)
    except FileNotFoundError as exc:
        print(f"Could not load the user profile: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Could not load the user profile: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Could not read the user profile: {exc}", file=sys.stderr)
        return 1

    print(build_summary(profile, profile_path))
    return 0


def build_summary(profile: UserProfile, profile_path: Path | None = None) -> str:
    """Build the text shown when the program starts."""

    path_text = str(profile_path) if profile_path is not None else str(get_profile_path())
    lines = [
        APP_NAME,
        f"Profile file: {path_text}",
        "",
        f"Name: {profile.personal.full_name or 'not set yet'}",
        f"Email: {profile.personal.email or 'not set yet'}",
        "",
        "Academic fields in this profile:",
    ]
    if profile.preferred_fields:
        lines.extend(f"- {name}" for name in profile.preferred_fields)
    else:
        lines.append("- none listed yet")

    ranked = [item for item in profile.research_interests if item.priority]
    lines.extend(
        [
            "",
            f"Research interests: {len(profile.research_interests)}",
            f"Research interests with a priority: {len(ranked)}",
            f"Priority fields: {', '.join(profile.priority_fields) or 'none yet'}",
            "",
            _filter_line("Country", profile.preferred_countries.mode, profile.preferred_countries.countries),
            _filter_line("City", profile.preferred_cities.mode, profile.preferred_cities.cities),
            _filter_line(
                "University",
                profile.preferred_universities.mode,
                profile.preferred_universities.universities,
            ),
            _filter_line(
                "Department",
                profile.department_filter.mode,
                profile.department_filter.departments,
            ),
            _filter_line("Field", profile.field_filter.mode, profile.field_filter.fields),
            "",
            "Scholarship targets:",
        ]
    )

    if not profile.scholarship_targets:
        lines.append("- none listed")
    for target in profile.scholarship_targets:
        state = "active" if target.active else "planned"
        lines.append(f"- {target.name} [{state}, {target.degree_level}, {target.funding}]")
        if target.active and target.countries and profile.preferred_countries.mode == "any":
            joined = ", ".join(target.countries)
            lines.append(
                f"  {target.name} is aimed at study in {joined}. "
                "Your country filter is still set to any country."
            )

    education_records = filled_education(profile)
    project_records = filled_projects(profile)
    internship_records = filled_internships(profile)
    certification_records = filled_certifications(profile)
    activity_records = filled_activities(profile)
    lines.extend(["", "Education:"])
    if education_records:
        for record in education_records:
            year = record.graduation_year if record.graduation_year is not None else "year not set"
            lines.append(f"- {record.degree} | {record.university} | {record.country} | {year}")
            if record.notes:
                lines.append(f"  {record.notes}")
    else:
        lines.append("- not set yet")
    lines.extend(
        [
            "",
            f"Technical skills listed: {len(profile.technical_skills)}",
            "Projects:",
        ]
    )
    lines.extend(f"- {item.title}" for item in project_records)
    if not project_records:
        lines.append("- none yet")
    lines.append("Internships:")
    if internship_records:
        for item in internship_records:
            label = item.organization or "Organization not provided"
            lines.append(f"- {label}: {item.description}")
    else:
        lines.append("- none yet")
    lines.append("Certifications:")
    if certification_records:
        for item in certification_records:
            issuer = f" ({item.issuer})" if item.issuer else ""
            lines.append(f"- {item.name}{issuer}")
    else:
        lines.append("- none yet")
    lines.append("Activities:")
    if activity_records:
        lines.extend(f"- {item.name}" for item in activity_records)
    else:
        lines.append("- none yet")

    if profile.load_warnings:
        lines.extend(["", "Profile warnings:"])
        lines.extend(f"- {warning}" for warning in profile.load_warnings)

    lines.extend(
        [
            "",
            "Professor records, website collection, email drafts, and application tracking are later steps.",
            "The matching module is ready to score a real professor record when you add one.",
        ]
    )
    return "\n".join(lines)


def _filter_line(label: str, mode: str, values: list[str]) -> str:
    if mode == "any":
        return f"{label} filter: any (no limit)."
    if mode == "one":
        shown = values[0] if values else "missing value"
        return f"{label} filter: one value ({shown})."
    shown = ", ".join(values) if values else "missing values"
    return f"{label} filter: multiple values ({shown})."


def _run_command(args: argparse.Namespace) -> int:
    from app.agent_pipeline import (
        PipelineLimits,
        configuration_report,
        publish_review,
        research_and_draft,
        run_pipeline,
    )
    from app.outreach import SENDS_EMAIL

    if SENDS_EMAIL:
        print("Email sending is disabled.", file=sys.stderr)
        return 2
    if args.command == "status":
        report = configuration_report()
        _print_report(report.as_dict())
        if not report.api_configured:
            print("Real search cannot run yet.")
        return 0
    if args.command in {"discover", "verify"}:
        limits = PipelineLimits()
        if args.limit is not None:
            limits.max_candidates_per_run = args.limit
        report = run_pipeline(limits=limits)
        _print_report(report.as_dict())
        if not report.api_configured:
            print("Real search cannot run yet.")
        return 0
    if args.command == "research-and-draft":
        limit = 5 if args.limit is None else args.limit
        report = research_and_draft(limit=limit, enable_openalex=True)
        _print_report(report.as_dict())
        print("Emails sent: 0")
        print("Gmail was not contacted. Drafts are saved for a later send step.")
        return 0
    if args.command in {"shortlist", "emails"}:
        from app.storage import ProfessorStore

        report = publish_review(ProfessorStore().load())
        _print_report(report.as_dict())
        if args.command == "emails":
            print("Emails sent: 0")
        return 0
    print(f"Unknown command: {args.command}", file=sys.stderr)
    return 2


def _print_report(report: dict[str, object]) -> None:
    print(f"API configured: {report.get('api_configured')}")
    print(f"Message: {report.get('message')}")
    print(f"Candidates processed: {report.get('candidates_processed')}")
    print(f"Professors discovered: {report.get('professors_discovered')}")
    print(f"Official profiles found: {report.get('official_profiles_found')}")
    print(f"Verified professors: {report.get('verified_professors')}")
    print(f"Public academic emails found: {report.get('public_academic_emails_found')}")
    print(f"Strong matches: {report.get('strong_matches')}")
    print(f"Email drafts generated: {report.get('email_drafts_generated')}")
    print(f"Emails sent: {report.get('emails_sent')}")


if __name__ == "__main__":
    raise SystemExit(main())

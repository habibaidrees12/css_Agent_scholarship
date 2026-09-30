# CSC Professor Research Agent

A beginner-friendly Python starter for finding suitable professors for fully funded master's scholarship applications.

CSC (Chinese Government Scholarship) is the first active target. The profile, filters, and matching code are set up so GKS, Erasmus, and other scholarships can be added later without redesigning the profile.

Personal education, skills, projects, and priorities are placeholders. Fill those in yourself. The academic scope starts broad across computer science, software engineering, information technology, and related areas.

## What this starter includes

- A configurable profile in `data/user_profile.json`
- Country, city, university, department, and field filters
- City selection for any city, one city, or multiple cities
- Research interests with optional priority levels: `high`, `medium`, or `low`
- A matching function that can score one professor record when you supply one
- Contact and paper fields on the professor shape, ready for a later data source

Professor records, website collection, email sending, and application tracking are later steps.

## Project layout

```text
csc-professor-agent/
├── app/
│   ├── __init__.py      Package overview
│   ├── config.py        Paths, filter modes, and the starter field list
│   ├── profile.py       Load, validate, and save the profile JSON
│   ├── models.py        Profile, professor, paper, contact, and match shapes
│   ├── matching.py      Compare a profile with one professor record
│   ├── sources.py       Academic source interface and OpenAlex provider
│   ├── research.py      Search settings, filters, and match scoring
│   ├── discovery.py     Run a discovery pass and save passing professors
│   ├── storage.py       JSON storage and professor deduplication
│   ├── verification.py  Official identity check for one candidate
│   ├── university_sources.py
│   ├── web_discovery.py Search the web for official university pages
│   ├── web_fetcher.py   Fetch one public page
│   └── main.py          Command-line summary of the current profile
├── data/
│   ├── user_profile.json
│   ├── search_config.json
│   ├── professors.json
│   └── verification_candidates.json  Pilot review file. It does not replace professors.json.
├── tests/
├── .env.example
├── .gitignore
├── requirements.txt
├── README.md
└── main.py              Shortcut that runs app/main.py
```

## Profile schema

Edit `data/user_profile.json`. Empty strings, empty lists, and `null` mean "not filled in yet."

| JSON field | What it is for |
| --- | --- |
| `personal` | Name and contact details for a later email draft |
| `education` | Each degree, including `degree`, `university`, and `graduation_year` |
| `technical_skills` | A list of skill names |
| `projects` | Project title, description, technologies, year, and URL |
| `internships` | Organization, role, description, and years |
| `certifications` | Name, issuer, and year |
| `research_interests` | Areas to compare with professors. `priority` may be `high`, `medium`, `low`, or `null` |
| `preferred_fields` | Broad fields used when scoring research overlap |
| `priority_fields` | Field names that should count as high priority |
| `preferred_countries` | Country filter |
| `preferred_cities` | City filter |
| `preferred_universities` | University filter |
| `department_filter` | Department filter |
| `field_filter` | Optional hard limit on fields. This is separate from scoring |
| `scholarship_targets` | CSC is active. GKS and Erasmus are present and inactive |

The starter `preferred_fields` and `research_interests` are the same broad list:

- Computer Science
- Software Engineering
- Information Technology
- Data Science
- Data Analytics
- Artificial Intelligence
- Machine Learning
- Natural Language Processing
- Information Systems
- Database Systems
- Software Systems
- Web/Mobile Application Development
- Cloud Computing
- Human-Computer Interaction
- Healthcare IT / Health Informatics

You can add other closely related areas. `priority: null` means the area is included and not ranked.

### Filter modes

Use the same three modes for countries, cities, universities, departments, and fields.

Any city:

```json
"preferred_cities": {
  "mode": "any",
  "cities": []
}
```

One city:

```json
"preferred_cities": {
  "mode": "one",
  "cities": ["City One"]
}
```

Multiple cities:

```json
"preferred_cities": {
  "mode": "multiple",
  "cities": ["City One", "City Two"]
}
```

`mode: "one"` needs exactly one value. `mode: "multiple"` needs at least two. Spelling should match the professor record aside from capitalization. The starter profile uses `any` for every filter, so no city or country is hard-coded as your search limit.

CSC's scholarship record lists China because that describes the scholarship. Your country filter stays `any` until you change it.

`preferred_fields` always feeds the research score. `field_filter` is an extra on/off limit. Leave `field_filter.mode` as `any` when every preferred field should remain eligible.

## How matching will work

`app/matching.py` exposes `match_professor(profile, professor)`.

- Active filters are pass/fail. A failed city filter still reports the research score.
- Overlap is based on whole phrases, so "data" does not match "database".
- The strongest matching area sets the base score. More matching areas add points, up to 100.
- Listing many fields does not divide the score down.
- A paper from the last five years that supports a match adds a small bonus.
- Papers are read only from fields you already have. Nothing is downloaded.

There is no sample professor in `data/`. Build a `Professor` object from a real source when you are ready:

```python
from app.matching import match_professor
from app.models import Professor, ProfessorContact, ResearchPaper
from app.profile import load_profile

profile = load_profile()
professor = Professor(
    name="Professor Name",
    university="University Name",
    department="Department Name",
    city="City Name",
    country="Country Name",
    research_areas=["Database Systems"],
    papers=[ResearchPaper(title="Paper Title", year=2024, abstract="...")],
    contact=ProfessorContact(email="name@university.edu"),
)
result = match_professor(profile, professor)
print(result.summary)
```

Replace every value above with a real record. Those strings are only there to show the fields.

## How to run

From this folder (`csc-professor-agent`):

```powershell
python main.py
```

If `python` is not recognized, use the Windows launcher:

```powershell
py -3 main.py
```

Optional settings:

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
python main.py --profile data\user_profile.json
```

`python-dotenv` is the only dependency, and it is optional. Without it, the program still reads `data/user_profile.json`.

Run the tests:

```powershell
python -m unittest discover -s tests -v
```

Or, with the Windows launcher:

```powershell
py -3 -m unittest discover -s tests -v
```

Use Python 3.9 or newer.

## Discovery

`data/search_config.json` starts with China, any city, any university, and the broad CS/IT areas. `data/professors.json` starts as an empty list.

`run_discovery()` asks each source for those areas, scores passing professors with `match_professor`, and stores them. The default OpenAlex and official-university sources do not call the network, so a normal test run does not add professors and does not need an API key.

Live OpenAlex search is optional later. Set `enable_network=True` on `OpenAlexSource` when you want it. No API key is required. `OPENALEX_MAILTO` in `.env` is an optional contact email for OpenAlex's polite pool.

## Official university page pilot

This reads the saved professors and looks for public `.edu.cn` or `.ac.cn` pages. It does not change `data/professors.json` and it does not send email. The review file is `data/verification_candidates.json`.

From this folder:

```powershell
py -3 -m app.web_discovery
py -3 -m app.main discover
py -3 -m app.main verify
py -3 -m app.main shortlist
py -3 -m app.main emails
py -3 -m app.main status
```

`discover` and `verify` use a configured Bing Web Search API or Google Programmable Search API. Put the key in `.env` as `BING_SEARCH_API_KEY`, or `GOOGLE_SEARCH_API_KEY` with `GOOGLE_SEARCH_ENGINE_ID`. If neither provider is configured, the command prints `No configured search API.` and does not invent search results. The default cap is 10 candidates. Draft commands write files only. They do not send email.

## What to build next

1. Turn on a live OpenAlex search and save the returned professors to `data/professors.json`.
2. Add official university-page collection for `.edu.cn` department and professor pages.
3. Print the saved professors as a ranked list.
4. Add email-draft generation that uses the profile and a professor's public email. Do not send mail automatically.
5. Add an application tracker for status, deadlines, and follow-ups.

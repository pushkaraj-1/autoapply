"""Greenhouse job boards: read the questions from the public API and plan every answer.

The Chrome extension does the actual filling, using the field ids planned here.
"""

import re
from dataclasses import asdict, dataclass
from urllib.parse import parse_qs, urlparse

import httpx

from jobautomate.answers import Answer, resolve_later
from jobautomate.cover_letter import job_description_text
from jobautomate.profile import load_profile

API = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}?questions=true"

KINDS = {
    "input_text": "text",
    "textarea": "text",
    "input_file": "file",
    "multi_value_single_select": "select",
    "multi_value_multi_select": "multiselect",
}


@dataclass
class Field:
    id: str
    label: str
    kind: str
    required: bool
    options: list[str]
    answer: Answer


def parse_url(url: str) -> tuple[str, str]:
    """Returns (board, job_id) from a Greenhouse link or a company page carrying gh_jid."""
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    if "token" in query and "for" in query:
        return query["for"][0], query["token"][0]
    match = re.search(r"greenhouse\.io/([^/]+)/jobs/(\d+)", url)
    if match:
        return match.group(1), match.group(2)
    if "gh_jid" in query:
        host = parsed.hostname.removeprefix("www.").removeprefix("jobs.").removeprefix("careers.")
        return host.split(".")[0], query["gh_jid"][0]
    raise ValueError(f"Not a Greenhouse link I understand: {url}")


def real_board(job_id: str) -> str | None:
    """The board a job belongs to, from Greenhouse's own embed link, which redirects
    to "...?for=<board>&token=<job id>". Company pages with gh_jid don't always use
    the board's name (careers.withwaymo.com is board "waymo")."""
    try:
        response = httpx.get("https://boards.greenhouse.io/embed/job_app", params={"token": job_id}, timeout=15)
    except httpx.HTTPError:
        return None
    board = parse_qs(urlparse(response.headers.get("location", "")).query).get("for", [""])[0]
    return board or None


def fetch_job(board: str, job_id: str) -> dict:
    response = httpx.get(API.format(board=board, job_id=job_id), timeout=30)
    if response.status_code == 404 and (board_name := real_board(job_id)) and board_name != board:
        response = httpx.get(API.format(board=board_name, job_id=job_id), timeout=30)
    response.raise_for_status()
    return response.json()


def build_plan(job: dict, saved: dict | None = None) -> list[Field]:
    """saved maps field id to an answer kept from an earlier run or edited by hand."""
    saved = saved or {}
    company = job.get("company_name") or ""
    context = job_description_text(job["content"])
    p = load_profile()
    fields = []
    for question in job["questions"]:
        for f in question["fields"]:
            options = [v["label"] for v in f.get("values", [])]
            if f["name"] in saved:
                answer = Answer(saved[f["name"]], "saved", "from answers.yaml")
            else:
                answer = resolve_later(question["label"], f["name"], f["type"], options, company, context, required=question["required"])
            fields.append(Field(f["name"], question["label"].strip(), KINDS.get(f["type"], f["type"]), question["required"], options, answer))
            if f["name"] == "phone":
                # The form puts a country picker in front of the phone number.
                fields.insert(-1, Field("country", "Country", "select", True, [], Answer(p["contact"]["country"], "profile")))
    if job.get("education") in ("education_required", "education_optional"):
        # Not in the question list; the form shows School, Degree and sometimes
        # Discipline and dates, one block per school.
        schools = [
            {"school": e["school"], "degree": e["degree_level"], "discipline": e["discipline"], "disciplines": e.get("majors") or [e["discipline"]], "search": e.get("search", e["school"]), "names": e.get("names", [e["school"]]), "start": str(e["start"]), "end": str(e["end"])}
            for e in p["education"]
        ]
        fields.append(Field("education", "Education", "education", job["education"] == "education_required", [], Answer(schools, "profile")))
    if job.get("location_questions"):
        city = f"{p['contact']['city']}, {p['contact']['state']}"
        fields.append(Field("candidate-location", "Location (City)", "location", True, [], Answer(city, "profile")))
    demographics = job.get("demographic_questions") or {}
    for question in demographics.get("questions", []):
        options = [o["label"] for o in question["answer_options"]]
        answer = resolve_later(question["label"], "", question["type"], options, company, required=question["required"])
        fields.append(Field(str(question["id"]), question["label"], KINDS[question["type"]], question["required"], options, answer))
    return fields


def plan_as_dicts(fields: list[Field]) -> list[dict]:
    return [asdict(f) for f in fields]


def summary(job: dict) -> tuple[str, str, str, str]:
    """(company, title, location, description html)"""
    return job.get("company_name") or "", job["title"].strip(), job["location"]["name"], job["content"]

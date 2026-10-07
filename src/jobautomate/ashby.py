"""Ashby job boards: read the form from Ashby's API and plan every answer.

The Chrome extension fills the fields, finding each one by its data-field-path.
"""

import copy
import re
import threading
import time

import httpx

from jobautomate.answers import Answer, resolve_later
from jobautomate.cover_letter import job_description_text
from jobautomate.greenhouse import Field
from jobautomate.profile import load_profile

GRAPHQL = "https://jobs.ashbyhq.com/api/non-user-graphql"
BOARD_API = "https://api.ashbyhq.com/posting-api/job-board/{board}"

FORM_QUERY = """query ApiJobPosting($organizationHostedJobsPageName: String!, $jobPostingId: String!) {
  jobPosting(organizationHostedJobsPageName: $organizationHostedJobsPageName, jobPostingId: $jobPostingId) {
    id title locationName descriptionHtml
    applicationForm { sections { fieldEntries { ... on FormFieldEntry { id field isRequired } } } }
    surveyForms { sections { fieldEntries { ... on FormFieldEntry { id field isRequired } } } }
  }
}"""
ORG_QUERY = """query ApiOrganizationFromHostedJobsPageName($organizationHostedJobsPageName: String!, $searchContext: OrganizationSearchContext) {
  organization: organizationFromHostedJobsPageName(organizationHostedJobsPageName: $organizationHostedJobsPageName, searchContext: $searchContext) { name }
}"""

KINDS = {
    "String": "text",
    "Email": "text",
    "Phone": "text",
    "LongText": "text",
    "Number": "text",
    "Url": "text",
    "File": "file",
    "Boolean": "boolean",
    "ValueSelect": "radio",
    "MultiValueSelect": "checkboxes",
    "Location": "location",
}


def parse_url(url: str) -> tuple[str, str]:
    match = re.search(r"jobs\.ashbyhq\.com/([^/?#]+)/(?:embed/)?([0-9a-f-]{36})", url)
    if not match:
        raise ValueError(f"Not an Ashby link I understand: {url}")
    return match.group(1), match.group(2)


def graphql(operation: str, query: str, variables: dict) -> dict:
    # Ashby answers "429 Too Many Requests" when several applications open at once;
    # waiting a little and asking again gets through.
    for attempt in range(5):
        response = httpx.post(f"{GRAPHQL}?op={operation}", json={"operationName": operation, "query": query, "variables": variables}, timeout=30)
        if response.status_code != 429 or attempt == 4:
            break
        time.sleep(float(response.headers.get("retry-after") or 0) or 1.5 * 2**attempt)
    response.raise_for_status()
    body = response.json()
    if body.get("errors"):
        raise RuntimeError(f"Ashby API error: {body['errors'][0]['message']}")
    return body["data"]


# Each application asks for its job more than once (warming up, then filling), and
# company names repeat across jobs, so answers are kept for a while.
_jobs: dict[tuple[str, str], tuple[float, dict]] = {}
_companies: dict[str, str] = {}
_lock = threading.Lock()
JOB_TTL = 600


def fetch_job(board: str, job_id: str) -> dict:
    with _lock:
        cached = _jobs.get((board, job_id))
    if cached and time.time() - cached[0] < JOB_TTL:
        return copy.deepcopy(cached[1])
    posting = graphql("ApiJobPosting", FORM_QUERY, {"organizationHostedJobsPageName": board, "jobPostingId": job_id})["jobPosting"]
    if not posting:
        raise ValueError(f"Ashby has no open posting {job_id} on board {board}")
    if board not in _companies:
        org = graphql("ApiOrganizationFromHostedJobsPageName", ORG_QUERY, {"organizationHostedJobsPageName": board, "searchContext": "JobPosting"})
        _companies[board] = (org.get("organization") or {}).get("name") or board.title()
    posting["company_name"] = _companies[board]
    with _lock:
        _jobs[(board, job_id)] = (time.time(), posting)
    return copy.deepcopy(posting)


def entries(posting: dict) -> list[dict]:
    forms = [posting["applicationForm"], *(posting.get("surveyForms") or [])]
    return [entry for form in forms for section in form["sections"] for entry in section["fieldEntries"] if "field" in entry]


def build_plan(posting: dict, saved: dict | None = None) -> list[Field]:
    """saved maps field path to an answer kept from an earlier run or edited by hand."""
    saved = saved or {}
    company = posting["company_name"]
    context = job_description_text(posting.get("descriptionHtml") or "")
    p = load_profile()
    fields = []
    for entry in entries(posting):
        f = entry["field"]
        kind = KINDS.get(f["type"], f["type"])
        options = ["Yes", "No"] if kind == "boolean" else [v["label"] for v in f.get("selectableValues") or []]
        if f["path"] in saved:
            answer = Answer(saved[f["path"]], "saved", "from answers.yaml")
        elif kind == "location":
            answer = Answer(f"{p['contact']['city']}, {p['contact']['state']}", "profile")
        else:
            answer = resolve_later(f["title"], f["path"], f["type"], options, company, context, required=entry["isRequired"])
        fields.append(Field(f["path"], f["title"], kind, entry["isRequired"], options, answer))
    return fields


def summary(posting: dict) -> tuple[str, str, str, str]:
    """(company, title, location, description html)"""
    return posting["company_name"], posting["title"].strip(), posting.get("locationName") or "", posting.get("descriptionHtml") or ""

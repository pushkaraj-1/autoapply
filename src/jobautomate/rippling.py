"""Rippling job boards: read the form from the job page's embedded data and plan every answer.

The extension finds standard fields by data-testid="input-<oid>" and custom
questions by a data-testid ending in their uniqueKey.
"""

import json
import re

import httpx

from jobautomate.answers import Answer, resolve_later
from jobautomate.cover_letter import job_description_text
from jobautomate.greenhouse import Field
from jobautomate.profile import load_profile

JOB_PAGE = "https://ats.rippling.com/{board}/jobs/{job_id}"
BASIC_KINDS = {"SHORT_ANSWER": "text", "PHONE_NUMBER": "text", "FILE": "file", "PRONOUN": "skip"}
CUSTOM_KINDS = {"select": "select", "Text": "text", "text": "text", "enum": "enum", "Date": "date"}


def parse_url(url: str) -> tuple[str, str]:
    match = re.search(r"ats\.rippling\.com/(?:[a-z]{2}-[A-Z]{2}/)?([^/?#]+)/jobs/([0-9a-f-]{36})", url)
    if not match:
        raise ValueError(f"Not a Rippling link I understand: {url}")
    return match.group(1), match.group(2)


def fetch_job(board: str, job_id: str) -> dict:
    page = httpx.get(JOB_PAGE.format(board=board, job_id=job_id), timeout=30, headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True)
    data = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page.raise_for_status().text, re.S)
    if not data:
        raise ValueError("Rippling page did not include the job data")
    return json.loads(data.group(1))["props"]["pageProps"]["apiData"]["jobPost"]


def build_plan(job: dict, saved: dict | None = None) -> list[Field]:
    saved = saved or {}
    company = job["companyName"]
    context = job_description_text(summary(job)[3])
    p = load_profile()
    fields = []
    application = job["activeJobApplication"]
    for q in application["basicQuestions"]:
        kind = "location" if q["oid"] == "location" else BASIC_KINDS.get(q["fieldType"], "text")
        if kind == "skip":
            continue
        if q["oid"] in saved:
            answer = Answer(saved[q["oid"]], "saved", "from answers.yaml")
        elif kind == "location":
            answer = Answer(f"{p['contact']['city']}, {p['contact']['state']}", "profile")
        else:
            answer = resolve_later(q["title"], q["oid"], "File" if kind == "file" else "String", [], company, context, required=q["required"])
        fields.append(Field(q["oid"], q["title"], kind, q["required"], [], answer))
    for block in application.get("additionalQuestions") or []:
        for q in block["form"]["questions"]:
            kind = CUSTOM_KINDS.get(q["dataType"], q["dataType"])
            options = q.get("strChoices") or [str(i) for i in q.get("intChoices") or []]
            if q["uniqueKey"] in saved:
                answer = Answer(saved[q["uniqueKey"]], "saved", "from answers.yaml")
            else:
                field_type = {"LONG_ANSWER": "LongText", "DATE": "Date"}.get(q.get("questionType"), "String")
                if kind == "enum" and q.get("isMultiSelectEnabled"):
                    field_type = "MultiValueSelect"
                answer = resolve_later(q["title"], q["uniqueKey"], field_type, options, company, context, required=q["isRequired"])
            fields.append(Field(q["uniqueKey"], q["title"].strip(), kind, q["isRequired"], options, answer))
    return fields


def summary(job: dict) -> tuple[str, str, str, str]:
    """(company, title, location, description html)"""
    description = job["description"]
    if isinstance(description, dict):
        description = "".join(str(part) for part in description.values() if part)
    return job["companyName"], job["name"].strip(), ", ".join(job.get("workLocations") or []), description

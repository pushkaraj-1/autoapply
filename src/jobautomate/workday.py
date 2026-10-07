"""Workday job sites: read the job from Workday's public API.

Workday forms sit behind a login and change step by step, so the questions
are not known up front. The extension reads each step's fields from the page
and asks the server for answers (see prepare.answer_fields).
"""

import re

import httpx

from jobautomate.greenhouse import Field

URL_PATTERN = re.compile(
    r"https://(?P<host>(?P<tenant>[^./]+)\.wd\d+\.myworkdayjobs\.com)/(?:[a-z]{2}-[A-Z]{2}/)?(?P<site>[^/]+)/job/(?P<path>.+?)(?:/apply.*)?/?(?:[?#].*)?$"
)


def parse_url(url: str) -> tuple[str, str]:
    match = URL_PATTERN.match(url)
    if not match:
        raise ValueError(f"Not a Workday job link I understand: {url}")
    return match["tenant"], match["path"].split("/")[-1]


def fetch_url(url: str) -> dict:
    match = URL_PATTERN.match(url)
    api = f"https://{match['host']}/wday/cxs/{match['tenant']}/{match['site']}/job/{match['path']}"
    data = httpx.get(api, timeout=30, headers={"Accept": "application/json"}).raise_for_status().json()
    job = data["jobPostingInfo"]
    job["company_name"] = (data.get("hiringOrganization") or {}).get("name") or match["tenant"].title()
    return job


def build_plan(job: dict, saved: dict | None = None) -> list[Field]:
    return []  # fields arrive from the page one step at a time


def summary(job: dict) -> tuple[str, str, str, str]:
    """(company, title, location, description html)"""
    return job["company_name"], job["title"].strip(), job.get("location", ""), job.get("jobDescription", "")

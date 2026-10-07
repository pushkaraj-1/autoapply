"""SmartRecruiters job pages: read the job from the public postings API.

Links look like jobs.smartrecruiters.com/<Company>/<posting id>-<slug> (the job ad)
or jobs.smartrecruiters.com/oneclick-ui/company/<Company>/publication/<uuid> (the
application). The API takes either the id or the uuid. The application's questions
are not public, so the extension reads them from the page (see prepare.answer_fields).
"""

import re

import httpx

from jobautomate.greenhouse import Field

API = "https://api.smartrecruiters.com/v1/companies/{company}/postings/{posting}"
URL_PATTERNS = (
    re.compile(r"https://(?:jobs\.smartrecruiters\.com|www\.smartr\.me)/oneclick-ui/company/(?P<company>[^/?#]+)/publication/(?P<posting>[0-9a-f-]{36})"),
    re.compile(r"https://(?:jobs|careers)\.smartrecruiters\.com/(?P<company>[^/?#]+)/(?P<posting>\d{6,})"),
)


def parse_url(url: str) -> tuple[str, str]:
    for pattern in URL_PATTERNS:
        if match := pattern.match(url):
            return match["company"], match["posting"]
    raise ValueError(f"Not a SmartRecruiters job link I understand: {url}")


def fetch_url(url: str) -> dict:
    company, posting = parse_url(url)
    response = httpx.get(API.format(company=company, posting=posting), timeout=30)
    if response.status_code == 404:
        raise ValueError("This SmartRecruiters job is no longer posted.")
    return response.raise_for_status().json()


def build_plan(job: dict, saved: dict | None = None) -> list[Field]:
    return []  # fields arrive from the page


def summary(job: dict) -> tuple[str, str, str, str]:
    """(company, title, location, description html)"""
    sections = (job.get("jobAd") or {}).get("sections") or {}
    parts = [(sections.get(k) or {}).get("text") or "" for k in ("jobDescription", "qualifications", "additionalInformation", "companyDescription")]
    location = job.get("location") or {}
    where = location.get("fullLocation") or ", ".join(p for p in (location.get("city"), location.get("region"), location.get("country")) if p)
    if location.get("remote"):
        where = f"{where} (remote)" if where else "Remote"
    return (job.get("company") or {}).get("name") or "", (job.get("name") or "").strip(), where, "\n".join(p for p in parts if p)

"""Oracle Recruiting Cloud (Candidate Experience) job sites: read the job from the
site's public REST API.

The application itself sits behind an email step (and sometimes a code), and its
questions are not public, so the extension reads each section's fields from the
page and asks the server for answers (see prepare.answer_fields).
"""

import re

import httpx

from jobautomate.greenhouse import Field

URL_PATTERN = re.compile(
    r"https://(?P<host>(?P<tenant>[^./]+)\.fa(?:\.[^/]+)?\.oraclecloud\.com)/hcmUI/CandidateExperience/(?P<lang>[a-z]{2})/sites/(?P<site>[^/]+)/(?:job|requisitions/preview)/(?P<id>\d+)"
)


def parse_url(url: str) -> tuple[str, str]:
    match = URL_PATTERN.match(url)
    if not match:
        raise ValueError(f"Not an Oracle job link I understand: {url}")
    return match["tenant"], match["id"]


def fetch_url(url: str) -> dict:
    match = URL_PATTERN.match(url)
    base = f"https://{match['host']}"
    headers = {"Accept": "application/json", "ora-irc-language": match["lang"]}
    details = httpx.get(
        f"{base}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails",
        params={"expand": "all", "onlyData": "true", "finder": f'ById;Id="{match["id"]}",siteNumber={match["site"]}'},
        headers=headers,
        timeout=30,
    ).raise_for_status().json()
    if not details.get("items"):
        raise ValueError("This Oracle job is no longer posted.")
    job = details["items"][0]
    try:
        settings = httpx.get(f"{base}/hcmRestApi/CandidateExperience/{match['lang']}/siteSettings/{match['site']}", headers=headers, timeout=30).json()
        job["company_name"] = (settings.get("app") or {}).get("siteName") or ""
    except (httpx.HTTPError, ValueError):
        job["company_name"] = ""
    job["company_name"] = job["company_name"] or match["tenant"].upper()
    return job


def build_plan(job: dict, saved: dict | None = None) -> list[Field]:
    return []  # fields arrive from the page, section by section


def summary(job: dict) -> tuple[str, str, str, str]:
    """(company, title, location, description html)"""
    parts = [job.get(k) or "" for k in ("ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr", "CorporateDescriptionStr")]
    return job["company_name"], (job.get("Title") or "").strip(), job.get("PrimaryLocation") or "", "\n".join(p for p in parts if p)

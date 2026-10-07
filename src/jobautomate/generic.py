"""Any other job site (Gusto, Wellfound, ...): the extension reads the fields and the
job details from the page, because these sites don't offer a public API.

This module only names the job folder; answer_fields gets the job details from
the page instead of fetching them.
"""

import hashlib
import re
from urllib.parse import urlparse

from jobautomate.greenhouse import Field


def parse_url(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"Not a web address: {url}")
    host = parsed.hostname.removeprefix("www.").removeprefix("jobs.")
    # iCIMS: /jobs/<id>/<slug>/<page> for every page of one application.
    if icims := re.match(r"/jobs/(\d+)/([^/]+)", parsed.path):
        board = re.sub(r"^(careers|internal|external|jobs|uscareers)-", "", host.split(".")[0])
        return board, f"{icims.group(1)}-{icims.group(2)}"[:80]
    path = re.sub(r"/(applicants/new|apply|application)/?$", "", parsed.path.rstrip("/"))
    slug = path.split("/")[-1] or "job"
    # Some sites keep the job id in the query (Indeed's viewjob?jk=..., gh_jid on
    # career pages, New York Life's apply?pid=...).
    if job_id := re.search(r"(?:^|&)(?:jk|gh_jid|currentJobId|jobId|job_id|jobid|jid|pid|postingId|reqId|requisitionId|career_job_req_id|req|id)=([A-Za-z0-9-]+)", parsed.query, re.I):
        slug = f"{slug}-{job_id.group(1)}"
    elif parsed.query and not re.search(r"\d{3,}", slug):
        # No job id in the path or a known query name: tell jobs apart by the whole link,
        # so two jobs on one site never share a folder (and a cover letter).
        slug = f"{slug}-{hashlib.sha1(url.split('#')[0].encode()).hexdigest()[:10]}"
    return host.split(".")[0], re.sub(r"[^A-Za-z0-9-]+", "-", slug)[-80:]


def fetch_url(url: str) -> dict:
    return {}  # filled in from the page by answer_fields


def build_plan(job: dict, saved: dict | None = None) -> list[Field]:
    return []


def summary(job: dict) -> tuple[str, str, str, str]:
    """(company, title, location, description html)"""
    return job.get("company", ""), job.get("title", ""), job.get("location", ""), job.get("description", "")

"""Job description fetcher (port of lib/jd-fetch.mjs).

Uses the cleanest route for each site: LinkedIn's guest posting endpoint, the
Greenhouse, Lever, Ashby and Workday APIs, and otherwise the page itself turned
into text. Pages that only render with JavaScript come back short and are marked
generic-sparse.
"""

import html
import json
import re
import time
from functools import lru_cache
from urllib.parse import urlparse

import httpx

from jobautomate.scanner.sources import UA

MIN_USEFUL = 200  # fewer characters than this means the description wasn't really found


def strip_html_to_text(page: str) -> str:
    if not isinstance(page, str):
        return ""
    text = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", " ", page, flags=re.I)
    text = re.sub(r"</(p|div|li|br|h[1-6]|tr)>", "\n", text, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()


def parse_ats_url(url: str) -> dict:
    if m := re.search(r"linkedin\.com/jobs/view/(?:[^/?#]*?-)?(\d{6,})", url):
        return {"ats": "linkedin", "id": m.group(1)}
    if m := re.search(r"(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/([^/]+)/jobs/(\d+)", url):
        return {"ats": "greenhouse", "org": m.group(1), "id": m.group(2)}
    if m := re.search(r"(?:jobs|jobs\.eu)\.lever\.co/([^/]+)/([0-9a-f-]{36})", url):
        return {"ats": "lever", "org": m.group(1), "id": m.group(2)}
    if m := re.search(r"jobs\.ashbyhq\.com/([^/]+)/([0-9a-f-]{36})", url):
        return {"ats": "ashby", "org": m.group(1), "id": m.group(2)}
    if m := re.search(r"jobs\.smartrecruiters\.com/(?:oneclick-ui/company/)?([^/?#]+)/(?:publication/)?([0-9a-f-]{36}|\d{6,})", url):
        return {"ats": "smartrecruiters", "org": m.group(1), "id": m.group(2)}
    if m := re.search(r"jobright\.ai/jobs/info/([0-9a-f]+)", url):
        return {"ats": "jobright", "id": m.group(1)}
    if ".myworkdayjobs.com" in url:
        parsed = urlparse(url)
        parts = [p for p in parsed.path.split("/") if p]
        if "job" in parts and parts.index("job") > 0:
            i = parts.index("job")
            return {"ats": "workday", "host": parsed.hostname, "tenant": parsed.hostname.split(".")[0], "site": parts[i - 1], "path": "/".join(parts[i + 1 :])}
    return {"ats": "generic"}


def http_text(url: str, retries: int = 1) -> str:
    for attempt in range(retries + 1):
        response = httpx.get(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}, timeout=15, follow_redirects=True)
        if (response.status_code == 429 or response.status_code >= 500) and attempt < retries:
            time.sleep(1.5 * (attempt + 1))  # back off, mostly for LinkedIn
            continue
        response.raise_for_status()
        return response.text
    return ""


@lru_cache(maxsize=64)
def ashby_board(org: str) -> dict:
    return json.loads(http_text(f"https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=true"))


def fetch_jd(url: str) -> tuple[str, str]:
    """(description text, where it came from)."""
    ats = parse_ats_url(url)
    try:
        text = ""
        if ats["ats"] == "linkedin":
            text = strip_html_to_text(http_text(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{ats['id']}"))
        elif ats["ats"] == "greenhouse":
            text = strip_html_to_text(html.unescape(json.loads(http_text(f"https://boards-api.greenhouse.io/v1/boards/{ats['org']}/jobs/{ats['id']}")).get("content", "")))
        elif ats["ats"] == "lever":
            data = json.loads(http_text(f"https://api.lever.co/v0/postings/{ats['org']}/{ats['id']}"))
            lists = " ".join(f"{x.get('text', '')} {x.get('content', '')}" for x in data.get("lists", []))
            text = strip_html_to_text(f"{data.get('description', '')} {lists} {data.get('additional', '')}")
        elif ats["ats"] == "ashby":
            job = next((j for j in ashby_board(ats["org"]).get("jobs", []) if ats["id"] in (j.get("jobUrl") or "") or j.get("id") == ats["id"]), None)
            text = strip_html_to_text((job or {}).get("descriptionHtml") or (job or {}).get("descriptionPlain", ""))
        elif ats["ats"] == "smartrecruiters":
            sections = (json.loads(http_text(f"https://api.smartrecruiters.com/v1/companies/{ats['org']}/postings/{ats['id']}")).get("jobAd") or {}).get("sections") or {}
            text = strip_html_to_text(" ".join((sections.get(k) or {}).get("text", "") for k in ("jobDescription", "qualifications", "additionalInformation")))
        elif ats["ats"] == "jobright":
            # The page carries Jobright's summary of the job ad, not the ad itself.
            page = http_text(f"https://jobright.ai/jobs/info/{ats['id']}")
            data = re.search(r'id="__NEXT_DATA__"[^>]*>([\s\S]*?)</script>', page)
            job = json.loads(data.group(1))["props"]["pageProps"]["dataSource"]["jobResult"] if data else {}
            quals = job.get("qualifications") or {}
            parts = [job.get("jobSummary", ""), *job.get("coreResponsibilities", []), *quals.get("mustHave", []), *quals.get("preferredHave", []), *job.get("benefitsSummaries", [])]
            text = "\n".join(p for p in parts if p)
        elif ats["ats"] == "workday":
            info = json.loads(http_text(f"https://{ats['host']}/wday/cxs/{ats['tenant']}/{ats['site']}/job/{ats['path']}")).get("jobPostingInfo", {})
            text = strip_html_to_text(info.get("jobDescription", ""))
        if len(text) >= MIN_USEFUL:
            return text, ats["ats"]
    except Exception:
        pass  # fall through to reading the page itself
    try:
        text = strip_html_to_text(http_text(url))
        return text, "generic" if len(text) >= MIN_USEFUL else "generic-sparse"
    except Exception:
        return "", "error"

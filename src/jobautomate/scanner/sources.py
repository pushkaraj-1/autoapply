"""Job sources (port of lib/job-sources.mjs). No AI calls.

Each fetcher returns a list of offers {title, url, company, location, posted, source},
plus {"__error": "..."} entries for problems that should be shown but not stop the scan.

Sources:
  simplify    curated GitHub listings.json (intern / new grad)
  speedyapply SpeedyApply's GitHub job list (Markdown table)
  jobright    Jobright's GitHub new grad list (Markdown table; links open on jobright.ai)
  hiringcafe  hiring.cafe server-rendered search (US)
  linkedin    LinkedIn's public guest job search (no account used)
  remoteok    RemoteOK JSON feed
  remotive    Remotive JSON feed
  greenhouse  boards-api.greenhouse.io, one board per company
  lever       api.lever.co, one board per company
  ashby       api.ashbyhq.com, one board per company
  smartrecruiters  api.smartrecruiters.com, one company each (US postings)
  themuse     The Muse public jobs API
  googlejobs  Google Programmable Search over company job sites (GOOGLE_CSE_KEY / GOOGLE_CSE_CX)
  adzuna      Adzuna REST API (ADZUNA_APP_ID / ADZUNA_API_KEY)
  jobspy      python-jobspy: Indeed, Google, ZipRecruiter (LinkedIn only behind a proxy)
"""

import html
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode, urlparse

import httpx

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
TIMEOUT = 15


class NoCredentials(Exception):
    """A source needs keys that are not in .env; reported as a notice."""


def clean(value) -> str:
    return re.sub(r"\s+", " ", value).strip() if isinstance(value, str) else ""


def to_iso_date(value) -> str:
    """Epoch seconds or milliseconds, or an ISO string, as YYYY-MM-DD ('' if unknown)."""
    if value in (None, ""):
        return ""
    try:
        if isinstance(value, (int, float)):
            seconds = value if value < 2e10 else value / 1000
            return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%d")
        text = str(value).strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return text
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except (ValueError, OverflowError, OSError):
        return ""


def describe(error: Exception) -> str:
    """A short error for the page, without the request URL (it can carry API keys)."""
    if isinstance(error, httpx.HTTPStatusError):
        return f"HTTP {error.response.status_code}"
    if isinstance(error, httpx.TimeoutException):
        return "timed out"
    return re.sub(r"([?&](?:key|app_key|app_id|api_key)=)[^&\s'\"]+", r"\1***", str(error))[:160]


REFUSED = (401, 403, 429)


def over_terms(name: str, terms: list[str], search, workers: int = 3, delay: float = 0.0, give_up: int | None = 2) -> list[dict]:
    """Runs search(term) for every term, a few at a time. When the site refuses
    (403, 429, ...) give_up times, the terms not yet searched are skipped instead
    of each failing in turn."""
    refused = [0]
    lock = threading.Lock()

    def one(term: str) -> list[dict]:
        if give_up and refused[0] >= give_up:
            return [{"__skipped": term}]
        try:
            try:
                result = search(term)
            except httpx.HTTPStatusError as error:
                if error.response.status_code != 429:
                    raise
                # "Too many requests": wait as long as the site asks (up to 10 seconds), then one more try.
                wait = error.response.headers.get("Retry-After", "")
                time.sleep(min(float(wait), 10) if wait.isdigit() else 4)
                result = search(term)
        except Exception as error:
            if isinstance(error, httpx.HTTPStatusError) and error.response.status_code in REFUSED:
                with lock:
                    refused[0] += 1
            return [{"__error": f'{name} "{term}": {describe(error)}'}]
        if delay:
            time.sleep(delay)  # spaces out each worker's requests
        return result

    out: list[dict] = []
    with ThreadPoolExecutor(workers) as pool:
        for batch in pool.map(one, terms or [""]):
            out += batch
    skipped = sum("__skipped" in o for o in out)
    out = [o for o in out if "__skipped" not in o]
    if skipped:
        out.append({"__error": f"{name}: the site kept refusing, so the other {skipped} searches were skipped"})
    return out


def strip_html(text) -> str:
    return clean(html.unescape(re.sub(r"<[^>]*>", "", text))) if isinstance(text, str) else ""


def get_text(url: str, headers: dict | None = None, proxy: str | None = None) -> str:
    response = httpx.get(url, headers={"User-Agent": UA, **(headers or {})}, timeout=TIMEOUT, follow_redirects=True, proxy=proxy)
    response.raise_for_status()
    return response.text


def get_json(url: str, **kwargs):
    return json.loads(get_text(url, headers={"Accept": "application/json"}, **kwargs))


def normalize_boards(boards) -> list[dict]:
    """Board entries may be a token string ("anthropic") or {token, company}."""
    out = []
    for board in boards or []:
        board = {"token": board} if isinstance(board, str) else board
        if board and board.get("token"):
            out.append({"token": str(board["token"]).strip(), "company": clean(board.get("company", ""))})
    return out


def label_from_token(token: str) -> str:
    text = re.sub(r"[-_]+", " ", str(token)).strip()
    return text[:1].upper() + text[1:]


def offer(title, url, company, location, posted, source) -> dict:
    return {"title": clean(title), "url": url or "", "company": clean(company), "location": clean(location), "posted": posted, "source": source}


def valid(offers: list[dict]) -> list[dict]:
    return [o for o in offers if o["url"] and o["title"]]


# ---- Simplify ----


def parse_simplify(listings) -> list[dict]:
    if not isinstance(listings, list):
        return []
    out = []
    for job in listings:
        if not job or job.get("active") is False or job.get("is_visible") is False or not job.get("url"):
            continue
        locations = job.get("locations")
        location = " / ".join(locations) if isinstance(locations, list) else clean(locations or job.get("location", ""))
        out.append(offer(job.get("title"), job["url"], job.get("company_name") or job.get("company", ""), location, to_iso_date(job.get("date_posted") or job.get("date_updated")), "simplify"))
    return out


def fetch_simplify(cfg: dict, terms: list[str]) -> list[dict]:
    out = []
    for url in cfg.get("repos", []):
        try:
            out += parse_simplify(get_json(url))
        except Exception as error:
            out.append({"__error": f"simplify {url}: {describe(error)}"})
    return out


# ---- GitHub job lists in Markdown tables (SpeedyApply, Jobright) ----

A_TAG = re.compile(r'<a\s[^>]*href="([^"]+)"[^>]*>([\s\S]*?)</a>', re.I)
MD_LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")
TITLE_COLUMNS = ("position", "job title", "role", "title")
LINK_COLUMNS = ("posting", "apply", "application", "link")
DATE_COLUMNS = ("age", "date posted", "posted", "date")


def md_cell_text(cell: str) -> str:
    text = A_TAG.sub(lambda m: m.group(2), cell)
    text = MD_LINK.sub(lambda m: m.group(1), text)
    return clean(re.sub(r"<[^>]+>|\*\*|__", " ", text))


def md_cell_link(cell: str) -> str:
    match = A_TAG.search(cell) or MD_LINK.search(cell)
    if not match:
        return ""
    return match.group(1) if match.re is A_TAG else match.group(2)


def md_posted(text: str, today: datetime) -> str:
    """ "3d", "5h", "2w", "1mo" (age) or "Oct 06" (date) as an ISO date."""
    text = text.strip().lower()
    if m := re.fullmatch(r"(\d+)\s*(h|d|w|mo)", text):
        days = int(m.group(1)) * {"h": 0, "d": 1, "w": 7, "mo": 30}[m.group(2)]
        return (today - timedelta(days=days)).date().isoformat()
    try:
        day = datetime.strptime(f"{text} {today.year}", "%b %d %Y")
    except ValueError:
        return ""
    if day > today + timedelta(days=1):  # "Dec 30" read in January belongs to last year
        day = day.replace(year=today.year - 1)
    return day.date().isoformat()


def parse_markdown_jobs(text: str, source: str, today: datetime | None = None) -> list[dict]:
    """Rows of every table with a company and a position column. Rows marked "↳"
    belong to the company above. Rows with no link are closed and are skipped."""
    today = today or datetime.now()
    out = []
    columns: dict[str, int] = {}
    company = ""
    for line in text.splitlines():
        if not line.startswith("|"):
            columns = {}
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue
        names = [md_cell_text(c).lower() for c in cells]
        if "company" in names and any(n in names for n in TITLE_COLUMNS):
            columns = {n: i for i, n in enumerate(names)}
            continue
        if not columns:
            continue
        get = lambda keys: next((cells[columns[k]] for k in keys if k in columns and columns[k] < len(cells)), "")
        name = md_cell_text(get(("company",)))
        company = company if name in ("↳", "") else name
        title_cell = get(TITLE_COLUMNS)
        url = md_cell_link(get(LINK_COLUMNS)) or md_cell_link(title_cell)
        if not url or "🔒" in line:
            continue
        url = re.sub(r"\?(utm_[^=&]+=[^&]*&?)+$", "", url)  # tracking only
        out.append(offer(md_cell_text(title_cell), url, company, md_cell_text(get(("location",))), md_posted(md_cell_text(get(DATE_COLUMNS)), today), source))
    return valid(out)


def markdown_fetcher(source: str):
    def fetch(cfg: dict, terms: list[str]) -> list[dict]:
        out = []
        for url in cfg.get("lists", []):
            try:
                out += parse_markdown_jobs(get_text(url), source)
            except Exception as error:
                out.append({"__error": f"{source} {url}: {describe(error)}"})
        return out

    return fetch


# ---- hiring.cafe ----

HIRING_CAFE_US_LOCATION = {
    "id": "user_country_us",
    "formatted_address": "United States",
    "types": ["country"],
    "geometry": {"location": {"lat": 37.09024, "lng": -95.712891}},
    "address_components": [{"long_name": "United States", "short_name": "US", "types": ["country", "political"]}],
    "options": {"flexible_regions": ["anywhere_in_continent", "anywhere_in_world"]},
}


def extract_next_data(page: str) -> dict:
    match = re.search(r'id="__NEXT_DATA__"[^>]*>([\s\S]*?)</script>', page)
    if not match:
        raise ValueError("no __NEXT_DATA__ in page")
    return json.loads(match.group(1))


def parse_hiring_cafe(hits) -> list[dict]:
    out = []
    for hit in hits if isinstance(hits, list) else []:
        if not hit or hit.get("is_expired"):
            continue
        v5 = hit.get("v5_processed_job_data") or {}
        info = hit.get("job_information") or {}
        posted = to_iso_date(v5.get("estimated_publish_date") or v5.get("estimated_publish_date_millis"))
        out.append(offer(info.get("title") or info.get("job_title_raw", ""), hit.get("apply_url", ""), v5.get("company_name", ""), v5.get("formatted_workplace_location", ""), posted, "hiringcafe"))
    return valid(out)


def fetch_hiring_cafe(cfg: dict, terms: list[str]) -> list[dict]:
    def search(term: str) -> list[dict]:
        state = {"searchQuery": term, "dateFetchedPastNDays": cfg.get("date_fetched_past_n_days", 30)}
        if cfg.get("us_only", True):
            state["locations"] = [HIRING_CAFE_US_LOCATION]
        page = get_text("https://hiring.cafe/?searchState=" + quote(json.dumps(state, separators=(",", ":")), safe=""), headers={"Accept": "text/html"})
        return parse_hiring_cafe(extract_next_data(page).get("props", {}).get("pageProps", {}).get("ssrHits", []))

    # Some of its searches get refused even on a good day, so it gets a few more chances.
    return over_terms("hiringcafe", terms, search, workers=cfg.get("workers", 2), delay=cfg.get("delay_ms", 800) / 1000, give_up=4)


# ---- RemoteOK ----


def parse_remoteok(rows) -> list[dict]:
    out = []
    for row in rows if isinstance(rows, list) else []:
        if not row or row.get("legal") or not (row.get("position") or row.get("id")):
            continue
        url = row.get("url") or (f"https://remoteok.com/remote-jobs/{row['slug']}" if row.get("slug") else "")
        out.append(offer(row.get("position") or row.get("title", ""), url, row.get("company", ""), clean(row.get("location")) or "Remote", to_iso_date(row.get("date") or row.get("epoch")), "remoteok"))
    return valid(out)


def fetch_remoteok(cfg: dict, terms: list[str]) -> list[dict]:
    try:
        return parse_remoteok(get_json("https://remoteok.com/api"))
    except Exception as error:
        return [{"__error": f"remoteok: {describe(error)}"}]


# ---- Remotive ----


def parse_remotive(data) -> list[dict]:
    jobs = (data or {}).get("jobs", [])
    return valid([offer(j.get("title"), j.get("url", ""), j.get("company_name", ""), j.get("candidate_required_location") or "Remote", to_iso_date(j.get("publication_date")), "remotive") for j in jobs])


def fetch_remotive(cfg: dict, terms: list[str]) -> list[dict]:
    out = []
    categories = cfg.get("categories", ["software-dev"])
    for i, category in enumerate(categories):
        try:
            out += parse_remotive(get_json(f"https://remotive.com/api/remote-jobs?category={quote(category)}"))
        except Exception as error:
            out.append({"__error": f"remotive {category}: {describe(error)}"})
        if i < len(categories) - 1:
            time.sleep(cfg.get("delay_ms", 2500) / 1000)  # stay under 2 requests a minute
    return out


# ---- Adzuna ----


def parse_adzuna(data) -> list[dict]:
    results = (data or {}).get("results", [])
    return valid([offer(r.get("title"), r.get("redirect_url", ""), (r.get("company") or {}).get("display_name", ""), (r.get("location") or {}).get("display_name", ""), to_iso_date(r.get("created")), "adzuna") for r in results])


def fetch_adzuna(cfg: dict, terms: list[str]) -> list[dict]:
    app_id = os.environ.get("ADZUNA_APP_ID")
    app_key = os.environ.get("ADZUNA_APP_KEY") or os.environ.get("ADZUNA_API_KEY")
    if not app_id or not app_key:
        raise NoCredentials("needs ADZUNA_APP_ID and ADZUNA_APP_KEY in .env")
    def search(term: str) -> list[dict]:
        out = []
        for page in range(1, cfg.get("max_pages", 1) + 1):
            params = {"app_id": app_id, "app_key": app_key, "what": term, "results_per_page": cfg.get("results_per_page", 50), "sort_by": "date", "content-type": "application/json"}
            if cfg.get("max_days_old"):
                params["max_days_old"] = cfg["max_days_old"]
            batch = parse_adzuna(get_json(f"https://api.adzuna.com/v1/api/jobs/{cfg.get('country', 'us')}/search/{page}?{urlencode(params)}"))
            out += batch
            if len(batch) < params["results_per_page"]:
                break
        return out

    return over_terms("adzuna", terms, search, workers=cfg.get("workers", 4), delay=cfg.get("delay_ms", 300) / 1000)


# ---- LinkedIn guest search ----


def parse_linkedin_guest(page: str) -> list[dict]:
    """Fields are read per job card, so a card missing one field never shifts the others."""
    out = []
    for card in re.split(r"<li[\s>]", page)[1:]:
        link = re.search(r'href="(https://[^"]*/jobs/view/[^"?]+)', card)
        title = re.search(r"base-search-card__title[^>]*>([\s\S]*?)</h3>", card)
        if not link or not title:
            continue
        company = re.search(r"(?:hidden-nested-link|base-search-card__subtitle-link)[^>]*>([\s\S]*?)</a>", card)
        location = re.search(r"job-search-card__location[^>]*>([\s\S]*?)</span>", card)
        date = re.search(r'datetime="([^"]+)"', card)
        out.append(offer(strip_html(title.group(1)), link.group(1), strip_html(company.group(1)) if company else "", strip_html(location.group(1)) if location else "", to_iso_date(date.group(1)) if date else "", "linkedin"))
    return valid(out)


def fetch_linkedin(cfg: dict, terms: list[str]) -> list[dict]:
    proxy = os.environ.get("LINKEDIN_PROXY") or os.environ.get("PROXY_URL") or None
    def search(term: str) -> list[dict]:
        out = []
        for page in range(cfg.get("pages", 1)):
            params = {"keywords": term, "location": cfg.get("location", "United States"), "geoId": cfg.get("geo_id", "103644278"), "f_TPR": cfg.get("time_posted", "r86400"), "sortBy": "DD", "start": page * 10}
            for key, name in (("experience", "f_E"), ("workplace_type", "f_WT"), ("job_type", "f_JT")):
                if cfg.get(key):
                    params[name] = str(cfg[key])
            cards = parse_linkedin_guest(get_text("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?" + urlencode(params), headers={"Accept": "text/html,application/xhtml+xml", "Accept-Language": "en-US,en;q=0.9"}, proxy=proxy))
            out += cards
            if not cards:
                break
        return out

    # A few at a time with a pause; two refusals in a row mean it is rate limiting.
    return over_terms("linkedin", terms, search, workers=cfg.get("workers", 2), delay=cfg.get("delay_ms", 1500) / 1000)


# ---- Greenhouse, Lever, Ashby boards ----


def parse_greenhouse(data, company: str) -> list[dict]:
    jobs = (data or {}).get("jobs", [])
    return valid([offer(j.get("title"), j.get("absolute_url", ""), clean(j.get("company_name")) or company, (j.get("location") or {}).get("name", ""), to_iso_date(j.get("first_published") or j.get("updated_at")), "greenhouse") for j in jobs])


def parse_lever(data, company: str) -> list[dict]:
    if not isinstance(data, list):
        return []
    out = []
    for job in data:
        location = clean((job.get("categories") or {}).get("location", ""))
        out.append(offer(job.get("text"), job.get("hostedUrl", ""), company, location or ("Remote" if job.get("workplaceType") == "remote" else ""), to_iso_date(job.get("createdAt")), "lever"))
    return valid(out)


def parse_ashby(data, company: str) -> list[dict]:
    out = []
    for job in (data or {}).get("jobs", []):
        if not job or job.get("isListed") is False:
            continue
        # Keep the real city so the US filter can judge it; "Remote" only when there is none.
        location = clean(job.get("location", "")) or ("Remote" if job.get("isRemote") else "")
        out.append(offer(job.get("title"), job.get("jobUrl") or job.get("applyUrl", ""), company, location, to_iso_date(job.get("publishedAt")), "ashby"))
    return valid(out)


def parse_smartrecruiters(data, company: str) -> list[dict]:
    out = []
    for job in (data or {}).get("content", []):
        location = job.get("location") or {}
        where = clean(location.get("fullLocation")) or ", ".join(p for p in (location.get("city"), location.get("region"), location.get("country")) if p)
        if location.get("remote") and not where:
            where = "Remote"
        url = f"https://jobs.smartrecruiters.com/{(job.get('company') or {}).get('identifier', '')}/{job.get('id', '')}"
        out.append(offer(job.get("name"), url, clean((job.get("company") or {}).get("name")) or company, where, to_iso_date(job.get("releasedDate")), "smartrecruiters"))
    return valid(out)


# SmartRecruiters companies can have hundreds of US postings, 100 to a page.
SMARTRECRUITERS_PAGES = 5


def fetch_smartrecruiters(cfg: dict, terms: list[str]) -> list[dict]:
    def one(board: dict) -> list[dict]:
        company = board["company"] or label_from_token(board["token"])
        out: list[dict] = []
        try:
            for page in range(cfg.get("max_pages", SMARTRECRUITERS_PAGES)):
                data = get_json(f"https://api.smartrecruiters.com/v1/companies/{quote(board['token'])}/postings?limit=100&offset={page * 100}&country=us")
                out += parse_smartrecruiters(data, company)
                if (page + 1) * 100 >= (data or {}).get("totalFound", 0):
                    break
        except Exception as error:
            out.append({"__error": f"smartrecruiters {board['token']}: {describe(error)}"})
        return out

    out: list[dict] = []
    with ThreadPoolExecutor(cfg.get("workers", 8)) as pool:
        for batch in pool.map(one, normalize_boards(cfg.get("boards"))):
            out += batch
    return out


BOARDS = {
    "greenhouse": ("https://boards-api.greenhouse.io/v1/boards/{}/jobs?content=true", parse_greenhouse),
    "lever": ("https://api.lever.co/v0/postings/{}?mode=json", parse_lever),
    "ashby": ("https://api.ashbyhq.com/posting-api/job-board/{}?includeCompensation=true", parse_ashby),
}


def board_fetcher(kind: str):
    url_format, parse = BOARDS[kind]

    def fetch(cfg: dict, terms: list[str]) -> list[dict]:
        def one(board: dict) -> list[dict]:
            try:
                return parse(get_json(url_format.format(quote(board["token"]))), board["company"] or label_from_token(board["token"]))
            except Exception as error:
                return [{"__error": f"{kind} {board['token']}: {describe(error)}"}]

        out: list[dict] = []
        with ThreadPoolExecutor(cfg.get("workers", 8)) as pool:
            for batch in pool.map(one, normalize_boards(cfg.get("boards"))):
                out += batch
        return out

    return fetch


# ---- The Muse ----


def parse_themuse(data) -> list[dict]:
    out = []
    for r in (data or {}).get("results", []):
        locations = " / ".join(clean(l.get("name")) for l in r.get("locations") or [] if l.get("name")) or "Flexible"
        out.append(offer(r.get("name"), (r.get("refs") or {}).get("landing_page", ""), (r.get("company") or {}).get("name", ""), locations, to_iso_date(r.get("publication_date")), "themuse"))
    return valid(out)


def fetch_themuse(cfg: dict, terms: list[str]) -> list[dict]:
    out = []
    max_pages = cfg.get("max_pages", 2)
    for page in range(1, max_pages + 1):
        params = [("page", page)] + [("category", c) for c in cfg.get("categories", ["Software Engineering"])] + [("location", l) for l in cfg.get("locations", ["United States"])]
        if os.environ.get("THEMUSE_API_KEY"):
            params.append(("api_key", os.environ["THEMUSE_API_KEY"]))
        try:
            data = get_json("https://www.themuse.com/api/public/jobs?" + urlencode(params))
            out += parse_themuse(data)
            if data.get("page_count") and page >= data["page_count"]:
                break
        except Exception as error:
            out.append({"__error": f"themuse page {page}: {describe(error)}"})
            break
        if page < max_pages:
            time.sleep(cfg.get("delay_ms", 800) / 1000)
    return out


# ---- Google Programmable Search over company job sites ----

TRUSTED_ATS_SITES = ["job-boards.greenhouse.io", "boards.greenhouse.io", "jobs.lever.co", "jobs.ashbyhq.com", "jobs.smartrecruiters.com", "myworkdayjobs.com"]


def company_from_ats_url(url: str) -> str:
    try:
        parsed = urlparse(url)
    except ValueError:
        return ""
    host = parsed.hostname or ""
    first = next((s for s in parsed.path.split("/") if s), "")
    if re.search(r"(^|\.)(greenhouse\.io|lever\.co|ashbyhq\.com|smartrecruiters\.com)$", host):
        return label_from_token(first)
    if host.endswith("myworkdayjobs.com"):
        return label_from_token(host.split(".")[0])
    return ""


def parse_google_jobs(data) -> list[dict]:
    out = []
    for item in (data or {}).get("items", []):
        url = item.get("link", "")
        pagemap = item.get("pagemap") or {}
        posting = (pagemap.get("jobposting") or [None])[0] or {}
        meta = (pagemap.get("metatags") or [None])[0] or {}
        title = posting.get("title") or item.get("title", "")
        company = clean(posting.get("hiringorganization", "")) or company_from_ats_url(url)
        location = posting.get("joblocation") or posting.get("addresslocality", "")
        out.append(offer(title, url, company, location, to_iso_date(posting.get("dateposted") or meta.get("article:published_time", "")), "googlejobs"))
    return valid(out)


def fetch_google_jobs(cfg: dict, terms: list[str]) -> list[dict]:
    key = os.environ.get("GOOGLE_CSE_KEY")
    cx = os.environ.get("GOOGLE_CSE_CX") or os.environ.get("GOOGLE_CSE_ID")
    if not key or not cx:
        raise NoCredentials("needs GOOGLE_CSE_KEY and GOOGLE_CSE_CX in .env")
    sites = cfg.get("sites") if isinstance(cfg.get("sites"), list) else TRUSTED_ATS_SITES
    site_filter = "(" + " OR ".join(f"site:{s}" for s in sites) + ")" if sites else ""
    out, queries = [], 0
    for term in terms or [""]:
        for page in range(cfg.get("pages_per_term", 1)):
            if queries >= cfg.get("max_queries", 90):
                out.append({"__error": "googlejobs: reached the daily query limit set in max_queries"})
                return out
            params = {"key": key, "cx": cx, "q": f"{term} {site_filter}".strip(), "num": 10, "start": page * 10 + 1}
            if cfg.get("date_restrict"):
                params["dateRestrict"] = cfg["date_restrict"]
            try:
                batch = parse_google_jobs(get_json("https://www.googleapis.com/customsearch/v1?" + urlencode(params)))
                queries += 1
                out += batch
                if len(batch) < 10:
                    break
            except Exception as error:
                out.append({"__error": f'googlejobs "{term}": {describe(error)}'})
                if isinstance(error, httpx.HTTPStatusError) and error.response.status_code in REFUSED:
                    out.append({"__error": "googlejobs: Google refused the search key, so the other terms were skipped. Check GOOGLE_CSE_KEY and that the Custom Search API is turned on."})
                    return out
                break
            time.sleep(cfg.get("delay_ms", 400) / 1000)
    return out


# ---- JobSpy (Indeed, Google, ZipRecruiter) ----


def parse_jobspy_rows(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows:
        city, state = clean(row.get("city") or ""), clean(row.get("state") or "")
        location = ", ".join(p for p in (city, state) if p) or clean(row.get("location") or "") or ("Remote" if row.get("is_remote") else "")
        job = offer(row.get("title"), row.get("job_url") or row.get("url", ""), row.get("company") or "", location, to_iso_date(str(row.get("date_posted") or "")), f"jobspy:{row.get('site') or 'job'}")
        # Indeed and ZipRecruiter block plain page requests later, so keep the description now.
        job["description"] = (row.get("description") or "").strip()
        out.append(job)
    return valid(out)


def fetch_jobspy(cfg: dict, terms: list[str]) -> list[dict]:
    try:
        from jobspy import scrape_jobs
    except ImportError as error:
        return [{"__error": f"jobspy: python-jobspy is not installed ({describe(error)})"}]
    proxies = list(cfg.get("proxies") or []) + [p.strip() for p in (os.environ.get("JOBSPY_PROXIES") or os.environ.get("PROXY_URL") or "").split(",") if p.strip()]
    sites = list(cfg.get("sites") or ["indeed", "google"])
    if proxies and cfg.get("linkedin_via_proxy", True) and "linkedin" not in sites:
        sites.append("linkedin")  # LinkedIn through JobSpy only survives behind a proxy
    def search(term: str) -> list[dict]:
        kwargs = dict(
            site_name=sites,
            search_term=term,
            google_search_term=f"{term} jobs near {cfg.get('location', 'United States')}".strip(),
            location=cfg.get("location", "United States"),
            results_wanted=cfg.get("results_per_site", 40),
            hours_old=cfg.get("hours_old", 168),
            country_indeed=cfg.get("country_indeed", "USA"),
            description_format="markdown",
            verbose=0,
        )
        if proxies:
            kwargs["proxies"] = proxies
        if cfg.get("remote"):
            kwargs["is_remote"] = True
        frame = scrape_jobs(**kwargs)
        if frame is None or len(frame) == 0:
            return []
        return parse_jobspy_rows(json.loads(frame.to_json(orient="records", date_format="iso")))

    return over_terms("jobspy", terms, search, workers=cfg.get("workers", 3), give_up=None)


FETCHERS = {
    "simplify": fetch_simplify,
    "speedyapply": markdown_fetcher("speedyapply"),
    "jobright": markdown_fetcher("jobright"),
    "hiringcafe": fetch_hiring_cafe,
    "linkedin": fetch_linkedin,
    "remoteok": fetch_remoteok,
    "remotive": fetch_remotive,
    "greenhouse": board_fetcher("greenhouse"),
    "lever": board_fetcher("lever"),
    "ashby": board_fetcher("ashby"),
    "smartrecruiters": fetch_smartrecruiters,
    "themuse": fetch_themuse,
    "googlejobs": fetch_google_jobs,
    "adzuna": fetch_adzuna,
    "jobspy": fetch_jobspy,
}

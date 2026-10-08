"""Finds a listed job on its company's own careers site, so the queue can apply there.

Jobs from LinkedIn, Indeed, ZipRecruiter and Jobright link to the job site, not to the
company's form. The form is found, in this order:

1. The employer's own link the job site gave with the listing (JobSpy's Indeed and
   ZipRecruiter results), after following click-tracking redirects.
2. The company's careers sites we already know. Every scanned job on Workday, Oracle,
   Greenhouse, Lever, Ashby, SuccessFactors or iCIMS tells us where that company
   posts (the `careers` table); those sites' public job searches are searched for
   the title.
3. Greenhouse, Lever and Ashby boards named after the company ("Scale AI" -> scaleai).
4. A web search: Brave Search (BRAVE_SEARCH_KEY) or Google Programmable Search
   (GOOGLE_CSE_KEY / GOOGLE_CSE_CX, whose free 100 a day are shared with the googlejobs
   scan source), within a daily allowance. Whatever careers site it turns up is
   remembered, so each company is looked up once.

A posting counts as the same job when the titles match after small differences are
set aside (a location, a team in brackets, a requisition number) and the level
agrees (intern, new grad, II, senior...). Close calls are put to the AI.
"""

import html
import json
import os
import re
import threading
import time
from datetime import datetime
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

import httpx
from dotenv import dotenv_values

from jobautomate.profile import ROOT
from jobautomate.scanner import store

# Raised when the search gets better, so jobs not found before are looked at again.
LOOKUP_VERSION = 2
HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129 Safari/537.36", "Accept-Language": "en-US,en;q=0.9"}
CACHE_SECONDS = 6 * 3600
GOOGLE_QUOTA = ROOT / "data" / "google_quota.json"
GOOGLE_DAILY_LIMIT = 100  # Google's free allowance per key per day
SEARCH_RETRY_DAYS = 30  # a company the web search found nothing for is searched again after this

_cache: dict[str, tuple[float, list[dict]]] = {}
_lock = threading.Lock()

# ---- company names ----

# Words that are part of a legal or descriptive name but rarely of a board's name.
SUFFIXES = r"\b(inc|incorporated|llc|ltd|limited|corp|corporation|co|company|plc|gmbh|technologies|technology|labs|group|holdings|the|industries|solutions|international|enterprises|ai|io|hq)\b"
# Job sites whose links are not the company's own form.
AGGREGATORS = re.compile(r"(^|\.)(linkedin\.com|indeed\.com|ziprecruiter\.com|jobright\.ai|glassdoor\.com|simplyhired\.com|dice\.com|builtin\.com|wellfound\.com|adzuna\.com|themuse\.com|hiring\.cafe)$")


def is_aggregator(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return bool(AGGREGATORS.search(host))


def norm(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (text or "").lower().replace("&", " and ")))


def company_key(name: str) -> str:
    """One key for the ways a company is written: "Anduril Industries, Inc." and
    "ANDURIL" both give "anduril"; "Athelas (Commure)" gives "athelas"."""
    name = re.sub(r"\(.*?\)", " ", (name or "").lower()).replace("&", " and ")
    core = re.findall(r"[a-z0-9]+", re.sub(SUFFIXES, " ", name))
    return "".join(core) or "".join(re.findall(r"[a-z0-9]+", name))


def board_names(company: str) -> list[str]:
    """Likely board names for a company: "Scale AI, Inc." gives scaleai, scale-ai, scale."""
    company = re.sub(r"\(.*?\)", " ", company)
    words = re.findall(r"[a-z0-9]+", company.lower().replace("&", "and"))
    core = re.findall(r"[a-z0-9]+", re.sub(SUFFIXES, " ", company.lower().replace("&", "and")))
    out = []
    for parts in (words, core):
        if parts:
            out += ["".join(parts), "-".join(parts)]
    if core and len(core[0]) >= 4:
        out.append(core[0])
    return list(dict.fromkeys(t for t in out if len(t) >= 2))


# ---- titles ----

STOP = {"the", "a", "an", "of", "and", "for", "in", "to", "with", "at", "on", "or", "role", "position", "opening", "job", "hiring"}
# Words that only say where the job is; they don't make it another job.
PLACE = {"us", "usa", "u", "s", "united", "states", "remote", "hybrid", "onsite", "on", "site", "in", "office", "north", "america", "na", "nyc", "sf", "bay", "area"}
LEVEL_WORDS = {
    "intern": "intern", "interns": "intern", "internship": "intern", "coop": "coop", "co": None, "op": None,
    "new": None, "grad": "newgrad", "graduate": "newgrad", "graduates": "newgrad", "entry": "entry", "early": "entry", "career": None,
    "junior": "junior", "jr": "junior", "associate": "associate", "senior": "senior", "sr": "senior", "staff": "staff",
    "principal": "principal", "lead": "lead", "manager": "manager", "director": "director", "head": "head",
    "i": "1", "ii": "2", "iii": "3", "iv": "4", "1": "1", "2": "2", "3": "3", "4": "4",
}
# Word forms that name the same thing in a title.
STEMS = {"engineering": "engineer", "engineers": "engineer", "development": "developer", "developers": "developer", "scientists": "scientist", "sciences": "science", "analysts": "analyst", "programs": "program", "systems": "system", "applications": "application", "apps": "app", "ml": "machine learning", "ai": "ai", "swe": "software engineer", "sde": "software engineer"}
# Levels that mean the same thing for matching.
LEVEL_SAME = {"entry": "newgrad", "junior": "1", "associate": "1"}


def title_parts(title: str, location: str = "") -> tuple[set[str], set[str]]:
    """(the title's words, its level words), with places, requisition numbers and
    bracketed notes set aside."""
    text = (title or "").lower().replace("&", " and ").replace("co-op", "coop").replace("new grad", "grad").replace("early career", "entry")
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = re.sub(r"\((?:[^()]*(?:remote|hybrid|onsite|on-site|usa?|united states|\b[a-z]{2}\b|\d{4})[^()]*)\)", " ", text)  # (Remote), (Austin, TX), (Summer 2027)
    text = re.sub(r"\b(?:req(?:uisition)?|job)?\s*#?\s*[a-z]?\d{5,}\b", " ", text)  # R123456, Req #12345
    text = re.sub(r"\b(summer|fall|spring|winter)\b|\b20\d\d\b", " ", text)
    words = " ".join(STEMS.get(w, w) for w in re.findall(r"[a-z0-9]+", text)).split()
    place = PLACE | set(re.findall(r"[a-z]+", (location or "").lower()))
    levels = {LEVEL_SAME.get(LEVEL_WORDS[w], LEVEL_WORDS[w]) for w in words if LEVEL_WORDS.get(w)}
    core = {w for w in words if w not in STOP and w not in place and w not in LEVEL_WORDS}
    return core, levels


def title_score(a: str, b: str, location: str = "", b_location: str = "") -> float:
    """1.0 for the same job title, down to 0. Different levels (intern and senior,
    II and III) score 0. `b_location` is the posting's own place, set aside from its
    title ("AI Engineer (New York)")."""
    if norm(a) == norm(b):
        return 1.0
    (core_a, level_a), (core_b, level_b) = title_parts(a, location), title_parts(b, f"{location} {b_location}")
    if level_a != level_b or not core_a or not core_b:
        return 0.0
    if core_a == core_b:
        return 0.97
    jaccard = len(core_a & core_b) / len(core_a | core_b)
    # One title is the other plus a team or focus after a comma, dash or bracket
    # ("Software Engineer" vs "Software Engineer, Platform"): close, but not certain.
    # Words put into the middle ("Software QA Automation Engineer") make another job.
    if core_a <= core_b or core_b <= core_a:
        shorter, longer = (a, b) if core_a <= core_b else (b, a)
        head = re.split(r",|\s[-–|:]\s|\(|/", longer.lower())[0]
        if title_parts(head, f"{location} {b_location}")[0] <= title_parts(shorter, f"{location} {b_location}")[0]:
            return max(jaccard, 0.75)
    return jaccard


_same_cache: dict[tuple[str, str], bool] = {}


def ai_same_job(company: str, listed: str, posted: str) -> bool:
    """Asks the AI whether two titles at one company are the same opening."""
    key = (norm(listed), norm(posted))
    if key in _same_cache:
        return _same_cache[key]
    from jobautomate.llm import chat

    try:
        reply = chat(
            [
                {"role": "system", "content": "You decide whether two job titles at the same company are the same opening, written two ways. Say no when the level differs (intern vs full time, I vs II, new grad vs senior), when the specialty differs (QA, test, data, security, hardware, sales vs software), or when the second is a different program or team (such as a venture or rotation program). Say yes only when a recruiter would call them the same job. Reply with only yes or no."},
                {"role": "user", "content": f"Company: {company}\nTitle on a job site: {listed}\nTitle on the company's careers site: {posted}"},
            ],
            temperature=0,
            max_tokens=3,
            model=os.environ.get("OPENROUTER_WRITING_MODEL") or None,  # the stronger model: a wrong yes queues the wrong job
        )
        same = reply.strip().lower().startswith("y")
    except Exception:
        same = False
    _same_cache[key] = same
    return same


def best_posting(company: str, title: str, location: str, postings: list[dict]) -> dict | None:
    """The posting that is the listed job, if any. Clear matches win; a close call
    goes to the AI. Several openings with one title: the one in the same city."""
    from jobautomate.answers import NON_US_PLACE, US_PLACE

    # Openings outside the United States are left out (user, 2026-10-07), unless the
    # listing itself is abroad.
    if not location or US_PLACE.search(location) or not NON_US_PLACE.search(location):
        postings = [p for p in postings if not (NON_US_PLACE.search(p.get("location") or "") and not US_PLACE.search(p.get("location") or ""))]
    scored = sorted(((title_score(title, p["title"], location, p.get("location", "")), p) for p in postings if p.get("url") and p.get("title")), key=lambda x: -x[0])
    if not scored or scored[0][0] < 0.6:
        return None
    top = scored[0][0]
    if top < 0.85:
        candidates = [p for s, p in scored[:3] if s >= 0.6 and ai_same_job(company, title, p["title"])]
    else:
        candidates = [p for s, p in scored if s >= top - 0.001]
    if not candidates:
        return None
    city = norm((location or "").split(",")[0])
    near = [p for p in candidates if city and city in norm(p.get("location", ""))]
    return (near or candidates)[0]


# ---- careers sites: where each company posts, read from links ----


def site_of(url: str) -> tuple[str, str] | None:
    """(system, base) for a link to a job or careers page, or None. base is what that
    system's search needs: a board name, or a site address."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    path = [p for p in parsed.path.split("/") if p]
    if host.endswith(".myworkdayjobs.com") or host.endswith(".myworkdaysite.com"):
        parts = [p for p in path if not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", p)]
        if parts and parts[0] not in ("wday", "job"):
            return "workday", f"{host}/{parts[0]}"
        return None
    if host.endswith(".oraclecloud.com") and "CandidateExperience" in parsed.path:
        match = re.search(r"/sites/([^/]+)", parsed.path)
        return ("oracle", f"{host}/{match.group(1)}") if match else None
    if host.endswith("greenhouse.io"):
        board = parse_qs(parsed.query).get("for", [None])[0] or (path[0] if path and path[0] not in ("embed", "v1") else None)
        return ("greenhouse", board) if board else None
    if host in ("jobs.lever.co", "jobs.eu.lever.co") and path:
        return "lever", path[0]
    if host == "jobs.ashbyhq.com" and path:
        return "ashby", path[0]
    if host.endswith(".icims.com"):
        return "icims", host
    if "ats=successfactors" in parsed.query or re.search(r"career\d*\.successfactors\.|\.sapsf\.", host):
        return "successfactors", host
    return None


def learn(company: str, url: str, source: str) -> None:
    """Remembers where a company posts, from one of its job links."""
    found = site_of(url)
    key = company_key(company)
    if found and key:
        with store.connect() as db:
            db.execute("INSERT OR REPLACE INTO careers (company, system, base, source, updated) VALUES (?, ?, ?, ?, ?)", (key, *found, source, datetime.now().isoformat(timespec="seconds")))


def learn_from_scans() -> int:
    """Fills the careers table from every scanned job on a known careers system."""
    rows = []
    now = datetime.now().isoformat(timespec="seconds")
    with store.connect() as db:
        for job in db.execute("SELECT company, url, ats_url FROM jobs"):
            for link in (job["ats_url"], job["url"]):
                found = link and not is_aggregator(link) and site_of(link)
                if found and company_key(job["company"]):
                    rows.append((company_key(job["company"]), *found, "scan", now))
        db.executemany("INSERT OR IGNORE INTO careers (company, system, base, source, updated) VALUES (?, ?, ?, ?, ?)", rows)
    return len(rows)


def known_sites(company: str) -> list[tuple[str, str]]:
    with store.connect() as db:
        return [(r["system"], r["base"]) for r in db.execute("SELECT system, base FROM careers WHERE company = ? ORDER BY source = 'scan' DESC", (company_key(company),))]


# ---- each system's public job search ----


def cached(key: str, fetch) -> list[dict]:
    with _lock:
        hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    try:
        postings = fetch()
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return []  # not cached, so a hiccup is tried again later
    with _lock:
        _cache[key] = (time.time(), postings)
    return postings


def search_workday(base: str, title: str) -> list[dict]:
    host, site = base.split("/", 1)
    tenant = host.split(".")[0]

    def fetch():
        response = httpx.post(f"https://{host}/wday/cxs/{tenant}/{site}/jobs", json={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": title}, headers={**HEADERS, "Accept": "application/json"}, timeout=20)
        response.raise_for_status()
        return [{"title": j.get("title", ""), "url": f"https://{host}/{site}{j['externalPath']}", "location": j.get("locationsText", "")} for j in response.json().get("jobPostings", []) if j.get("externalPath")]

    return cached(f"workday:{base}:{norm(title)}", fetch)


def search_oracle(base: str, title: str) -> list[dict]:
    host, site = base.split("/", 1)
    keyword = re.sub(r'["\\,;]', " ", title)

    def fetch():
        response = httpx.get(
            f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions",
            params={"onlyData": "true", "expand": "requisitionList.secondaryLocations", "finder": f'findReqs;siteNumber={site},limit=25,keyword="{keyword}"'},
            headers={**HEADERS, "Accept": "application/json"},
            timeout=20,
        )
        response.raise_for_status()
        items = response.json().get("items") or [{}]
        return [{"title": r.get("Title", ""), "url": f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{r['Id']}", "location": r.get("PrimaryLocation", "")} for r in items[0].get("requisitionList", []) if r.get("Id")]

    return cached(f"oracle:{base}:{norm(title)}", fetch)


def search_board(system: str, board: str) -> list[dict]:
    """A whole Greenhouse, Lever or Ashby board (they are small enough to read at once)."""
    urls = {
        "greenhouse": f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs",
        "lever": f"https://api.lever.co/v0/postings/{board}?mode=json",
        "ashby": f"https://api.ashbyhq.com/posting-api/job-board/{board}",
    }

    def fetch():
        response = httpx.get(urls[system], timeout=15, follow_redirects=True)
        if response.status_code != 200:
            return []
        data = response.json()
        if system == "greenhouse":
            return [{"title": j.get("title", ""), "url": j.get("absolute_url", ""), "location": (j.get("location") or {}).get("name", "")} for j in data.get("jobs", [])]
        if system == "lever":
            return [{"title": j.get("text", ""), "url": j.get("hostedUrl", ""), "location": (j.get("categories") or {}).get("location", "")} for j in data] if isinstance(data, list) else []
        return [{"title": j.get("title", ""), "url": j.get("jobUrl", ""), "location": j.get("location", "")} for j in data.get("jobs", []) if j.get("isListed", True)]

    return cached(f"{system}:{board}", fetch)


def search_icims(host: str, title: str) -> list[dict]:
    def fetch():
        response = httpx.get(f"https://{host}/jobs/search", params={"ss": "1", "searchKeyword": title, "in_iframe": "1"}, headers=HEADERS, timeout=20, follow_redirects=True)
        response.raise_for_status()
        out = {}
        for anchor in re.finditer(r"<a\b[^>]*href=\"(https://[^\"]+/jobs/\d+/[^\"?]+/job)[^\"]*\"[^>]*>(.*?)</a>", response.text, re.S):
            label = html.unescape(re.sub(r"<[^>]+>", " ", anchor.group(2))).strip()
            attr = re.search(r'title="(?:\d+\s*-\s*)?([^"]+)"', anchor.group(0))
            name = " ".join((html.unescape(attr.group(1)) if attr else label).split())
            if name and name.lower() != "title":
                out.setdefault(anchor.group(1), {"title": name, "url": anchor.group(1), "location": ""})
        return list(out.values())

    return cached(f"icims:{host}:{norm(title)}", fetch)


def search_successfactors(host: str, title: str) -> list[dict]:
    def fetch():
        response = httpx.get(f"https://{host}/search/", params={"q": title, "locale": "en_US"}, headers=HEADERS, timeout=20, follow_redirects=True)
        response.raise_for_status()
        out = {}
        for anchor in re.finditer(r"<a\b[^>]*class=\"jobTitle-link\"[^>]*>(.*?)</a>|<a\b[^>]*>(.*?)</a>", response.text, re.S):
            tag = anchor.group(0)
            href = re.search(r'href="(/job/[^"]+)"', tag)
            if not href or "jobTitle-link" not in tag:
                continue
            name = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", anchor.group(1) or anchor.group(2) or "")).split())
            url = f"https://{host}{href.group(1)}?ats=successfactors"
            out.setdefault(url, {"title": name, "url": url, "location": ""})
        return list(out.values())

    return cached(f"sf:{host}:{norm(title)}", fetch)


def search(system: str, base: str, title: str) -> list[dict]:
    if system == "workday":
        return search_workday(base, title)
    if system == "oracle":
        return search_oracle(base, title)
    if system in ("greenhouse", "lever", "ashby"):
        return search_board(system, base)
    if system == "icims":
        return search_icims(base, title)
    if system == "successfactors":
        return search_successfactors(base, title)
    return []


# ---- the employer's own link from the listing ----

# Click-tracking hosts that carry the real address in a parameter, or only redirect.
TRACKER_PARAMS = ("rx_url", "url", "u", "dest", "destination", "redirect", "target")


def resolve(url: str) -> str:
    """The page a listing's employer link finally leads to."""
    if not url:
        return ""
    for _ in range(2):  # a link inside a link
        query = parse_qs(urlparse(url).query)
        inner = next((unquote(query[p][0]) for p in TRACKER_PARAMS if p in query and query[p][0].startswith("http")), None)
        if not inner or site_of(url):
            break
        url = inner
    if site_of(url):
        return url
    try:
        response = httpx.get(url, headers=HEADERS, timeout=15, follow_redirects=True)
        final = str(response.url)
        # Pages that move on with a script or a meta refresh.
        moved = re.search(r"""(?:window\.location(?:\.href)?\s*=|http-equiv=["']refresh["'][^>]*url=)\s*["']?(https?://[^"'\s>]+)""", response.text[:20000], re.I)
        if not site_of(final) and moved:
            final = html.unescape(moved.group(1))
        return final
    except httpx.HTTPError:
        return url


# ---- Google ----


def google_left() -> int:
    """Searches left today on the Google key, shared with the googlejobs scan source."""
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        data = json.loads(GOOGLE_QUOTA.read_text())
    except (OSError, ValueError):
        data = {}
    return max(0, GOOGLE_DAILY_LIMIT - (data.get(today, 0) if isinstance(data, dict) else 0))


def google_used(count: int = 1) -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    with _lock:
        try:
            data = json.loads(GOOGLE_QUOTA.read_text())
        except (OSError, ValueError):
            data = {}
        data = {today: data.get(today, 0) + count}  # only today's count is kept
        GOOGLE_QUOTA.parent.mkdir(parents=True, exist_ok=True)
        GOOGLE_QUOTA.write_text(json.dumps(data))


_search_today = {"day": "", "count": 0, "google_refused": ""}


def search_keys() -> dict:
    env = {**dotenv_values(ROOT / ".env"), **os.environ}
    return {"brave": env.get("BRAVE_SEARCH_KEY") or "", "google": env.get("GOOGLE_CSE_KEY") or "", "cx": env.get("GOOGLE_CSE_CX") or env.get("GOOGLE_CSE_ID") or ""}


def web_results(query: str) -> list[dict] | None:
    """[{title, url}] for a web search, or None when no search service can be used.
    Brave Search (BRAVE_SEARCH_KEY) is used when set, else Google Programmable Search
    (GOOGLE_CSE_KEY / GOOGLE_CSE_CX), whose free 100 a day are shared with the
    googlejobs scan source."""
    keys = search_keys()
    today = datetime.now().strftime("%Y-%m-%d")
    if keys["brave"]:
        response = httpx.get("https://api.search.brave.com/res/v1/web/search", params={"q": query, "count": 10}, headers={"X-Subscription-Token": keys["brave"], "Accept": "application/json"}, timeout=20)
        response.raise_for_status()
        return [{"title": r.get("title", ""), "url": r.get("url", "")} for r in (response.json().get("web") or {}).get("results", [])]
    if keys["google"] and keys["cx"] and _search_today["google_refused"] != today and google_left() > 0:
        google_used()
        response = httpx.get("https://www.googleapis.com/customsearch/v1", params={"key": keys["google"], "cx": keys["cx"], "q": query, "num": 10}, timeout=20)
        if response.status_code == 403:
            _search_today["google_refused"] = today  # no access to the API; not asked again today
            return None
        response.raise_for_status()
        return [{"title": i.get("title", ""), "url": i.get("link", "")} for i in response.json().get("items", [])]
    return None


def search_status() -> str:
    """Which web search the careers lookup can use, in words, for the settings page."""
    keys = search_keys()
    if keys["brave"]:
        return "Brave Search"
    if keys["google"] and keys["cx"]:
        return "Google (refused today: the key's project has no access to the Custom Search API)" if _search_today["google_refused"] == datetime.now().strftime("%Y-%m-%d") else "Google"
    return "none (add BRAVE_SEARCH_KEY to .env)"


def web_search(company: str, title: str, allowance: int) -> list[dict]:
    """Search results for the company's job ([{title, url}]), within `allowance`
    lookups today. Each company is searched once (again after SEARCH_RETRY_DAYS)."""
    company_id = company_key(company)
    if not company_id:
        return []
    lookup = f"web:{company_id}"
    with store.connect() as db:
        done = db.execute("SELECT at FROM lookups WHERE key = ?", (lookup,)).fetchone()
    if done and (datetime.now() - datetime.fromisoformat(done["at"])).days < SEARCH_RETRY_DAYS:
        return []
    today = datetime.now().strftime("%Y-%m-%d")
    with _lock:
        if _search_today["day"] != today:
            _search_today.update(day=today, count=0)
        if _search_today["count"] >= allowance:
            return []
        _search_today["count"] += 1
    try:
        found = web_results(f"{company} {title} careers apply")
    except (httpx.HTTPError, ValueError):
        return []
    if found is None:
        return []
    results = [{"title": re.split(r"\s+[-|@]\s+|\s+at\s+", r["title"])[0], "url": r["url"]} for r in found]
    careers_links = [r["url"] for r in results if site_of(r["url"])]
    with store.connect() as db:
        db.execute("INSERT OR REPLACE INTO lookups (key, result, at) VALUES (?, ?, ?)", (lookup, careers_links[0] if careers_links else "", datetime.now().isoformat(timespec="seconds")))
    return results


# ---- putting it together ----


def find(job: dict, google_allowance: int = 0) -> tuple[str, str]:
    """(the job's own application page or "", how it was found)."""
    company, title, location = job.get("company") or "", job.get("title") or "", job.get("location") or ""
    # 1. The employer's own link from the listing.
    direct = resolve(job.get("direct_url") or "")
    if direct and site_of(direct):
        learn(company, direct, "scan")
        return direct, "the job site's own link"
    if direct and not is_aggregator(direct) and urlparse(direct).scheme == "https":
        # The company's own site, on a system the queue doesn't know: still the real
        # form, for Apply on Find jobs (the extension fills it there).
        return direct, "the job site's own link"
    if not company or not title:
        return "", ""
    # 2. Careers sites we know for this company.
    seen = set()
    for system, base in known_sites(company):
        seen.add((system, base))
        posting = best_posting(company, title, location, search(system, base, title))
        if posting:
            return posting["url"], f"{company}'s careers site"
    # 3. Greenhouse, Lever and Ashby boards named after the company.
    for board in board_names(company):
        for system in ("greenhouse", "lever", "ashby"):
            if (system, board) in seen:
                continue
            postings = search_board(system, board)
            if postings:
                learn(company, postings[0]["url"], "scan")
            posting = best_posting(company, title, location, postings)
            if posting:
                return posting["url"], f"{company}'s {system.title()} board"
    # 4. A web search, for companies whose careers site we don't know yet.
    if google_allowance > 0:
        results = web_search(company, title, google_allowance)
        for result in results:
            if site_of(result["url"]):
                learn(company, result["url"], "google")
        posting = best_posting(company, title, location, [r for r in results if site_of(r["url"]) and re.search(r"/job|/jobs/|/position|/requisitions/|/postings?/|ashbyhq\.com/[^/]+/[0-9a-f-]{20,}", r["url"])])
        if posting:
            return posting["url"], "a web search"
        for system, base in known_sites(company):
            if (system, base) not in seen:
                posting = best_posting(company, title, location, search(system, base, title))
                if posting:
                    return posting["url"], f"{company}'s careers site (found with a web search)"
    return "", ""

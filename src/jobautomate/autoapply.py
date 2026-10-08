"""Auto apply queue: fills queued jobs in a headless Chrome and submits a job only
when every field matched, nothing needs your answer, there is no warning (such as
"does not sponsor visas") and no captcha. Anything else is kept under Needs you,
to finish in your own browser.

Only Greenhouse, Lever and Ashby forms are sent by themselves: their questions
come from the site's own data, so "every field matched" covers every question.
A daily limit caps how many go out. Submitted jobs go into the tracker once the
site shows its confirmation.
"""

import json
import random
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import parse_qs, urlparse

import httpx
from dotenv import dotenv_values

from jobautomate import greenhouse, oracle, otp, tracker
from jobautomate.prepare import citizenship_warning, log_application, site_for
from jobautomate.profile import ROOT, load_profile
from jobautomate.scanner import careers
from jobautomate.scanner import run as scan_run
from jobautomate.scanner import store

SERVER = "http://127.0.0.1:8765"
SHOTS = ROOT / "data" / "autoapply"
# Sites the queue may submit on by itself (the user approved Workday and Rippling on
# 2026-10-07, and Oracle Cloud and SuccessFactors on 2026-10-08).
SITES = {"greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby", "workday": "Workday", "rippling": "Rippling", "oracle": "Oracle Cloud", "successfactors": "SuccessFactors", "icims": "iCIMS", "adp": "ADP"}
# Sites the queue takes but always hands to you: iCIMS shows an hCaptcha challenge
# as soon as its email step is sent, and captchas are never solved.
# ADP's first step is checked by an invisible reCAPTCHA, and from the queue's browser
# the emailed code never arrives (checked 2026-10-08); bot checks are never worked around.
HANDOFF_SITES = {
    "icims": "iCIMS asks for a captcha right after its email step, which you need to do yourself",
    "adp": "ADP only emails its sign-in code to a normal browser, so this one needs yours",
}
# web_lookups_per_day: web searches (Brave Search, or Google whose free 100 a day are
# shared with the googlejobs scan source) a day for finding companies' careers sites.
DEFAULTS = {"daily_cap": 15, "submit": True, "min_fit": 70, "sites": list(SITES), "gap_seconds": 45, "code_wait_minutes": 10, "web_lookups_per_day": 40}
# Jobs prepared ahead of the one being filled: their answers and cover letters are
# written while the queue works, so each fill starts with everything ready.
WARM_AHEAD = 3
# Network hiccups get another try before a job counts as failed.
TRANSIENT = re.compile(r"net::ERR_|Timeout \d+ms exceeded|timed out|ECONNRESET|Target (page|closed)|502|503|504", re.I)
# Red flags that mean the job is not open to you; these never go in the queue.
HARD_FLAGS = re.compile(r"sponsor|citizen|clearance|green card|permanent resident", re.I)

# The extension's content scripts run in the page with chrome.runtime stubbed, the
# same way tests/run_extension.py runs them; API calls come back through __jaApi.
STUB = """
window.chrome = window.chrome || {};
window.__listeners = [];
chrome.runtime = {
  sendMessage: (message, callback) => { if (message.type !== 'api') return callback && callback(null); window.__jaApi(JSON.stringify(message)).then(r => callback && callback(JSON.parse(r))); },
  onMessage: { addListener: (fn) => window.__listeners.push(fn) },
  lastError: undefined,
};
chrome.storage = { local: { get: (defaults, callback) => (callback ? callback(defaults) : Promise.resolve(defaults)), set: () => {} } };
"""

TRIGGER = """
() => new Promise(resolve => {
  for (const fn of window.__listeners) {
    if (fn({ type: "fill" }, {}, resolve) === true) return;
  }
  resolve({ summary: "no listener took the fill message", failed: true });
})
"""

FORM_READY = (
    '[id="first_name"], [data-field-path], input[name="email"], [data-testid="input-first_name"], input[type="email"], input[type="file"], '
    # Workday: its sign-in screens and application steps
    '[data-automation-id="progressBarActiveStep"], [data-automation-id="SignInWithEmailButton"], [data-automation-id="signInLink"], [data-automation-id="applyManually"], '
    # Oracle: its email step, code step and application sections
    'input[name="primary-email"], [id^="pin-code-"], .apply-flow-section, '
    # SuccessFactors: the posting's Apply button, sign-in, new account and the form
    'a.dialogApplyBtn, [id^="applyOption-"], input#username, #fbclc_userName, #careerform'
)
CLOSED = re.compile(r"no longer (accepting|available|open)|job (is )?(not found|closed)|position has been filled|page (you('re| are) looking for )?(was )?not found", re.I)
DONE = re.compile(
    r"thank(s| you)\b.{0,60}\b(applying|application|submitting|interest)|application (has been |was )?(successfully )?(submitted|received|sent)"
    r"|we('ve| have) received your application|successfully (applied|submitted)|application submitted|you('ve| have) (successfully )?applied|congratulations",
    re.I,
)
EMAIL_CODE = re.compile(r"security code|verification code|enter the code|code (was |has been )?sent to", re.I)
# The site turned the submission away as automated. This is never worked around;
# the job goes to you to finish in your own browser.
SPAM = re.compile(r"flagged as (possible |potential )?spam|couldn.t submit your application|suspicious activity|automated (submission|traffic)", re.I)

# Required boxes still empty just before Submit. Search boxes of custom dropdowns are
# skipped (they stay empty after a pick); their hidden mirror inputs are checked instead.
REQUIRED_EMPTY = """
() => {
  const text = (e) => (e ? e.innerText || e.textContent || '' : '').replace(/\\s+/g, ' ').replace(/\\*/g, '').trim();
  const labelOf = (e) => {
    const byFor = e.id && document.querySelector(`label[for="${CSS.escape(e.id)}"]`);
    const byIds = (e.getAttribute('aria-labelledby') || '').split(' ').map((id) => document.getElementById(id)).filter(Boolean);
    const box = e.closest('fieldset');
    return (text(byFor) || byIds.map(text).join(' ') || e.getAttribute('aria-label') || text(box && box.querySelector('legend')) ||
      text(e.closest('.field-wrapper, .select__container, [data-field-path], .application-question, li, .form-group') ) || e.name || e.id || 'a required field').slice(0, 80);
  };
  const out = [], names = new Set();
  for (const e of document.querySelectorAll('input, textarea, select')) {
    if (e.closest('#job-autofill-panel') || e.disabled) continue;
    if (!(e.required || e.getAttribute('aria-required') === 'true')) continue;
    const type = (e.type || '').toLowerCase();
    if (['hidden', 'submit', 'button', 'file', 'search'].includes(type) || e.getAttribute('role') === 'combobox') continue;
    if (type === 'checkbox' || type === 'radio') {
      if (e.name && names.has(e.name)) continue;
      names.add(e.name);
      const group = e.name ? [...document.querySelectorAll(`input[name="${CSS.escape(e.name)}"]`)] : [e];
      if (!group.some((x) => x.checked)) out.push(labelOf(e));
      continue;
    }
    const mirror = e.getAttribute('aria-hidden') === 'true';
    if (!mirror && e.offsetParent === null) continue;
    if (!String(e.value || '').trim()) out.push(mirror ? labelOf(e.closest('.select, .field-wrapper') || e) : labelOf(e));
  }
  // Radios drawn without inputs (role="radio" in a radiogroup), marked required by a star.
  for (const box of document.querySelectorAll('[role="radiogroup"]')) {
    if (box.closest('#job-autofill-panel') || !box.offsetParent) continue;
    const named = (box.getAttribute('aria-labelledby') || '').split(' ').map((id) => document.getElementById(id)).filter(Boolean);
    const label = named.map((e) => e.innerText || '').join(' ') || box.getAttribute('aria-label') || '';
    const required = box.getAttribute('aria-required') === 'true' || /\\*/.test(label) || Boolean(named.some((e) => e.querySelector('[class*="required" i]')));
    const radios = [...box.querySelectorAll('[role="radio"]')];
    if (required && radios.length && !radios.some((r) => r.getAttribute('aria-checked') === 'true' || r.checked)) out.push(text({ innerText: label }).slice(0, 80) || 'a required choice');
  }
  return [...new Set(out)].slice(0, 8);
}
"""

# Visible captcha boxes and challenges. Invisible reCAPTCHA (size=invisible) and
# hCaptcha's invisible checkbox are left out: they pass without anyone clicking.
CAPTCHAS = """
() => [...document.querySelectorAll('iframe')].filter(f => {
  const r = f.getBoundingClientRect(), s = getComputedStyle(f);
  return r.width > 50 && r.height > 50 && r.bottom > 0 && s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0';
}).map(f => f.src || '').filter(src =>
  /recaptcha\\/(api2|enterprise)\\/(anchor|bframe)|hcaptcha\\.com|challenges\\.cloudflare\\.com|arkoselabs|funcaptcha/.test(src) &&
  !/size=invisible|checkbox-invisible/.test(src))
"""

ERRORS = """
() => [...new Set([...document.querySelectorAll('[role=alert], .error, .error-message, .field-error, [class*="error" i]')]
  .filter(e => e.offsetParent && e.id !== 'job-autofill-panel' && e.innerText.trim() && e.innerText.length < 200)
  .map(e => e.innerText.trim()))].slice(0, 5)
"""

_state: dict = {"running": False, "stop": False, "skip": False, "submitting": False, "chrome": None, "current": None, "message": "", "code_request": None, "code": None}
_lock = threading.Lock()


def content_scripts() -> str:
    manifest = json.loads((ROOT / "extension" / "manifest.json").read_text())
    return "\n".join((ROOT / "extension" / name).read_text() for name in manifest["content_scripts"][0]["js"])


# ---- settings (the auto_apply section of profile/scanner.yaml) ----


def settings() -> dict:
    saved = scan_run.plain(scan_run.load_config().get("auto_apply") or {})
    return {**DEFAULTS, **{k: v for k, v in saved.items() if k in DEFAULTS}}


def save_settings(changes: dict) -> dict:
    config = scan_run.load_config()
    if "auto_apply" not in config:
        config["auto_apply"] = {}
    for key in DEFAULTS:
        if key in changes:
            config["auto_apply"][key] = changes[key]
    scan_run.write_config(config)
    return settings()


# ---- the queue ----


# Sites whose forms are read from the page (no module of their own on the server).
PAGE_SITES = {
    # Company career sites on SuccessFactors (the scanner marks them ?ats=successfactors)
    # and its own apply hosts, including the U.S. government cloud (ns2cloud.com).
    "successfactors": re.compile(r"[?&]ats=successfactors\b|//career\d*\.successfactors\.(com|eu)/|\.sapsf\.(com|eu|cn)/|//career[\w-]*\.ns2cloud\.com/", re.I),
    "icims": re.compile(r"\.icims\.com/|[?&]icims=1\b", re.I),
    "adp": re.compile(r"//(workforcenow|myjobs)\.adp\.com/", re.I),
}


def site_key(url: str) -> str | None:
    try:
        return site_for(url)[0].__name__.rsplit(".", 1)[-1]
    except ValueError:
        return next((key for key, pattern in PAGE_SITES.items() if pattern.search(url)), None)


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def submitted_today() -> int:
    with store.connect() as db:
        return db.execute("SELECT COUNT(*) FROM queue WHERE status = 'submitted' AND finished_at >= ?", (today(),)).fetchone()[0]


def entries() -> list[dict]:
    with store.connect() as db:
        rows = db.execute(
            "SELECT q.*, j.url, j.ats_url, j.title, j.company, j.location, j.fit, j.deep_fit, j.legacy_applied FROM queue q JOIN jobs j ON j.id = q.job_id ORDER BY q.added_at"
        ).fetchall()
    applied = scan_run.applied_check()
    result = []
    for row in rows:
        entry = dict(row)
        entry["reasons"] = json.loads(entry["reasons"] or "[]")
        entry["ai_answers"] = json.loads(entry["ai_answers"] or "[]")
        entry["apply_url"] = scan_run.apply_url(job_link(entry))
        entry["applied"] = applied(entry)
        entry["site"] = SITES.get(site_key(entry["apply_url"]) or "", "")
        entry["has_screenshot"] = (SHOTS / f"{entry['job_id']}.png").exists()
        result.append(entry)
    return result


def job_link(job: dict) -> str:
    """Where the job's application lives: its own form when one was found on the
    company's board (for jobs listed on LinkedIn and the like), else its link."""
    return job.get("ats_url") or job["url"]


def needs_lookup(job: dict) -> bool:
    """A job from LinkedIn, Indeed and the like whose own form hasn't been looked for
    yet, or was looked for by an older, weaker search."""
    if not careers.is_aggregator(job["url"]):
        return False
    return job.get("ats_url") is None or (not job.get("ats_url") and (job.get("ats_lookup") or 0) < careers.LOOKUP_VERSION)


def find_form(job: dict, google: int) -> tuple[str, str]:
    try:
        return careers.find(job, google)
    except Exception:
        return "", ""  # tried again with the next search version


def match_forms(jobs: list[dict], google: int | None = None) -> int:
    """Looks for the company's own application form for jobs listed on LinkedIn,
    Indeed and other job sites (see scanner/careers.py), and remembers what it
    found, or that there was nothing."""
    todo = [j for j in jobs if needs_lookup(j)]
    if not todo:
        return 0
    careers.learn_from_scans()
    allowance = settings()["web_lookups_per_day"] if google is None else google
    found = 0
    with ThreadPoolExecutor(8) as pool:
        for job, (link, _how) in zip(todo, pool.map(lambda j: find_form(j, allowance), todo)):
            store.update_job(job["id"], ats_url=link, ats_lookup=careers.LOOKUP_VERSION)
            job["ats_url"] = link
            found += bool(link)
    return found


def why_not(job: dict, allowed: list[str], applied) -> str:
    """Why a job can't go in the queue, or "" if it can."""
    site = site_key(scan_run.apply_url(job_link(job)))
    if applied(job):
        return "already applied"
    if site not in allowed:
        return f"only {', '.join(SITES[s] for s in allowed if s in SITES)} forms are sent by themselves; use Apply for this one"
    if any(HARD_FLAGS.search(flag) for flag in job.get("red_flags") or []):
        return "the AI found a sponsorship or citizenship red flag"
    return ""


def add(job_ids: list[str]) -> dict:
    """Adds jobs to the queue. Jobs that needed you or failed before go back in line."""
    s = settings()
    applied = scan_run.applied_check()
    added, skipped = [], []
    with store.connect() as db:
        existing = {r["job_id"]: r["status"] for r in db.execute("SELECT job_id, status FROM queue")}
    for job_id in job_ids:
        job = store.get_job(job_id)
        if not job:
            skipped.append({"id": job_id, "reason": "no such job"})
            continue
        reason = why_not(job, s["sites"], applied)
        if not reason and existing.get(job_id) in ("queued", "running", "submitted"):
            reason = "already in the queue" if existing[job_id] != "submitted" else "already submitted"
        if reason:
            skipped.append({"id": job_id, "title": job["title"], "company": job["company"], "reason": reason})
            continue
        with store.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO queue (job_id, added_at, status, reasons, ai_answers, page_url, finished_at) VALUES (?, ?, 'queued', '[]', '[]', '', NULL)",
                (job_id, tracker.now()),
            )
        added.append(job_id)
    return {"added": added, "skipped": skipped}


def add_link(listing_url: str, apply_url: str, company: str, title: str, location: str = "", description: str = "") -> dict:
    """A job you opened on LinkedIn, with the company's own page its Apply button led
    to (read by the extension's Queue this button). The job is saved to Find jobs and
    put in the queue when the queue can apply on that site."""
    from jobautomate.scanner.filters import company_role_key, fit_scorer

    real = careers.resolve(apply_url)
    if careers.is_aggregator(real):
        return {"added": False, "reason": "LinkedIn's Apply button did not lead to the company's own site."}
    careers.learn(company, real, "linkedin")
    job_id = store.job_id(listing_url)
    if not store.get_job(job_id):
        fit, matched = fit_scorer(scan_run.plain(scan_run.load_config()).get("scoring"))(title)
        store.add_jobs([{"url": listing_url, "role_key": company_role_key(company, title), "title": title, "company": company, "location": location, "source": "linkedin", "posted": today(), "fit": fit, "matched": matched, "description": description[:20000]}])
    store.update_job(job_id, ats_url=real, ats_lookup=careers.LOOKUP_VERSION)
    result = add([job_id])
    site = site_key(scan_run.apply_url(real))
    name = SITES.get(site or "", "") or (urlparse(real).hostname or "the company's site")
    if result["added"]:
        return {"added": True, "site": name, "apply_url": real}
    return {"added": False, "site": name, "apply_url": real, "reason": f"{result['skipped'][0]['reason']}. It is saved on Find jobs, where Apply opens the company's form." if result["skipped"] else "It is saved on Find jobs."}


# "Add best matches" scores at most this many not-yet-scored jobs per click, and looks
# up the company's own form for at most this many jobs from LinkedIn and other job sites.
SCORE_ON_ADD = 200
MATCH_ON_ADD = 400


def best(limit: int = 50, days: int = 14) -> dict:
    """Queues the best AI matches from the last `days` days that the queue can send.
    Jobs the AI has not looked at yet are scored first, the most promising (by title
    match) first, so new jobs don't wait for a separate fit check."""
    s = settings()
    applied = scan_run.applied_check()
    in_queue = store.queue_statuses()
    since = datetime.fromtimestamp(time.time() - days * 86400).strftime("%Y-%m-%d")
    recent = [job for job in store.list_jobs(since) if not job["hidden"] and job["id"] not in in_queue]
    # Jobs from LinkedIn and other job sites become sendable when their company's own
    # form is found; the best title matches are looked up first.
    unmatched = sorted((j for j in recent if needs_lookup(j)), key=lambda j: j.get("fit") or 0, reverse=True)
    matched_now = match_forms(unmatched[:MATCH_ON_ADD])
    candidates = [job for job in recent if not why_not(job, s["sites"], applied)]
    unscored = sorted((j for j in candidates if j.get("deep_fit") is None), key=lambda j: j.get("fit") or 0, reverse=True)[:SCORE_ON_ADD]
    scored_now = 0
    if unscored:
        with ThreadPoolExecutor(scan_run.SCORE_WORKERS) as pool:
            for future in as_completed([pool.submit(scan_run.score_one, store.get_job(j["id"])) for j in unscored]):
                try:
                    future.result()
                    scored_now += 1
                except Exception:
                    pass  # left unscored; tried again next time
        candidates = [store.get_job(j["id"]) for j in candidates]
    picks = [j for j in candidates if j and j.get("recommend") and (j.get("deep_fit") or 0) >= s["min_fit"] and not why_not(j, s["sites"], applied)]
    picks.sort(key=lambda j: (j.get("deep_fit") or 0, j.get("fit") or 0), reverse=True)
    result = add([j["id"] for j in picks[:limit]])
    result["scored_now"] = scored_now
    result["matched_now"] = matched_now
    result["considered"] = len(candidates)
    return result


def remove(job_id: str) -> None:
    with store.connect() as db:
        db.execute("DELETE FROM queue WHERE job_id = ? AND status != 'running'", (job_id,))


def set_entry(job_id: str, status: str, reasons: list[str] | None = None, ai_answers: list[str] | None = None, page_url: str = "") -> None:
    finished = tracker.now() if status not in ("queued", "running") else None
    with store.connect() as db:
        db.execute(
            "UPDATE queue SET status = ?, reasons = ?, ai_answers = ?, page_url = ?, finished_at = ? WHERE job_id = ?",
            (status, json.dumps(reasons or []), json.dumps(ai_answers or []), page_url, finished, job_id),
        )


def next_queued() -> dict | None:
    with store.connect() as db:
        row = db.execute("SELECT job_id FROM queue WHERE status = 'queued' ORDER BY added_at LIMIT 1").fetchone()
    return store.get_job(row["job_id"]) if row else None


# ---- running ----


def status() -> dict:
    with _lock:
        state = {k: _state[k] for k in ("running", "current", "message", "code_request")}
        state["stopping"] = _state["stop"]
        state["skipping"] = _state["skip"]
    return {**state, "submitted_today": submitted_today(), "settings": settings(), "entries": entries(), "search": careers.search_status()}


def start() -> bool:
    with _lock:
        if _state["running"]:
            return False
        _state.update(running=True, stop=False, current=None, message="Starting...")
    with store.connect() as db:  # a job left running by a restart goes back in line
        db.execute("UPDATE queue SET status = 'queued' WHERE status = 'running'")
    threading.Thread(target=_run, daemon=True).start()
    return True


def stop() -> None:
    """Stops the queue now. The job being filled goes back in line (or, if Submit was
    already pressed, under Check email)."""
    with _lock:
        if not _state["running"]:
            return
        _state.update(stop=True, message="Stopping...")
        chrome = _state["chrome"]
    if chrome:
        chrome.kill()


def skip() -> bool:
    """Ends the job being filled now; the queue goes on with the next one."""
    with _lock:
        if not _state["running"] or not _state["current"] or _state["stop"]:
            return False
        _state.update(skip=True, message=f"Skipping {_state['current']['title']}...")
        chrome = _state["chrome"]
    if chrome:
        chrome.kill()
    return True


CHROME_APP = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


class Chrome:
    """A headless Chrome of the queue's own, one per job. Playwright can only be used
    from the queue's thread, so Skip and Stop (which come from the server's other
    threads) end a job by ending its Chrome; the queue's next Playwright call then
    fails and the job is wrapped up."""

    def __init__(self, playwright):
        self.dir = tempfile.mkdtemp(prefix="ja-chrome-")
        exe = CHROME_APP if Path(CHROME_APP).exists() else playwright.chromium.executable_path
        self.proc = subprocess.Popen(
            [exe, "--headless=new", "--remote-debugging-port=0", f"--user-data-dir={self.dir}", "--no-first-run", "--no-default-browser-check", "--window-size=1280,900", "about:blank"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        port_file = Path(self.dir) / "DevToolsActivePort"
        end = time.time() + 30
        while time.time() < end and not (port_file.exists() and port_file.read_text().strip()):
            if self.proc.poll() is not None:
                raise RuntimeError("Chrome did not start")
            time.sleep(0.1)
        port = port_file.read_text().split()[0]
        self.browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")

    def kill(self) -> None:
        try:
            self.proc.kill()
        except OSError:
            pass

    def close(self) -> None:
        try:
            self.browser.close()
        except Exception:
            pass  # already gone after Skip or Stop
        self.kill()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        shutil.rmtree(self.dir, ignore_errors=True)


def interrupted(stopping: bool) -> dict:
    """The outcome of a job you skipped, or that Stop ended."""
    if _state["submitting"]:
        return {"status": "unconfirmed", "reasons": ["You ended it after Submit was pressed, so it may have been sent. Check your email before applying again."]}
    if stopping:
        return {"status": "queued", "reasons": []}  # back in line for the next run
    return {"status": "needs_you", "reasons": [f"You skipped it while it was being filled. {FINISH_YOURSELF}"]}


def warm_ahead(current_id: str) -> None:
    """Starts writing the answers and cover letters for the next jobs in line."""
    with store.connect() as db:
        rows = db.execute("SELECT job_id FROM queue WHERE status = 'queued' AND job_id != ? ORDER BY added_at LIMIT ?", (current_id, WARM_AHEAD)).fetchall()
    for row in rows:
        job = store.get_job(row["job_id"])
        if not job:
            continue
        try:
            httpx.post(f"{SERVER}/plan/warm", json={"url": form_url(job_link(job))}, timeout=10)
        except (httpx.HTTPError, ValueError):
            pass  # only a head start


def form_url(job_url: str) -> str:
    """The page with the application form. Greenhouse forms are opened on Greenhouse's
    own embed page, which always shows the form: company career pages that embed it
    (?gh_jid=...) and boards that redirect to the company's site both hide it in a frame."""
    url = scan_run.apply_url(job_url)
    try:
        site, board, job_id = site_for(url)
    except ValueError:
        return url
    if site is oracle:
        # The application starts at the email step; the posting page only links to it.
        return re.sub(r"(/job/\d+).*$", r"\1/apply/email", url)
    if site is not greenhouse:
        return url
    host = urlparse(url).hostname or ""
    if not host.endswith("greenhouse.io"):
        board = greenhouse.real_board(job_id) or board
    domain = "job-boards.eu.greenhouse.io" if host.endswith("eu.greenhouse.io") else "job-boards.greenhouse.io"
    return f"https://{domain}/embed/job_app?for={board}&token={job_id}"


def provide_code(job_id: str, code: str) -> bool:
    """The security code you typed on the Queue page, for the job waiting on it."""
    with _lock:
        request = _state.get("code_request")
        if not request or request["job_id"] != job_id:
            return False
        _state["code"] = re.sub(r"\s+", "", code)
    return True


def wait_for_code(job: dict, minutes: float, clicked_at, site: str = "greenhouse") -> str | None:
    """Gets the code the site emailed: read from your inbox when an app password is set
    (OTP_EMAIL_APP_PASSWORD in .env), or typed by you on the Queue page, whichever
    comes first. Waits up to `minutes` (or until Stop)."""
    reading = otp.configured()
    with _lock:
        _state.update(code=None, code_request={"job_id": job["id"], "title": job["title"], "company": job["company"], "since": tracker.now(), "minutes": minutes, "reading_email": reading})
    where = "Reading it from your email" if reading else "Type it on the Queue page to send the application"
    say(f"{job['company']} emailed you a security code. {where}...", {"id": job["id"], "title": job["title"], "company": job["company"]})
    end = time.time() + minutes * 60
    next_check = time.time() + 4  # the email takes a few seconds to arrive
    try:
        while time.time() < end and not _state["stop"] and not _state["skip"]:
            with _lock:
                if _state["code"]:
                    return _state["code"]
            if reading and time.time() >= next_check:
                next_check = time.time() + 5
                try:
                    code = otp.find_code(clicked_at, job["company"], site)
                except Exception:
                    code = None  # the inbox is a convenience; typing the code still works
                if code:
                    say(f"Found the security code in your email; entering it for {job['company']}...", {"id": job["id"], "title": job["title"], "company": job["company"]})
                    return code
            time.sleep(1)
        return None
    finally:
        with _lock:
            _state.update(code_request=None, code=None)


def enter_code(page, code: str) -> bool:
    """Types the code into the site's code boxes: one box per character, or one box."""
    boxes = page.locator('input[maxlength="1"]:visible, input[id^="pin-code-"]:visible')
    if boxes.count() >= len(code) >= 4:
        for i, char in enumerate(code):
            boxes.nth(i).fill(char)
        return True
    single = page.locator('input[name*="code" i]:visible, input[id*="code" i]:visible, input[aria-label*="code" i]:visible, input[autocomplete="one-time-code"]:visible')
    if single.count():
        single.first.fill(code)
        return True
    return False


def say(message: str, current: dict | None = None) -> None:
    with _lock:
        _state.update(message=message, current=current)


def _run() -> None:
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            while not _state["stop"]:
                s = settings()
                if s["submit"] and submitted_today() >= s["daily_cap"]:
                    say(f"Stopped: today's limit of {s['daily_cap']} applications is reached. Change it in Settings to send more.")
                    break
                job = next_queued()
                if not job:
                    say("Done. The queue is empty.")
                    break
                set_entry(job["id"], "running")
                warm_ahead(job["id"])
                with _lock:
                    _state.update(skip=False, submitting=False)
                say(f"Filling {job['title']} at {job['company']}...", {"id": job["id"], "title": job["title"], "company": job["company"]})
                outcome = None
                for attempt in range(2):
                    chrome = None
                    try:
                        chrome = Chrome(p)
                        with _lock:
                            _state["chrome"] = chrome
                        if _state["skip"] or _state["stop"]:
                            chrome.kill()  # Skip or Stop came while Chrome was starting
                        outcome = apply_one(chrome.browser, job, s)
                        break
                    except Exception as error:
                        if _state["skip"] or _state["stop"]:
                            break
                        if attempt == 0 and TRANSIENT.search(str(error)):
                            say(f"A network hiccup on {job['company']}; trying again...", {"id": job["id"], "title": job["title"], "company": job["company"]})
                            time.sleep(8)
                            continue
                        outcome = {"status": "failed", "reasons": [f"Something went wrong: {str(error).splitlines()[0][:200]}"]}
                    finally:
                        with _lock:
                            _state["chrome"] = None
                        if chrome:
                            chrome.close()
                if (_state["skip"] or _state["stop"]) and (outcome is None or outcome["status"] != "submitted"):
                    outcome = interrupted(stopping=_state["stop"] and not _state["skip"])
                finish(job, outcome)
                say(f"Last one: {job['title']} at {job['company']}: {LABELS[outcome['status']]}.")
                # A pause between sends, so applications don't go out in a burst.
                pause = random.uniform(s["gap_seconds"], s["gap_seconds"] * 1.5) if outcome["status"] == "submitted" else 3
                end = time.time() + pause
                while time.time() < end and not _state["stop"]:
                    time.sleep(1)
            if _state["stop"]:
                say("Stopped.")
    except Exception as error:
        say(f"The queue stopped: {error}")
    finally:
        with _lock:
            _state.update(running=False, stop=False, current=None)


LABELS = {
    "queued": "put back in line",
    "submitted": "submitted",
    "needs_you": "needs you",
    "ready": "filled in test mode, not sent",
    "unconfirmed": "sent, but no confirmation seen",
    "skipped": "skipped",
    "failed": "failed",
}


def finish(job: dict, outcome: dict) -> None:
    set_entry(job["id"], outcome["status"], outcome.get("reasons"), outcome.get("ai_answers"), outcome.get("page_url", ""))
    if outcome["status"] == "submitted":
        url = form_url(job_link(job))
        given = {"company": job["company"], "role": job["title"], "location": job["location"]}
        log_application({"url": url, "company": job["company"], "title": job["title"], "event": "auto_submitted", "problems": [], "board_url": job["url"]})
        tracker.record_applied(url, given, board_url=job["url"])


def forward(message_json: str, board_url: str) -> str:
    """The page's API calls, sent on to the local server."""
    message = json.loads(message_json)
    path, body = message.get("path"), message.get("body")
    if path == "/log":
        if not body or body.get("event") != "filled":
            # The queue adds the job to the tracker itself, once the site confirms it.
            return json.dumps({"ok": True, "status": 200, "data": {}})
        body = {**body, "board_url": board_url}
    response = httpx.post(SERVER + path, json=body, timeout=240) if body else httpx.get(SERVER + path, timeout=60)
    return json.dumps({"ok": response.is_success, "status": response.status_code, "data": response.json()})


def short(reason: str) -> str:
    """"Label (needs your answer (the AI suggested '...': why))" as "Label (AI suggested '...')", kept short."""
    reason = re.sub(r"\(needs your answer:? *", "(", reason)
    reason = re.sub(r"\(\(?the AI suggested (.+?): [^()]*\)\)?$", r"(AI suggested \1)", reason, flags=re.S)
    return reason if len(reason) <= 200 else reason[:197].rstrip() + "..."


def page_text(page) -> str:
    try:
        return page.evaluate("() => document.body ? document.body.innerText : ''")
    except Exception:  # the page is navigating
        return ""


def frame_text(frame) -> str:
    try:
        return frame.evaluate("() => document.body ? document.body.innerText : ''")
    except Exception:
        return ""


def captchas(page) -> list[str]:
    """Visible captchas in the page and in every frame inside it (iCIMS puts its form,
    and so its hCaptcha challenge, inside a frame)."""
    found = []
    for frame in page.frames:
        try:
            found += frame.evaluate(CAPTCHAS)
        except Exception:
            pass  # a frame that is loading or gone
    return found


def screenshot(page, job_id: str, name: str = "") -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    try:
        page.screenshot(path=str(SHOTS / f"{job_id}{name}.png"), full_page=True)
    except Exception:
        pass


def apply_one(browser, job: dict, s: dict) -> dict:
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    url = form_url(job_link(job))
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    try:
        page.expose_function("__jaApi", lambda message: forward(message, job["url"]))
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        if site_key(url) in HANDOFF_SITES:
            # Only checked to be still open; nothing on the page is pressed.
            page.wait_for_timeout(6000)
            screenshot(page, job["id"])
            if any(CLOSED.search(frame_text(frame)) for frame in page.frames):
                return {"status": "skipped", "reasons": ["The posting is closed."]}
            return {"status": "needs_you", "reasons": [f"{HANDOFF_SITES[site_key(url)]}. {FINISH_YOURSELF} The extension fills the email step and, after you sign in, the form."]}
        try:
            page.locator(FORM_READY).first.wait_for(state="attached", timeout=30000)
        except PlaywrightTimeout:
            screenshot(page, job["id"])
            if CLOSED.search(page_text(page)):
                return {"status": "skipped", "reasons": ["The posting is closed."]}
            return {"status": "needs_you", "reasons": ["The application form did not load."]}
        page.wait_for_timeout(1200)
        started = otp.now()
        report = run_fill(page)
        if site_key(url) == "successfactors" and not report.get("wall") and re.search(r"clicked Apply", report.get("summary") or ""):
            # The posting's Apply button opened the sign-in page.
            page.wait_for_load_state("domcontentloaded", timeout=60000)
            page.locator(SF_PAGES).first.wait_for(state="attached", timeout=40000)
            page.wait_for_timeout(1500)
            report = run_fill(page)
        if report.get("wall") in ("sf-signin", "sf-register"):
            blocked = sf_sign_in(page)
            if blocked:
                screenshot(page, job["id"])
                return blocked
            report = run_fill(page)
        if report.get("wall") in ("oracle-email", "oracle-pin"):
            blocked = oracle_email_step(page, job, s, started)
            if blocked:
                screenshot(page, job["id"])
                return blocked
            report = run_fill(page)
        if report.get("alreadyApplied"):
            return {"status": "skipped", "reasons": ["The site says you have already applied for this job."]}
        if report.get("failed") or "problems" not in report:
            screenshot(page, job["id"])
            return {"status": "failed", "reasons": [report.get("summary") or "The fill did not run."]}

        if site_key(url) == "successfactors" and page.locator("#fbclc_pwd").count():
            # Quick apply: the new account's boxes are part of the application.
            blocked = sf_account_boxes(page)
            if blocked:
                screenshot(page, job["id"])
                return blocked
        ai_answers = report["review"]
        # Jobs limited to U.S. citizens are not applied to (user, 2026-10-05). On forms
        # read from the page, questions answered before (and so left as they were) are
        # checked too.
        citizen_only = [w for w in report["warnings"] + citizenship_warning(page_questions(page)) if re.search(r"citizen", w, re.I)][:1]
        if citizen_only:
            return {"status": "skipped", "reasons": citizen_only}
        reasons = [f"Warning: {w}" for w in report["warnings"]]
        reasons += [f"Please fix: {p}" for p in report["problems"]]
        reasons += [f"Needs your answer: {short(n)}" for n in report["needYou"]]
        if not report["filled"]:
            reasons.append("Nothing could be filled.")
        if site_key(url) == "workday" and not report.get("reachedReview") and not reasons:
            reasons.append("Workday did not reach its Review step.")
        if captchas(page):
            reasons.append("The form shows a captcha, which you need to do yourself.")
        if not reasons:
            # A required box the fill could not see (one the site adds by itself) is
            # caught here, before anything is sent.
            empty = required_empty(page)
            if empty:
                reasons.append(f"Required fields are still empty: {'; '.join(empty)}. {FINISH_YOURSELF}")
        if reasons:
            screenshot(page, job["id"])
            return {"status": "needs_you", "reasons": reasons, "ai_answers": ai_answers}
        if not s["submit"]:
            screenshot(page, job["id"])
            return {"status": "ready", "reasons": ["Everything matched. Not sent, because test mode is on."], "ai_answers": ai_answers}
        return submit(page, job, ai_answers, s)
    finally:
        context.close()


def press(locator) -> None:
    """Clicks a button; when a cookie banner or overlay sits over it, presses it directly."""
    try:
        locator.click(timeout=5000)
    except Exception:
        locator.evaluate("e => e.click()")


ORACLE_NEXT = "button.apply-flow-pagination__button[title=Next], button[title=Next]"
ORACLE_PIN = 'input[id^="pin-code-"], .pin-code-input__input'
ORACLE_FORM = 'input[name="lastName"], input[name="firstName"], [class*="profile-import"]'


def oracle_email_step(page, job: dict, s: dict, started) -> dict | None:
    """Oracle's email step, after the fill put in your email and agreed to the terms:
    presses Next and, when Oracle emails a code to confirm it's you, gets the code
    (from your inbox, or typed on the Queue page) and verifies it. Returns an outcome
    when it can't get through, else None with the application form showing."""
    if page.locator('input[name="primary-email"]:visible').count():
        press(page.locator(ORACLE_NEXT).first)
    # The email box goes a moment before the code boxes or the form show up.
    for _ in range(40):
        page.wait_for_timeout(1000)
        if page.locator(ORACLE_PIN).count() or page.locator(ORACLE_FORM).count():
            break
    if page.locator(ORACLE_PIN).count():
        code = wait_for_code(job, s["code_wait_minutes"], started, "oracle")
        if not code:
            return {"status": "needs_you", "reasons": [f"Oracle emailed you a verification code and none was entered within {s['code_wait_minutes']} minutes. {FINISH_YOURSELF}"]}
        if not enter_code(page, code):
            return {"status": "needs_you", "reasons": [f"Oracle's verification code boxes could not be found. {FINISH_YOURSELF}"]}
        page.wait_for_timeout(500)
        verify = page.get_by_role("button", name=re.compile(r"^\s*verify\s*$", re.I))
        if verify.count():
            press(verify.first)
        for _ in range(40):
            page.wait_for_timeout(1000)
            if not page.locator(ORACLE_PIN).count():
                break
        else:
            return {"status": "needs_you", "reasons": [f"Oracle did not accept the verification code. {FINISH_YOURSELF}"]}
    elif page.locator('input[name="primary-email"]').count():
        return {"status": "needs_you", "reasons": [f"Oracle did not move past its email step. {FINISH_YOURSELF}"]}
    # The application loads its sections a moment after the step changes.
    try:
        page.locator(ORACLE_FORM).first.wait_for(state="attached", timeout=30000)
    except Exception:
        return {"status": "needs_you", "reasons": [f"Oracle's application form did not load. {FINISH_YOURSELF}"]}
    page.wait_for_timeout(3000)
    return None


SF_PAGES = 'input#username, #fbclc_userName, #careerform, #fbqa_apply'
# The application itself (the sign-in and account pages use #careerform too).
SF_FORM = '#fbqa_apply, #careerform .attachmentField, #careerform [id^="fbja_"], #careerform [role=combobox]'
SF_EXISTS = re.compile(r"account already exists|already an account with", re.I)
SF_BAD_LOGIN = re.compile(r"invalid (email address|user ?name) or password|incorrect (email|password)", re.I)
SF_VERIFY = re.compile(r"verify your email|verification (email|link)|activate your account|activation (email|link)", re.I)


def sf_sign_in(page) -> dict | None:
    """SuccessFactors' sign-in: signs in with EMAIL and PASSWORD from .env or, where you
    have no account yet, creates one with them (the user approved this on 2026-10-08).
    They are typed into the page here and never logged or passed to the page's scripts.
    Returns an outcome when it can't get through, else None with the application showing."""
    env = dotenv_values(ROOT / ".env")
    email, password = env.get("EMAIL"), env.get("PASSWORD")
    if not email or not password:
        return {"status": "needs_you", "reasons": [f"SuccessFactors needs an account: add EMAIL and PASSWORD to the .env file. {FINISH_YOURSELF}"]}
    if page.locator("input#username").count():
        page.fill("input#username", email)
        page.fill("input#password", password)
        press(page.get_by_role("button", name=re.compile(r"^\s*sign in\s*$", re.I)).first)
        for _ in range(30):
            page.wait_for_timeout(1000)
            if page.locator(SF_FORM).count() or SF_BAD_LOGIN.search(page_text(page)):
                break
        if page.locator("input#username:visible").count():
            if not SF_BAD_LOGIN.search(page_text(page)):
                return {"status": "needs_you", "reasons": [f"SuccessFactors did not sign you in. {FINISH_YOURSELF}"]}
            # No account with this email yet (or another password): try making one.
            # The first click can be taken up by the email box's "required" check.
            for _ in range(3):
                press(page.get_by_role("link", name=re.compile(r"create an account", re.I)).first)
                try:
                    page.locator("#fbclc_userName").wait_for(timeout=8000)
                    break
                except Exception:
                    continue
            if not page.locator("#fbclc_userName").count():
                return {"status": "needs_you", "reasons": [f"SuccessFactors turned down your sign-in and its new account page did not open. {FINISH_YOURSELF}"]}
    if page.locator("#fbclc_userName").count():
        blocked = sf_create_account(page, email, password)
        if blocked:
            return blocked
    try:
        page.locator(SF_FORM).first.wait_for(state="attached", timeout=40000)
    except Exception:
        if SF_VERIFY.search(page_text(page)):
            return {"status": "needs_you", "reasons": [f"SuccessFactors made you an account and emailed you a link to confirm it. Click the link, then {FINISH_YOURSELF[0].lower()}{FINISH_YOURSELF[1:]}"]}
        return {"status": "needs_you", "reasons": [f"After signing in, the SuccessFactors application did not open. {FINISH_YOURSELF}"]}
    page.wait_for_timeout(2000)
    return None


def sf_account_boxes(page) -> dict | None:
    """SuccessFactors' quick apply asks for a password for the new account on the
    application itself; it is filled from .env here, never by the page's scripts."""
    password = dotenv_values(ROOT / ".env").get("PASSWORD")
    if not password:
        return {"status": "needs_you", "reasons": [f"SuccessFactors needs a password for your new account: add PASSWORD to the .env file. {FINISH_YOURSELF}"]}
    for selector in ("#fbclc_pwd", "#fbclc_pwdConf"):
        box = page.locator(selector)
        if box.count() and box.first.is_visible() and not box.first.input_value():
            box.first.fill(password)
    return sf_accept_notice(page)


def sf_accept_notice(page) -> dict | None:
    """Accepts the privacy notice, which opens in a dialog with its own Accept button."""
    notice = page.locator("#dataPrivacyId")
    accepted = page.locator("#fbclc_dpcsId")
    if not notice.count() or (accepted.count() and accepted.first.input_value()):
        return None
    press(notice.first)
    accept = page.get_by_role("button", name=re.compile(r"^\s*(i )?(accept|agree|acknowledge)", re.I))
    try:
        accept.first.wait_for(state="visible", timeout=10000)
        press(accept.first)
    except Exception:
        return {"status": "needs_you", "reasons": [f"SuccessFactors' privacy notice could not be accepted. {FINISH_YOURSELF}"]}
    page.wait_for_timeout(800)
    return None


def sf_create_account(page, email: str, password: str) -> dict | None:
    """Fills SuccessFactors' Create an Account page from your profile and accepts its
    privacy notice. Returns an outcome when the account could not be made."""
    p = load_profile()
    phone = re.sub(r"\D", "", str(p["contact"].get("phone") or ""))[-10:]

    def fill(selector: str, value: str) -> None:
        if value and page.locator(selector).count():
            page.fill(selector, value)

    def choose(selector: str, pattern: str) -> None:
        box = page.locator(selector)
        if box.count():
            option = next((o for o in box.locator("option").all_inner_texts() if re.search(pattern, o, re.I)), None)
            if option:
                box.select_option(label=option)

    fill("#fbclc_userName", email)
    fill("#fbclc_emailConf", email)
    fill("#fbclc_pwd", password)
    fill("#fbclc_pwdConf", password)
    fill("#fbclc_fName", p["name"]["first"])
    fill("#fbclc_lName", p["name"]["last"])
    choose("#fbclc_ituCode", r"^united states.*\(\+1\)")
    fill("#fbclc_phoneNumber", phone)
    choose("#fbclc_country", r"^united states( of america)?$")
    blocked = sf_accept_notice(page)
    if blocked:
        return blocked
    press(page.locator("#fbclc_createAccountButton").first)
    for _ in range(30):
        page.wait_for_timeout(1000)
        if SF_EXISTS.search(page_text(page)):
            found = re.search(r"already an account with ([^.]+)", page_text(page), re.I)
            return {"status": "needs_you", "reasons": [f"You already have an account on this SuccessFactors site{' (with ' + found.group(1).strip() + ')' if found else ''}, but the EMAIL and PASSWORD in .env did not sign in to it. Sign in there yourself, or reset its password to the one in .env. {FINISH_YOURSELF}"]}
        if not page.locator("#fbclc_userName").count():
            return None
        errors = [e for e in page.evaluate(ERRORS) if e]
        if errors:
            return {"status": "needs_you", "reasons": [f"SuccessFactors would not create your account: {'; '.join(errors)[:300]}. {FINISH_YOURSELF}"]}
    return {"status": "needs_you", "reasons": [f"SuccessFactors did not finish creating your account. {FINISH_YOURSELF}"]}


def run_fill(page) -> dict:
    """Runs the extension's fill and returns its report. Workday may reload the page
    part way (after signing in); the scripts are then loaded again and the fill,
    which carries on by itself after a reload, is waited for."""
    report: dict = {}
    for _ in range(3):
        page.evaluate(STUB)
        page.evaluate(content_scripts())
        try:
            report = page.evaluate(TRIGGER) or {}
        except Exception as error:
            if not re.search(r"Execution context was destroyed|navigat|Target closed", str(error), re.I):
                raise
            report = {"reloading": True}
        if not report.get("reloading"):
            return report
        page.wait_for_load_state("domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)
    return report


def page_questions(page) -> list[str]:
    """The labels of every question the page-reading fill sees, answered or not."""
    try:
        return page.evaluate("() => typeof gnFields === 'function' ? gnFields(gnForm()).map((f) => f.label) : []")
    except Exception:
        return []


def required_empty(page) -> list[str]:
    try:
        return page.evaluate(REQUIRED_EMPTY)
    except Exception:
        return []


def submit_button(page, site: str | None = None):
    patterns = [r"^\s*submit( (my |your )?application)?\s*$", r"submit"]
    if site == "successfactors":
        patterns.append(r"^\s*apply\s*$")  # its form ends in Save and Apply
    for pattern in patterns:
        buttons = page.get_by_role("button", name=re.compile(pattern, re.I))
        for i in range(buttons.count()):
            if buttons.nth(i).is_visible():
                return buttons.nth(i)
    inputs = page.locator("input[type=submit]")
    for i in range(inputs.count()):
        if inputs.nth(i).is_visible():
            return inputs.nth(i)
    return None


FINISH_YOURSELF = "Click Open and fill to finish it in your own browser."


def submit(page, job: dict, ai_answers: list[str], s: dict) -> dict:
    def result(status: str, reasons: list[str]) -> dict:
        screenshot(page, job["id"])
        return {"status": status, "reasons": reasons, "ai_answers": ai_answers, "page_url": page.url}

    site = site_key(job_link(job))
    button = submit_button(page, site)
    if not button:
        return result("needs_you", [f"Everything matched, but the Submit button could not be found. {FINISH_YOURSELF}"])
    screenshot(page, job["id"], "-form")
    before_text, before_url = page_text(page), page.url
    before_done = len(DONE.findall(before_text))
    clicked_at = otp.now()
    with _lock:
        _state["submitting"] = True
    press(button)

    deadline = time.time() + 45
    code_tried = False
    while time.time() < deadline:
        page.wait_for_timeout(1500)
        text = page_text(page)
        if len(DONE.findall(text)) > before_done:
            return result("submitted", [])
        if captchas(page):
            return result("needs_you", [f"A captcha appeared after Submit, so the application was not sent. {FINISH_YOURSELF}"])
        if SPAM.search(text) and not SPAM.search(before_text):
            return result("needs_you", [f"The site turned away the automated submission (it flagged it as possible spam), so nothing was sent. {FINISH_YOURSELF}"])
        if EMAIL_CODE.search(text) and not EMAIL_CODE.search(before_text) and not code_tried:
            # The site emailed you a code. You type it on the Queue page; the queue enters it and sends.
            code_tried = True
            code = wait_for_code(job, s["code_wait_minutes"], clicked_at)
            if not code:
                return result("needs_you", [f"The site emailed you a security code and none was entered within {s['code_wait_minutes']} minutes. {FINISH_YOURSELF}"])
            if not enter_code(page, code):
                return result("needs_you", [f"The security code boxes could not be found. {FINISH_YOURSELF}"])
            page.wait_for_timeout(500)
            again = submit_button(page, site)
            if again:
                try:
                    again.click(timeout=15000)
                except Exception:
                    pass  # some sites send as soon as the last box is filled
            before_text = page_text(page)
            deadline = time.time() + 45
            continue
        if EMAIL_CODE.search(text) and code_tried and re.search(r"(invalid|incorrect|wrong|expired) (security |verification )?code", text, re.I):
            return result("needs_you", [f"The site did not accept that security code. {FINISH_YOURSELF}"])
        if page.url != before_url and not page.locator(FORM_READY).count():
            page.wait_for_timeout(2000)
            if DONE.search(page_text(page)):
                return result("submitted", [])
            return result("unconfirmed", ["The page changed after Submit but showed no thank-you message. Check your email before applying again."])
    errors = page.evaluate(ERRORS) if page.locator(FORM_READY).count() else []
    if errors:
        return result("needs_you", ["The site did not accept the form: " + "; ".join(errors)])
    return result("unconfirmed", ["Submit was clicked but no confirmation appeared. Check your email before applying again."])

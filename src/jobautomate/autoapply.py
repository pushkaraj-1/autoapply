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
import threading
import time
from datetime import datetime

import httpx

from jobautomate import tracker
from jobautomate.prepare import log_application, site_for
from jobautomate.profile import ROOT
from jobautomate.scanner import run as scan_run
from jobautomate.scanner import store

SERVER = "http://127.0.0.1:8765"
SHOTS = ROOT / "data" / "autoapply"
SITES = {"greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby"}
DEFAULTS = {"daily_cap": 15, "submit": True, "min_fit": 70, "sites": list(SITES), "gap_seconds": 45}
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

FORM_READY = '[id="first_name"], [data-field-path], input[name="email"], [data-testid="input-first_name"], input[type="email"], input[type="file"]'
CLOSED = re.compile(r"no longer (accepting|available|open)|job (is )?(not found|closed)|position has been filled|page (you('re| are) looking for )?(was )?not found", re.I)
DONE = re.compile(
    r"thank(s| you)\b.{0,60}\b(applying|application|submitting|interest)|application (has been |was )?(successfully )?(submitted|received|sent)"
    r"|we('ve| have) received your application|successfully (applied|submitted)|application submitted",
    re.I,
)
EMAIL_CODE = re.compile(r"security code|verification code|enter the code|code (was |has been )?sent to", re.I)

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

_state: dict = {"running": False, "stop": False, "current": None, "message": ""}
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


def site_key(url: str) -> str | None:
    try:
        return site_for(url)[0].__name__.rsplit(".", 1)[-1]
    except ValueError:
        return None


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def submitted_today() -> int:
    with store.connect() as db:
        return db.execute("SELECT COUNT(*) FROM queue WHERE status = 'submitted' AND finished_at >= ?", (today(),)).fetchone()[0]


def entries() -> list[dict]:
    with store.connect() as db:
        rows = db.execute(
            "SELECT q.*, j.url, j.title, j.company, j.location, j.fit, j.deep_fit, j.legacy_applied FROM queue q JOIN jobs j ON j.id = q.job_id ORDER BY q.added_at"
        ).fetchall()
    applied = scan_run.applied_check()
    result = []
    for row in rows:
        entry = dict(row)
        entry["reasons"] = json.loads(entry["reasons"] or "[]")
        entry["ai_answers"] = json.loads(entry["ai_answers"] or "[]")
        entry["apply_url"] = scan_run.apply_url(entry["url"])
        entry["applied"] = applied(entry)
        entry["site"] = SITES.get(site_key(entry["apply_url"]) or "", "")
        entry["has_screenshot"] = (SHOTS / f"{entry['job_id']}.png").exists()
        result.append(entry)
    return result


def why_not(job: dict, allowed: list[str], applied) -> str:
    """Why a job can't go in the queue, or "" if it can."""
    site = site_key(scan_run.apply_url(job["url"]))
    if applied(job):
        return "already applied"
    if site not in allowed:
        return "only Greenhouse, Lever and Ashby forms are sent by themselves; use Apply for this one"
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


def best(limit: int = 20, days: int = 14) -> dict:
    """Queues the best AI matches from the last `days` days that the queue can send."""
    s = settings()
    applied = scan_run.applied_check()
    in_queue = store.queue_statuses()
    since = datetime.fromtimestamp(time.time() - days * 86400).strftime("%Y-%m-%d")
    picks = [
        job
        for job in store.list_jobs(since)
        if not job["hidden"]
        and job["id"] not in in_queue
        and job.get("recommend")
        and (job.get("deep_fit") or 0) >= s["min_fit"]
        and not why_not(job, s["sites"], applied)
    ]
    picks.sort(key=lambda j: (j.get("deep_fit") or 0, j.get("fit") or 0), reverse=True)
    return add([j["id"] for j in picks[:limit]])


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
        state = {k: _state[k] for k in ("running", "current", "message")}
        state["stopping"] = _state["stop"]
    return {**state, "submitted_today": submitted_today(), "settings": settings(), "entries": entries()}


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
    with _lock:
        if _state["running"]:
            _state.update(stop=True, message="Stopping after this job...")


def say(message: str, current: dict | None = None) -> None:
    with _lock:
        _state.update(message=message, current=current)


def _run() -> None:
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="chrome", headless=True)
            try:
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
                    say(f"Filling {job['title']} at {job['company']}...", {"id": job["id"], "title": job["title"], "company": job["company"]})
                    try:
                        outcome = apply_one(browser, job, s)
                    except Exception as error:
                        outcome = {"status": "failed", "reasons": [f"Something went wrong: {error}"]}
                    finish(job, outcome)
                    say(f"Last one: {job['title']} at {job['company']}: {LABELS[outcome['status']]}.")
                    # A pause between sends, so applications don't go out in a burst.
                    pause = random.uniform(s["gap_seconds"], s["gap_seconds"] * 1.5) if outcome["status"] == "submitted" else 3
                    end = time.time() + pause
                    while time.time() < end and not _state["stop"]:
                        time.sleep(1)
                if _state["stop"]:
                    say("Stopped.")
            finally:
                browser.close()
    except Exception as error:
        say(f"The queue stopped: {error}")
    finally:
        with _lock:
            _state.update(running=False, stop=False, current=None)


LABELS = {
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
        url = scan_run.apply_url(job["url"])
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


def captchas(page) -> list[str]:
    try:
        return page.evaluate(CAPTCHAS)
    except Exception:
        return []


def screenshot(page, job_id: str, name: str = "") -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    try:
        page.screenshot(path=str(SHOTS / f"{job_id}{name}.png"), full_page=True)
    except Exception:
        pass


def apply_one(browser, job: dict, s: dict) -> dict:
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    url = scan_run.apply_url(job["url"])
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    try:
        page.expose_function("__jaApi", lambda message: forward(message, job["url"]))
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            page.locator(FORM_READY).first.wait_for(state="attached", timeout=30000)
        except PlaywrightTimeout:
            screenshot(page, job["id"])
            if CLOSED.search(page_text(page)):
                return {"status": "skipped", "reasons": ["The posting is closed."]}
            return {"status": "needs_you", "reasons": ["The application form did not load."]}
        page.wait_for_timeout(3000)
        page.evaluate(STUB)
        page.evaluate(content_scripts())
        report = page.evaluate(TRIGGER) or {}
        if report.get("failed") or "problems" not in report:
            screenshot(page, job["id"])
            return {"status": "failed", "reasons": [report.get("summary") or "The fill did not run."]}

        ai_answers = report["review"]
        # Jobs limited to U.S. citizens are not applied to (user, 2026-10-05).
        citizen_only = [w for w in report["warnings"] if re.search(r"citizen", w, re.I)]
        if citizen_only:
            return {"status": "skipped", "reasons": citizen_only}
        reasons = [f"Warning: {w}" for w in report["warnings"]]
        reasons += [f"Please fix: {p}" for p in report["problems"]]
        reasons += [f"Needs your answer: {short(n)}" for n in report["needYou"]]
        if not report["filled"]:
            reasons.append("Nothing could be filled.")
        if captchas(page):
            reasons.append("The form shows a captcha, which you need to do yourself.")
        if reasons:
            screenshot(page, job["id"])
            return {"status": "needs_you", "reasons": reasons, "ai_answers": ai_answers}
        if not s["submit"]:
            screenshot(page, job["id"])
            return {"status": "ready", "reasons": ["Everything matched. Not sent, because test mode is on."], "ai_answers": ai_answers}
        return submit(page, job, ai_answers)
    finally:
        context.close()


def submit_button(page):
    for pattern in (r"^\s*submit( (my |your )?application)?\s*$", r"submit"):
        buttons = page.get_by_role("button", name=re.compile(pattern, re.I))
        for i in range(buttons.count()):
            if buttons.nth(i).is_visible():
                return buttons.nth(i)
    inputs = page.locator("input[type=submit]")
    for i in range(inputs.count()):
        if inputs.nth(i).is_visible():
            return inputs.nth(i)
    return None


def submit(page, job: dict, ai_answers: list[str]) -> dict:
    def result(status: str, reasons: list[str]) -> dict:
        screenshot(page, job["id"])
        return {"status": status, "reasons": reasons, "ai_answers": ai_answers, "page_url": page.url}

    button = submit_button(page)
    if not button:
        return result("needs_you", ["Everything matched, but the Submit button could not be found."])
    screenshot(page, job["id"], "-form")
    before_text, before_url = page_text(page), page.url
    before_done = len(DONE.findall(before_text))
    button.click(timeout=15000)

    deadline = time.time() + 45
    while time.time() < deadline:
        page.wait_for_timeout(1500)
        text = page_text(page)
        if len(DONE.findall(text)) > before_done:
            return result("submitted", [])
        if captchas(page):
            return result("needs_you", ["A captcha appeared after Submit, so the application was not sent. Please apply in your own browser."])
        if EMAIL_CODE.search(text) and not EMAIL_CODE.search(before_text):
            return result("needs_you", ["The site emailed you a security code before it accepts the application. Please apply in your own browser."])
        if page.url != before_url and not page.locator(FORM_READY).count():
            page.wait_for_timeout(2000)
            if DONE.search(page_text(page)):
                return result("submitted", [])
            return result("unconfirmed", ["The page changed after Submit but showed no thank-you message. Check your email before applying again."])
    errors = page.evaluate(ERRORS) if page.locator(FORM_READY).count() else []
    if errors:
        return result("needs_you", ["The site did not accept the form: " + "; ".join(errors)])
    return result("unconfirmed", ["Submit was clicked but no confirmation appeared. Check your email before applying again."])

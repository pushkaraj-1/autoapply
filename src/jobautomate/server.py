"""Local server the Chrome extension talks to. Run with: uv run jobautomate-server"""

import base64
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import uvicorn
from dotenv import dotenv_values
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from jobautomate import autoapply, greenhouse, tracker
from jobautomate.answers import closest_option
from jobautomate.scanner import run as scan_run
from jobautomate.scanner import store as scan_store
from jobautomate.prepare import answer_fields, log_application, prepare, site_for, site_or_generic
from jobautomate.profile import ROOT, load_profile
from jobautomate.profile import resume_path

PORT = 8765
app = FastAPI()


class PlanRequest(BaseModel):
    url: str


class FieldsRequest(BaseModel):
    url: str
    fields: list[dict]
    job: dict | None = None  # company, title, description read from the page


class ChooseRequest(BaseModel):
    label: str
    wanted: str
    options: list[str]


class LogRequest(BaseModel):
    url: str
    company: str
    title: str
    event: str  # "filled" or "submit_clicked"
    problems: list[str] = []
    board_url: str = ""  # added by the extension when the tab was opened from Find jobs


def encode(path: Path) -> dict:
    return {"name": path.name, "base64": base64.b64encode(path.read_bytes()).decode()}


def upload_files() -> dict:
    """The files a form may ask for: the resume, and the transcript when profile.yaml
    names one (transcript_path)."""
    files = {"resume": encode(resume_path())}
    transcript = load_profile().get("transcript_path")
    if transcript and Path(transcript).expanduser().exists():
        files["transcript"] = encode(Path(transcript).expanduser())
    return files


@app.get("/health")
def health() -> dict:
    return {"ok": True}


# Answers prepared ahead of the Fill click (a tab opened from Find jobs, or the popup
# opened on a job page), by job, with when they were started.
_warming: dict[str, tuple[float, Future]] = {}
_warm_pool = ThreadPoolExecutor(3)
WARM_MINUTES = 30


def warm_key(url: str) -> str | None:
    try:
        site, board, job_id = site_for(url)
    except ValueError:
        return None
    return f"{site.__name__}-{board}-{job_id}"


@app.post("/plan/warm")
def plan_warm(request: PlanRequest) -> dict:
    """Starts preparing the answers and cover letter now, so Fill finds them ready."""
    key = warm_key(request.url)
    if not key:
        return {"warming": False}
    started = _warming.get(key)
    if not started or time.time() - started[0] > WARM_MINUTES * 60:
        _warming[key] = (time.time(), _warm_pool.submit(prepare, request.url))
    return {"warming": True}


def prepared_for(url: str):
    """The head-start result when there is a recent one (waiting for it if it is still
    running), else a fresh preparation."""
    started = _warming.pop(warm_key(url) or "", None)
    if started and time.time() - started[0] <= WARM_MINUTES * 60:
        try:
            return started[1].result()
        except Exception:
            pass  # prepared again below, so a real error is shown
    return prepare(url)


@app.post("/plan")
def plan(request: PlanRequest) -> dict:
    try:
        site_for(request.url)
    except ValueError as error:
        raise HTTPException(400, str(error))
    job = prepared_for(request.url)
    files = upload_files()
    if job.letter_pdf:
        files["cover_letter"] = encode(job.letter_pdf)
    return {
        "company": job.company,
        "title": job.title,
        "run_dir": str(job.run_dir),
        "fields": greenhouse.plan_as_dicts(job.fields),
        "files": files,
        "letter_text": job.letter_text,
        "warnings": job.warnings,
        "agree_to_terms": bool(load_profile()["standing_answers"].get("agree_to_certifications")),
    }


@app.post("/answer")
def answer(request: FieldsRequest) -> dict:
    """Answers fields the extension read from the page (Workday), plus the profile for repeating sections."""
    try:
        site_or_generic(request.url)
    except ValueError as error:
        raise HTTPException(400, str(error))
    job = answer_fields(request.url, request.fields, request.job)
    files = upload_files()
    if job.letter_pdf:
        files["cover_letter"] = encode(job.letter_pdf)
    p = load_profile()
    return {
        "company": job.company,
        "title": job.title,
        "fields": greenhouse.plan_as_dicts(job.fields),
        "files": files,
        "warnings": job.warnings,
        "profile": {k: p[k] for k in ("name", "contact", "links", "experience", "education", "languages", "skills", "standing_answers", "demographics")},
    }


@app.get("/profile/email")
def profile_email() -> dict:
    """The application email, for the email step of iCIMS and Oracle."""
    return {"email": load_profile()["contact"]["email"]}


@app.post("/login")
def login(request: PlanRequest, origin: str | None = Header(default=None)) -> dict:
    """Workday sign-in details from .env. Read on every call so edits apply without a restart.

    Only the extension (which sends a chrome-extension:// origin) may ask; a web page
    calling localhost would send its own origin and is refused.
    """
    if origin and not origin.startswith("chrome-extension://"):
        raise HTTPException(403, "Only the extension may ask for sign-in details.")
    parsed = urlparse(request.url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not (host.endswith(".myworkdayjobs.com") or host.endswith(".myworkday.com")):
        raise HTTPException(400, "Sign-in details are only given out for Workday pages.")
    env = dotenv_values(ROOT / ".env")
    email = env.get("WORKDAY_EMAIL") or env.get("EMAIL")
    password = env.get("WORKDAY_PASSWORD") or env.get("PASSWORD")
    if not email or not password:
        raise HTTPException(404, "Add WORKDAY_EMAIL and WORKDAY_PASSWORD to the .env file and save it.")
    return {"email": email, "password": password}


@app.post("/choose")
def choose(request: ChooseRequest) -> dict:
    """The page's option that means the same as an answer the page doesn't list as written."""
    return {"option": closest_option(request.label, request.wanted, request.options[:200])}


@app.post("/log")
def log(request: LogRequest) -> dict:
    log_application(request.model_dump())
    if request.event == "submit_clicked":
        tracker.record_applied(request.url, guess={"company": request.company, "role": request.title}, board_url=request.board_url)
    return {"ok": True}


# ---- tracker, for the dashboard ----


class ApplicationRequest(BaseModel):
    url: str = ""
    company: str = ""
    role: str = ""
    location: str = ""
    page_title: str = ""  # from the popup's Mark as applied; only a fallback for the role
    page_text: str = ""  # the page's text, kept as the description if the job board can't be read
    description: str = ""  # pasted on the dashboard
    board_url: str = ""
    applied_at: str | None = None


class DescriptionRequest(BaseModel):
    text: str


@app.get("/applications")
def applications() -> dict:
    return {"applications": tracker.load()}


@app.post("/applications")
def add_application(request: ApplicationRequest) -> dict:
    """Mark as applied (from the popup) or a job added by hand on the dashboard."""
    if not request.url and not (request.role or request.company):
        raise HTTPException(400, "Give the job's link, or at least its role or company.")
    given = {"company": request.company, "role": request.role, "location": request.location, "description": request.description}
    return tracker.record_applied(request.url, given, {"role": request.page_title}, request.applied_at, request.page_text, request.board_url)


@app.patch("/applications/{application_id}")
def change_application(application_id: str, changes: dict) -> dict:
    try:
        return tracker.update(application_id, changes)
    except KeyError:
        raise HTTPException(404, "No such application.")
    except ValueError as error:
        raise HTTPException(400, str(error))


@app.delete("/applications/{application_id}")
def remove_application(application_id: str) -> dict:
    try:
        tracker.delete(application_id)
    except KeyError:
        raise HTTPException(404, "No such application.")
    return {"ok": True}


@app.get("/applications/{application_id}/description")
def application_description(application_id: str) -> dict:
    return {"text": tracker.read_description(application_id)}


@app.put("/applications/{application_id}/description")
def change_description(application_id: str, request: DescriptionRequest) -> dict:
    try:
        return tracker.set_description(application_id, request.text)
    except KeyError:
        raise HTTPException(404, "No such application.")


@app.get("/applications/{application_id}/letter")
def application_letter(application_id: str) -> FileResponse:
    path = tracker.letter_path(application_id)
    if not path:
        raise HTTPException(404, "No cover letter for this application.")
    return FileResponse(path, media_type="application/pdf")


# ---- Find jobs (the job scanner) ----


class ScanRequest(BaseModel):
    only: list[str] | None = None  # source names; all enabled sources when empty


class AppliedRequest(BaseModel):
    on: bool


def scanned_job(job_id: str) -> dict:
    job = scan_store.get_job(job_id)
    if not job:
        raise HTTPException(404, "No such job.")
    return job


@app.get("/jobs")
def jobs(days: int | None = 10) -> dict:
    """Scanned jobs posted in the last `days` days (never more than 10)."""
    return scan_run.board(days or None)


@app.get("/jobs/progress")
def jobs_progress() -> dict:
    return {**scan_run.progress(), "counts": scan_store.counts()}


@app.post("/jobs/scan")
def jobs_scan(request: ScanRequest) -> dict:
    return {"started": scan_run.start_scan(request.only), "progress": scan_run.progress()}


@app.get("/jobs/settings")
def jobs_settings() -> dict:
    return scan_run.settings()


@app.put("/jobs/settings")
def change_jobs_settings(changes: dict) -> dict:
    return scan_run.save_config(changes)


@app.get("/jobs/{job_id}")
def job_detail(job_id: str) -> dict:
    return scanned_job(job_id)


@app.patch("/jobs/{job_id}")
def change_job(job_id: str, changes: dict) -> dict:
    """hidden: true or false; opened: true when you click Apply."""
    scanned_job(job_id)
    fields = {}
    if "hidden" in changes:
        fields["hidden"] = int(bool(changes["hidden"]))
    if changes.get("opened"):
        fields["opened_at"] = tracker.now()
    if fields:
        scan_store.update_job(job_id, **fields)
    return scanned_job(job_id)


@app.post("/jobs/{job_id}/applied")
def job_applied(job_id: str, request: AppliedRequest) -> dict:
    """The board's Applied box: adds the job to the tracker, or takes it out again."""
    job = scanned_job(job_id)
    if request.on:
        tracker.record_applied(job["url"], {"company": job["company"], "role": job["title"], "location": job["location"]}, board_url=job["url"])
    else:
        key = tracker.job_key(job["url"])
        for application in tracker.load():
            if application.get("board_url") == job["url"] or application["key"] == key:
                tracker.delete(application["id"])
        scan_store.update_job(job_id, legacy_applied=None)
    return {"ok": True}


# ---- auto apply queue ----


class QueueRequest(BaseModel):
    ids: list[str] = []


class BestRequest(BaseModel):
    limit: int = 50


class CodeRequest(BaseModel):
    job_id: str
    code: str


@app.get("/queue")
def queue() -> dict:
    return autoapply.status()


@app.post("/queue")
def queue_add(request: QueueRequest) -> dict:
    return autoapply.add(request.ids)


class LinkRequest(BaseModel):
    listing_url: str
    apply_url: str
    company: str
    title: str
    location: str = ""
    description: str = ""


@app.post("/queue/link")
def queue_link(request: LinkRequest, origin: str | None = Header(default=None)) -> dict:
    """A LinkedIn job and where its Apply button led, from the extension's Queue this button."""
    if origin and not origin.startswith("chrome-extension://"):
        raise HTTPException(403, "Only the extension may add jobs this way.")
    if not request.apply_url.startswith(("https://", "http://")) or not request.title or not request.company:
        raise HTTPException(400, "The job's title, company and link are needed.")
    return autoapply.add_link(**request.model_dump())


@app.post("/queue/best")
def queue_best(request: BestRequest) -> dict:
    """Adds the best title matches the queue can send by itself."""
    return autoapply.best(max(1, min(request.limit, 100)))


@app.post("/queue/code")
def queue_code(request: CodeRequest) -> dict:
    """The security code a site emailed you, typed on the Queue page."""
    if not autoapply.provide_code(request.job_id, request.code):
        raise HTTPException(409, "The queue is not waiting for a code for this job anymore.")
    return {"ok": True}


@app.post("/queue/start")
def queue_start() -> dict:
    return {"started": autoapply.start(), **autoapply.status()}


@app.post("/queue/stop")
def queue_stop() -> dict:
    autoapply.stop()
    return autoapply.status()


@app.post("/queue/skip")
def queue_skip() -> dict:
    """Ends the job being filled now; the queue goes on with the next one."""
    return {"skipped": autoapply.skip(), **autoapply.status()}


@app.put("/queue/settings")
def queue_settings(changes: dict) -> dict:
    return autoapply.save_settings(changes)


@app.delete("/queue/{job_id}")
def queue_remove(job_id: str) -> dict:
    autoapply.remove(job_id)
    return {"ok": True}


@app.get("/queue/{job_id}/screenshot")
def queue_screenshot(job_id: str, which: str = "") -> FileResponse:
    """The page after the queue finished with the job ("form": the filled form before Submit)."""
    if not re.fullmatch(r"[0-9a-f]{6,40}", job_id):
        raise HTTPException(404, "No such job.")
    path = autoapply.SHOTS / f"{job_id}{'-form' if which == 'form' else ''}.png"
    if not path.exists():
        raise HTTPException(404, "No screenshot for this job.")
    return FileResponse(path, media_type="image/png")


def main() -> None:
    threading.Thread(target=scan_run.auto_scan_loop, daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=PORT)

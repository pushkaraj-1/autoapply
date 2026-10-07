"""Applications you have sent, for the dashboard. Stored in data/tracker.json.

Each application keeps its current status and a history of status changes, so
the dashboard can count everything that reached an interview even if it later
ended in a rejection. A copy of each job description is kept in
data/descriptions/, because postings are often taken down before the interview.
"""

import html
import json
import os
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from jobautomate import generic
from jobautomate.prepare import RUNS, fetch, letter_pdf_name, site_or_generic
from jobautomate.profile import ROOT

PATH = ROOT / "data" / "tracker.json"
DESCRIPTIONS = ROOT / "data" / "descriptions"
STATUSES = ("applied", "heard_back", "interview", "offer", "rejected")
# Changes read the whole file and write it back, so the extension's calls and the
# auto apply queue take turns.
_lock = threading.RLock()


def locked(function):
    def run(*args, **kwargs):
        with _lock:
            return function(*args, **kwargs)

    run.__doc__ = function.__doc__
    return run


def load() -> list[dict]:
    if not PATH.exists():
        return []
    return json.loads(PATH.read_text())["applications"]


def save(applications: list[dict]) -> None:
    temp = PATH.with_suffix(".tmp")
    temp.write_text(json.dumps({"applications": applications}, indent=2))
    os.replace(temp, PATH)


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def job_key(url: str) -> str:
    """Same key for every page of one job (overview, form, Workday steps)."""
    try:
        site, board, job_id = site_or_generic(url)
        return f"{site.__name__.split('.')[-1]}-{board}-{job_id}"
    except ValueError:
        return url


def site_name(url: str) -> str:
    try:
        site = site_or_generic(url)[0]
    except ValueError:
        return "Other"
    if site is not generic:
        name = site.__name__.split(".")[-1]
        return {"smartrecruiters": "SmartRecruiters"}.get(name, name.capitalize())
    host = urlparse(url).hostname or ""
    for name in ("gusto", "wellfound", "icims", "successfactors", "sapsf"):
        if name in host:
            return {"icims": "iCIMS", "successfactors": "SuccessFactors", "sapsf": "SuccessFactors"}.get(name, name.capitalize())
    return host.removeprefix("www.")


def run_dir(url: str) -> Path | None:
    try:
        _, board, job_id = site_or_generic(url)
    except ValueError:
        return None
    existing = sorted(RUNS.glob(f"*-{board}-{job_id}"))
    return existing[-1] if existing else None


def job_details(url: str) -> dict:
    """Company, role and location from the saved job, or from the job board."""
    try:
        site, board, job_id = site_or_generic(url)
    except ValueError:
        return {}
    folder = run_dir(url)
    job = None
    if folder and (folder / "job.json").exists():
        job = json.loads((folder / "job.json").read_text())
    elif site is not generic:
        try:
            job = fetch(site, board, job_id, url)
        except Exception:
            job = None
    if not job:
        return {}
    company, title, location, description = site.summary(job)
    return {"company": company, "role": title, "location": location, "description": readable(description or "")}


def readable(content: str) -> str:
    """Description HTML (or page text) as plain text with paragraph breaks kept."""
    text = html.unescape(content)
    text = re.sub(r"<(br|/p|/div|/h\d|/ul|/ol)[^>]*>", "\n", text)
    text = re.sub(r"<li[^>]*>", "\n- ", text)
    text = re.sub(r"<h\d[^>]*>", "\n\n", text)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"(?<=\S)\n{2,}(?=- )", "\n", text)  # bullets of one list stay together
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def description_path(application: dict) -> Path:
    name = re.sub(r"[^A-Za-z0-9]+", "-", f"{application['company']} {application['role']}").strip("-")[:70]
    return DESCRIPTIONS / f"{application['applied_at'][:10]}-{name}-{application['id']}.txt"


def find_description(application_id: str) -> Path | None:
    found = sorted(DESCRIPTIONS.glob(f"*-{application_id}.txt"))
    return found[-1] if found else None


def save_description(application: dict, text: str) -> None:
    """Writes the description with a short header, so the file reads well on its own."""
    DESCRIPTIONS.mkdir(parents=True, exist_ok=True)
    old = find_description(application["id"])
    if old:
        old.unlink()
    header = "\n".join(
        line
        for line in (
            " at ".join(part for part in (application["role"], application["company"]) if part),
            application.get("location", ""),
            application.get("url", ""),
            f"Applied {application['applied_at'][:10]}",
        )
        if line
    )
    description_path(application).write_text(f"{header}\n\n---\n\n{text.strip()}\n")


def read_description(application_id: str) -> str:
    path = find_description(application_id)
    if not path:
        return ""
    return path.read_text().split("\n---\n", 1)[-1].strip()


def board_details(board_url: str) -> dict:
    """What the job scanner saved for the job you opened from the Find jobs page."""
    from jobautomate.scanner import store  # the scanner also reads the tracker

    job = store.find_by_url(board_url) if board_url else None
    if not job:
        return {}
    return {"company": job["company"], "role": job["title"], "location": job["location"], "description": readable(job.get("description") or "")}


@locked
def record_applied(
    url: str, given: dict | None = None, guess: dict | None = None, applied_at: str | None = None, page_text: str = "", board_url: str = ""
) -> dict:
    """Adds the job as applied, or returns it with already=True if it is already tracked.

    given: company, role, location (and description) you typed; these win.
    guess: what a page showed (its title), used only when nothing better says.
    page_text: the page's text, kept as the description when nothing better exists.
    board_url: the Find jobs listing this application was opened from.
    """
    applications = load()
    key = job_key(url) if url else f"manual-{uuid.uuid4().hex[:10]}"
    for application in applications:
        if application["key"] == key or (board_url and application.get("board_url") == board_url):
            return {**application, "already": True}
    found = job_details(url) if url else {}
    details = {k: v for source in (guess or {}, board_details(board_url), found, given or {}) for k, v in source.items() if v}
    folder = run_dir(url) if url else None
    when = applied_at or now()
    application = {
        "id": uuid.uuid4().hex[:10],
        "key": key,
        "company": details.get("company", ""),
        "role": details.get("role", ""),
        "location": details.get("location", ""),
        "site": site_name(url) if url else "Other",
        "url": url,
        "board_url": board_url,
        "applied_at": when,
        "status": "applied",
        "history": [{"status": "applied", "at": when}],
        "notes": "",
        "has_letter": bool(folder and (folder / letter_pdf_name()).exists()),
        "run_dir": folder.name if folder else "",
    }
    description = details.get("description") or readable(page_text)
    if description:
        save_description(application, description)
    application["has_description"] = bool(description)
    applications.append(application)
    save(applications)
    return application


@locked
def set_description(application_id: str, text: str) -> dict:
    applications = load()
    for application in applications:
        if application["id"] == application_id:
            if text.strip():
                save_description(application, text)
            elif path := find_description(application_id):
                path.unlink()
            application["has_description"] = bool(text.strip())
            save(applications)
            return application
    raise KeyError(application_id)


@locked
def update(application_id: str, changes: dict) -> dict:
    applications = load()
    for application in applications:
        if application["id"] != application_id:
            continue
        status = changes.get("status")
        if status and status != application["status"]:
            if status not in STATUSES:
                raise ValueError(f"Unknown status: {status}")
            application["status"] = status
            application["history"].append({"status": status, "at": now()})
        for field in ("company", "role", "location", "notes", "applied_at", "url"):
            if field in changes:
                application[field] = changes[field]
        save(applications)
        return application
    raise KeyError(application_id)


@locked
def delete(application_id: str) -> None:
    applications = load()
    remaining = [a for a in applications if a["id"] != application_id]
    if len(remaining) == len(applications):
        raise KeyError(application_id)
    save(remaining)
    if path := find_description(application_id):
        path.unlink()


def letter_path(application_id: str) -> Path | None:
    for application in load():
        if application["id"] == application_id and application.get("run_dir"):
            path = RUNS / application["run_dir"] / letter_pdf_name()
            return path if path.exists() else None
    return None

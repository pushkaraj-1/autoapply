"""Runs a scan (port of scan-sources.mjs) and the AI fit check (port of deep-score.mjs)
in a background thread, keeping a progress record the Find jobs page polls."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from urllib.parse import urlparse

from ruamel.yaml import YAML

from jobautomate import tracker
from jobautomate.profile import ROOT
from jobautomate.scanner import deep, jd, store
from jobautomate.scanner.filters import company_role_key, fit_scorer, location_filter, title_filter, within_days
from jobautomate.scanner.sources import FETCHERS, NoCredentials, describe

CONFIG_PATH = ROOT / "profile" / "scanner.yaml"
STATE_PATH = ROOT / "data" / "scan_state.json"
SOURCE_WORKERS = 6  # sources are different websites, so they can run side by side
SCORE_WORKERS = 10  # the AI check waits on the network, so more at once is faster

_yaml = YAML()
_yaml.preserve_quotes = True
_progress: dict = {"running": False}
_lock = threading.Lock()


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(CONFIG_PATH.with_name("scanner.example.yaml").read_text())
    return _yaml.load(CONFIG_PATH.read_text())


def save_config(changes: dict) -> dict:
    """Applies settings from the page, keeping the file's comments."""
    config = load_config()
    for key in ("search_terms", "max_age_days", "auto_scan_hours"):
        if key in changes:
            config[key] = changes[key]
    for key in ("positive", "negative"):
        if key in changes.get("title_filter", {}):
            config["title_filter"][key] = changes["title_filter"][key]
    for name, on in changes.get("sources", {}).items():
        if name in config["sources"]:
            config["sources"][name]["enabled"] = bool(on)
    for key, value in changes.get("deep_score", {}).items():
        config["deep_score"][key] = value
    write_config(config)
    return settings()


def write_config(config: dict) -> None:
    with CONFIG_PATH.open("w") as file:
        _yaml.dump(config, file)


def settings() -> dict:
    config = load_config()
    return {
        "search_terms": list(config.get("search_terms") or []),
        "max_age_days": config.get("max_age_days", 3),
        "auto_scan_hours": config.get("auto_scan_hours", 0),
        "title_filter": {k: list(config["title_filter"].get(k) or []) for k in ("positive", "negative")},
        "sources": {name: bool(cfg.get("enabled")) for name, cfg in config["sources"].items()},
        "deep_score": dict(config.get("deep_score") or {}),
    }


def unique_terms(terms: list[str]) -> list[str]:
    """Search terms without repeats; the same words in another order count as a repeat
    ("new grad software engineer" and "software engineer new grad" find the same jobs)."""
    seen, out = set(), []
    for term in terms:
        key = " ".join(sorted(str(term).lower().split()))
        if key and key not in seen:
            seen.add(key)
            out.append(str(term).strip())
    return out


def plain(value):
    """ruamel's commented maps and lists as ordinary dicts and lists."""
    return json.loads(json.dumps(value, default=str))


def progress() -> dict:
    with _lock:
        state = dict(_progress)
    state["last_scan"] = last_scan()
    return state


def last_scan() -> str | None:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text()).get("last_scan")
    return None


def set_progress(**fields) -> None:
    with _lock:
        _progress.update(fields)


def start_scan(only: list[str] | None = None) -> bool:
    """Starts a scan unless one is already running."""
    with _lock:
        if _progress.get("running"):
            return False
        _progress.clear()
        _progress.update(running=True, phase="scan", started_at=datetime.now().isoformat(timespec="seconds"), sources=[], notices=[], added=0, stale=0, scored=0, to_score=0, new_ids=[])
    threading.Thread(target=_scan, args=(only,), daemon=True).start()
    return True


def start_scoring(ids: list[str]) -> bool:
    with _lock:
        if _progress.get("running"):
            return False
        _progress.clear()
        _progress.update(running=True, phase="score", started_at=datetime.now().isoformat(timespec="seconds"), sources=[], notices=[], added=0, stale=0, scored=0, to_score=0, new_ids=[])
    threading.Thread(target=_score, args=(store.unscored(0, len(ids), ids),), daemon=True).start()
    return True


def _scan(only: list[str] | None) -> None:
    try:
        config = plain(load_config())
        terms = unique_terms(config.get("search_terms") or [])
        max_age = config.get("max_age_days", 3)
        keep_title = title_filter(config.get("title_filter"))
        keep_location = location_filter(config.get("location_filter"))
        score_title = fit_scorer(config.get("scoring"))
        sources = config.get("sources") or {}
        enabled = [name for name in FETCHERS if (name in only if only else (sources.get(name) or {}).get("enabled"))]
        seen_urls, seen_roles = store.seen_sets()
        status = {name: {"key": name, "state": "running", "found": 0, "added": 0} for name in enabled}
        started = time.time()
        set_progress(sources=list(status.values()))
        new_offers: list[dict] = []
        notices: list[str] = []
        stale = 0

        def run(name: str) -> list[dict]:
            return FETCHERS[name](sources.get(name) or {}, terms)

        with ThreadPoolExecutor(SOURCE_WORKERS) as pool:
            futures = {pool.submit(run, name): name for name in enabled}
            for future in as_completed(futures):
                name = futures[future]
                try:
                    raw = future.result()
                except NoCredentials as error:
                    notices.append(f"{name}: skipped, {error}")
                    status[name].update(state="skipped")
                    set_progress(sources=list(status.values()), notices=list(notices))
                    continue
                except Exception as error:
                    notices.append(f"{name}: {describe(error)}")
                    status[name].update(state="error")
                    set_progress(sources=list(status.values()), notices=list(notices))
                    continue
                notices += [o["__error"] for o in raw if "__error" in o][:5]
                offers = [o for o in raw if "__error" not in o]
                found = len(offers)
                offers = [o for o in offers if keep_title(o["title"]) and keep_location(o["location"])]
                fresh_by_date = [o for o in offers if within_days(o["posted"], max_age)]
                stale += len(offers) - len(fresh_by_date)
                added = []
                for o in fresh_by_date:
                    key = company_role_key(o["company"], o["title"])
                    if o["url"] in seen_urls or key in seen_roles:
                        continue
                    seen_urls.add(o["url"])
                    seen_roles.add(key)
                    fit, matched = score_title(o["title"])
                    added.append({**o, "role_key": key, "fit": fit, "matched": matched})
                store.add_jobs(added)
                store.fill_descriptions(fresh_by_date)
                new_offers += added
                status[name].update(state="done", found=found, added=len(added), seconds=round(time.time() - started))
                set_progress(sources=list(status.values()), notices=list(notices), added=len(new_offers), stale=stale)

        STATE_PATH.write_text(json.dumps({"last_scan": datetime.now().isoformat(timespec="seconds")}))
        set_progress(new_ids=[store.job_id(o["url"]) for o in new_offers])
        settings_ = config.get("deep_score") or {}
        if settings_.get("enabled", True) and new_offers:
            min_fit = settings_.get("min_fit", 70)
            shortlist = sorted((o for o in new_offers if o["fit"] >= min_fit), key=lambda o: -o["fit"])[: settings_.get("limit", 60)]
            _score(store.unscored(0, len(shortlist), [store.job_id(o["url"]) for o in shortlist]) if shortlist else [])
        else:
            set_progress(running=False, phase="done", finished_at=datetime.now().isoformat(timespec="seconds"))
    except Exception as error:
        set_progress(running=False, phase="done", error=describe(error), finished_at=datetime.now().isoformat(timespec="seconds"))


def _score(jobs: list[dict]) -> None:
    set_progress(phase="score", to_score=len(jobs), scored=0)
    done = 0

    def check(job: dict) -> None:
        text, via = (job["description"], "scan") if len(job.get("description") or "") >= 200 else jd.fetch_jd(job["url"])
        result = deep.score(job, text, via)
        store.update_job(job["id"], deep_fit=result["fit"], verdict=result["verdict"], recommend=int(result["recommend"]), reasons=result["reasons"], gaps=result["gaps"], red_flags=result["red_flags"], jd_via=result["jd_via"], scored_at=result["scored_at"], description=text if len(text) >= 200 else job.get("description"))

    try:
        with ThreadPoolExecutor(SCORE_WORKERS) as pool:
            for future in as_completed([pool.submit(check, job) for job in jobs]):
                try:
                    future.result()
                except Exception as error:
                    with _lock:
                        _progress.setdefault("notices", []).append(f"AI check: {describe(error)}")
                done += 1
                set_progress(scored=done)
    finally:
        set_progress(running=False, phase="done", finished_at=datetime.now().isoformat(timespec="seconds"))


def auto_scan_loop() -> None:
    """Scans on a schedule while the server runs, when auto_scan_hours is set."""
    while True:
        try:
            hours = float(load_config().get("auto_scan_hours") or 0)
            last = last_scan()
            due = hours > 0 and (not last or datetime.now() - datetime.fromisoformat(last) >= timedelta(hours=hours))
            if due:
                start_scan()
        except Exception:
            pass
        time.sleep(600)


# ---- board helpers ----

WORKDAY_APPLY = "/apply/applyManually"


def apply_url(url: str) -> str:
    """The application form itself, not the posting page, where we can tell."""
    parsed = urlparse(url)
    host, path = parsed.hostname or "", parsed.path.rstrip("/")
    if host == "jobs.lever.co" and path.count("/") == 2:
        return parsed._replace(path=path + "/apply", fragment="").geturl()
    if host == "jobs.ashbyhq.com" and path.count("/") == 2:
        return parsed._replace(path=path + "/application", query="", fragment="").geturl()
    if host.endswith(".myworkdayjobs.com") and "/job/" in path and "/apply" not in path:
        return parsed._replace(path=path + WORKDAY_APPLY, query="", fragment="").geturl()
    return url


def board(days: int | None) -> dict:
    """Jobs for the Find jobs page, with applied state taken from the tracker."""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d") if days else None
    jobs = store.list_jobs(since)
    applied = applied_check()
    queued = store.queue_statuses()
    for job in jobs:
        job["apply_url"] = apply_url(job["url"])
        job["applied"] = applied(job)
        job["queue"] = queued.get(job["id"])
    return {"jobs": jobs, "progress": progress(), "counts": store.counts()}


def applied_check():
    """A test for whether a scanned job is in the tracker (or was applied on the old board)."""
    applications = tracker.load()
    keys = {a["key"] for a in applications}
    urls = {a.get("board_url") for a in applications} | {a.get("url") for a in applications}
    return lambda job: job["url"] in urls or tracker.job_key(job["url"]) in keys or bool(job.get("legacy_applied"))

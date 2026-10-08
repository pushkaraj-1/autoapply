"""Everything needed before filling one job: questions, answers and cover letter.

Each job gets a folder in data/runs. A cover letter already in that folder is
reused, so hand edits to cover_letter.txt are what gets uploaded.
"""

import json
import re
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import ModuleType

import yaml

from jobautomate import ashby, cover_letter, generic, greenhouse, lever, oracle, rippling, smartrecruiters, workday
from jobautomate.answers import Answer, pick, resolve_later, settle, us_option
from jobautomate.greenhouse import Field
from jobautomate.profile import ROOT, load_profile

RUNS = ROOT / "data" / "runs"
def letter_pdf_name() -> str:
    """The cover letter's file name, as recruiters see it: FirstLastCoverLetter.pdf."""
    name = load_profile()["name"]
    return re.sub(r"[^A-Za-z0-9]", "", f"{name['first']}{name['last']}") + "CoverLetter.pdf"
SITES = (greenhouse, ashby, lever, rippling, workday, oracle, smartrecruiters)


@dataclass
class Prepared:
    company: str
    title: str
    location: str
    run_dir: Path
    fields: list[Field]
    letter_text: str | None
    letter_pdf: Path | None
    warnings: list[str]


def site_for(url: str) -> tuple[ModuleType, str, str]:
    """Returns (site module, board, job id), or raises ValueError for unsupported links."""
    for site in SITES:
        try:
            return site, *site.parse_url(url)
        except ValueError:
            continue
    raise ValueError(f"This page is not a Greenhouse, Ashby, Lever, Rippling or Workday application I can read: {url}")


def site_or_generic(url: str) -> tuple[ModuleType, str, str]:
    """Like site_for, but any other page falls back to reading the job from the page."""
    try:
        return site_for(url)
    except ValueError:
        return generic, *generic.parse_url(url)


def run_dir_for(board: str, job_id: str) -> Path:
    existing = sorted(RUNS.glob(f"*-{board}-{job_id}"))
    if existing:
        return existing[-1]
    path = RUNS / f"{datetime.now():%Y-%m-%d}-{board}-{job_id}"
    path.mkdir(parents=True)
    return path


_letters = ThreadPoolExecutor(2)


def start_letter(fields: list[Field], company: str, title: str, location: str, description_html: str, run_dir: Path) -> Future | None:
    """Starts the cover letter while the AI answers are still being written, when the
    form has a cover letter upload. File questions are answered by rules, so their
    answers are ready almost at once."""
    for field in fields:
        if field.kind != "file":
            continue
        answer = field.answer.result() if isinstance(field.answer, Future) else field.answer
        if answer.value == "cover_letter":
            return _letters.submit(letter_for, company, title, location, description_html, run_dir)
    return None


def letter_for(company: str, title: str, location: str, description_html: str, run_dir: Path) -> tuple[str, Path]:
    text_path = run_dir / "cover_letter.txt"
    # The letter records which job it was written for; one written for any other
    # company or role is never reused.
    for_path = run_dir / "cover_letter_for.json"
    this_job = {"company": company, "title": title}
    written_for = json.loads(for_path.read_text()) if for_path.exists() else None
    if not text_path.exists() or (written_for is not None and written_for != this_job):
        letter = cover_letter.generate(company, title, location, cover_letter.job_description_text(description_html))
        text_path.write_text(letter + "\n")
        for_path.write_text(json.dumps(this_job))
    elif written_for is None:
        for_path.write_text(json.dumps(this_job))  # letters from before this check
    pdf_path = run_dir / letter_pdf_name()
    if not pdf_path.exists() or pdf_path.stat().st_mtime < text_path.stat().st_mtime:
        cover_letter.to_pdf(text_path.read_text().strip(), pdf_path)
    return text_path.read_text().strip(), pdf_path


NO_SPONSORSHIP = re.compile(
    r"sponsorship\W{0,3}(is )?(not available|unavailable|not offered|not provided)|"
    r"(unable|not able|cannot|can't|can not|will not|won't|do not|does not|don't) (to )?(currently )?(provide |offer )?sponsor|"
    r"no (visa )?sponsorship|without (the need for )?(visa )?sponsorship|not (be )?(eligible|able) for (visa )?sponsorship",
    re.I,
)


CITIZENS_ONLY = re.compile(
    r"(must|required to) be an? (us|u\.s\.|united states) citizen|(us|u\.s\.|united states) citizenship (is )?required|citizenship required|"
    r"only (us|u\.s\.) citizens|(active|current) (security )?clearance (is )?required|must (hold|have) an? (active )?(secret|top secret|ts/sci) clearance|"
    r"(ts/sci|top secret|secret)( clearance)?( with [\w ]{0,20}poly(graph)?)? (is )?required|"
    r"active clearance\W+(ts|top secret|secret|ci poly|full scope)|(obtain|eligible for) (and maintain )?an? (secret|top secret|ts/sci|security) clearance",
    re.I,
)


def quote(text: str, match: re.Match) -> str:
    """The line holding the match, or the words around it when the text has no line breaks."""
    start = max(text.rfind("\n", 0, match.start()) + 1, match.start() - 60)
    end = text.find("\n", match.end())
    end = min(end if end > 0 else len(text), match.end() + 60)
    return " ".join(text[start:end].split())


def form_warnings(fields: list[Field]) -> list[str]:
    """Warnings from the form's own questions: one that asks about U.S. citizenship
    means the job is probably limited to citizens (user, 2026-10-05: don't apply)."""
    return citizenship_warning([f.label for f in fields])


def citizenship_warning(labels: list[str]) -> list[str]:
    """form_warnings for question labels read straight from a page."""
    from jobautomate.answers import US_CITIZEN

    if "united states" in load_profile()["work_authorization"]["citizenship"].lower():
        return []
    asked = next((label for label in labels if re.search(US_CITIZEN, label.lower())), None)
    return [f"This application asks about U.S. citizenship (\"{asked[:90]}\"), so it is probably limited to citizens."] if asked else []


def warnings_for(description_html: str) -> list[str]:
    text = cover_letter.job_description_text(description_html)
    work = load_profile()["work_authorization"]
    found = []
    if work["needs_sponsorship_now_or_future"] and (match := NO_SPONSORSHIP.search(text)):
        found.append(f"This job says it does not sponsor visas: \"{quote(text, match)}\"")
    if "united states" not in work["citizenship"].lower() and (match := CITIZENS_ONLY.search(text)):
        found.append(f"This job requires US citizenship or a clearance: \"{quote(text, match)}\"")
    return found


def load_saved(run_dir: Path) -> dict:
    path = run_dir / "answers.yaml"
    if not path.exists():
        return {}
    entries = yaml.safe_load(path.read_text()) or {}
    return {field_id: entry["answer"] for field_id, entry in entries.items() if entry.get("answer") is not None}


def save_drafts(run_dir: Path, fields: list[Field]) -> None:
    """Keeps AI drafts and open questions in answers.yaml so they stay fixed and can be edited."""
    path = run_dir / "answers.yaml"
    entries = (yaml.safe_load(path.read_text()) or {}) if path.exists() else {}  # Workday sends one step at a time
    entries.update(
        (f.id, {"question": " ".join(f.label.split()), "answer": f.answer.value, "note": f.answer.note})
        for f in fields
        if f.answer.source in ("llm", "ask", "saved")
    )
    if entries:
        header = "# Answers for this job. Edit any answer and click Fill again. Blank answers need you.\n"
        (run_dir / "answers.yaml").write_text(header + yaml.safe_dump(entries, sort_keys=False, allow_unicode=True, width=100))


def fetch(site: ModuleType, board: str, job_id: str, url: str) -> dict:
    return site.fetch_url(url) if hasattr(site, "fetch_url") else site.fetch_job(board, job_id)


def prepare(url: str) -> Prepared:
    site, board, job_id = site_for(url)
    job = fetch(site, board, job_id, url)
    company, title, location, description_html = site.summary(job)
    run_dir = run_dir_for(board, job_id)
    (run_dir / "job.json").write_text(json.dumps(job, indent=2))
    fields = site.build_plan(job, load_saved(run_dir))
    letter = start_letter(fields, company, title, location, description_html, run_dir)
    settle(fields)
    save_drafts(run_dir, fields)
    (run_dir / "plan.json").write_text(json.dumps(greenhouse.plan_as_dicts(fields), indent=2, default=str))
    letter_text, letter_pdf = letter.result() if letter else (None, None)
    return Prepared(company, title, location, run_dir, fields, letter_text, letter_pdf, warnings_for(description_html) + form_warnings(fields))


FIELD_TYPES = {"multiselect": "multi_value_multi_select", "checkboxes": "MultiValueSelect", "file": "File", "textarea": "LongText", "number": "Number"}


def answer_fields(url: str, page_fields: list[dict], page_job: dict | None = None) -> Prepared:
    """Answers fields read from the page, for sites whose questions are not known up front.

    page_job carries company, title and description read from the page, for
    sites the server cannot fetch.
    """
    site, board, job_id = site_or_generic(url)
    job = page_job if site is generic else fetch(site, board, job_id, url)
    company, title, location, description_html = site.summary(job)
    context = cover_letter.job_description_text(description_html)
    run_dir = run_dir_for(board, job_id)
    (run_dir / "job.json").write_text(json.dumps(job, indent=2))
    saved = load_saved(run_dir)
    p = load_profile()
    history = {**history_answers(page_fields, p), **icims_answers(page_fields, p)}
    fields = []
    for f in page_fields:
        if f["key"] in history:
            answer = history[f["key"]]  # always from the profile, never from saved answers or the AI
        elif f["key"] in saved:
            answer = Answer(saved[f["key"]], "saved", "from answers.yaml")
        elif f["kind"] == "location":
            answer = Answer(f"{p['contact']['city']}, {p['contact']['state']}", "profile")
        else:
            field_type = FIELD_TYPES.get(f["kind"], "String")
            answer = resolve_later(f["label"], f["key"], field_type, f.get("options") or [], company, context, required=bool(f.get("required")))
        fields.append(Field(f["key"], f["label"], f["kind"], bool(f.get("required")), f.get("options") or [], answer))
    letter = start_letter(fields, company, title, location, description_html, run_dir)
    settle(fields)
    save_drafts(run_dir, fields)
    letter_text, letter_pdf = letter.result() if letter else (None, None)
    return Prepared(company, title, location, run_dir, fields, letter_text, letter_pdf, warnings_for(description_html) + form_warnings(fields))


# Workday's repeating sections name their boxes "<section>-<n>--<field>", for
# example workExperience-20--companyName. These are the applicant's own history.
SECTION_BOX = re.compile(r"^(workExperience|education|language|languages|websites?)-(\d+)--(\w+)$", re.I)


def history_answers(page_fields: list[dict], p: dict) -> dict[str, Answer]:
    """Answers for work experience and education boxes, straight from the profile:
    the first panel on the page gets the first entry, and so on. A label like
    "Company" or "From" on its own says nothing about whose company or which date,
    so these are never sent to the AI (which once answered them from the job ad)."""
    panels: dict[str, list[str]] = {}
    for f in page_fields:
        if m := SECTION_BOX.match(f["key"]):
            order = panels.setdefault(m.group(1).lower(), [])
            if m.group(2) not in order:
                order.append(m.group(2))
    answers = {}
    for f in page_fields:
        m = SECTION_BOX.match(f["key"])
        if not m:
            continue
        section, index, box = m.group(1).lower(), panels[m.group(1).lower()].index(m.group(2)), m.group(3).lower()
        entries = {"workexperience": p.get("experience") or [], "education": p.get("education") or []}.get(section)
        if entries is None:
            continue  # languages and websites: left to the usual rules
        if index >= len(entries):
            answers[f["key"]] = Answer(None, "ask", f"this {section} panel is extra: your profile has {len(entries)}; please delete it")
            continue
        e = entries[index]
        if section == "workexperience":
            value = {
                "jobtitle": e["title"],
                "companyname": e["company"],
                "company": e["company"],
                "location": e.get("location"),
                "currentlyworkhere": "Yes" if not e.get("end") else "No",
                "startdate": str(e["start"]),
                "enddate": str(e["end"]) if e.get("end") else None,
                "roledescription": " ".join(str(e.get("description", "")).split()),
            }.get(box, "__unknown")
        else:
            level = e["degree_level"]
            value = {
                "schoolname": e["school"],
                "school": e["school"],
                "degree": level,
                "fieldofstudy": e["discipline"],
                "gradeaverage": str(e.get("gpa", "")).split("/")[0].strip() or None,
                "firstyearattended": str(e["start"])[:4],
                "lastyearattended": str(e["end"])[:4],
            }.get(box, "__unknown")
            if box == "degree" and f.get("options"):
                value = pick(f["options"], level, level.split("'")[0] + "s", level.split("'")[0]) or level
        if value == "__unknown":
            answers[f["key"]] = Answer(None, "skip", "a history box this tool doesn't know")
        else:
            answers[f["key"]] = Answer(value, "profile", f"{section} entry {index + 1} from your profile") if value else Answer(None, "skip", "not needed for this entry")
    return answers


# iCIMS names its boxes "PersonProfileFields.<Field>", and repeating rows
# "<rowId>_CandProfileFields.<Field>" (dates split into _Month, _Date and _Year).
# Its labels ("Number", "Login") are too short to say what they want, so these are
# answered by name.
ICIMS_BOX = re.compile(r"^(?:(?P<row>-?\d+)_)?(?:PersonProfileFields|CandProfileFields)\.(?P<field>[A-Za-z]+?)(?:_(?P<part>Month|Date|Day|Year))?$")
MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]


def same_name(a: str, b: str) -> bool:
    key = lambda s: re.sub(r"[^a-z0-9]+", " ", s.lower().replace("&", " and ")).strip()
    return key(a) == key(b)


def date_part(value: str, part: str, options: list[str]) -> str | None:
    """One part of "2025-01" for iCIMS's split date boxes."""
    year, month = (str(value).split("-") + ["01"])[:2]
    if part == "Year":
        return pick(options, year) if options else year
    if part in ("Date", "Day"):
        return pick(options, "1", "01") if options else "1"
    m = int(month)
    if not options:
        return str(m)
    name = MONTH_NAMES[m - 1]
    return next((o for o in options if o.strip().lower() in (name.lower(), name[:3].lower(), str(m), f"{m:02d}")), None)


def icims_answers(page_fields: list[dict], p: dict) -> dict[str, Answer]:
    contact, answers = p["contact"], {}
    digits = re.sub(r"\D", "", contact["phone"])[-10:]
    rows: dict[str, list[str]] = {"education": [], "work": []}
    for f in page_fields:
        m = ICIMS_BOX.match(f["key"])
        if m and m["row"] and not m["row"].startswith("-1"):
            kind = "education" if re.search(r"School|Degree|Major|Education|Graduat|GPA", m["field"]) else "work" if re.search(r"Employer|Company|Title|Position|Work|Job", m["field"]) else None
            if kind and m["row"] not in rows[kind]:
                rows[kind].append(m["row"])
    for f in page_fields:
        m = ICIMS_BOX.match(f["key"])
        if not m:
            continue
        field, options, value = m["field"], f.get("options") or [], "__none"
        if field == "Login":
            answers[f["key"]] = Answer(None, "skip", "your iCIMS sign-in name; left as it is")
            continue
        person = {
            "FirstName": p["name"]["first"],
            "LastName": p["name"]["last"],
            "Email": contact["email"],
            "PhoneNumber": f"{digits[:3]}-{digits[3:6]}-{digits[6:]}",
            "AddressStreet1": contact["address_line1"],
            "AddressCity": contact["city"],
            "AddressZip": contact["postal_code"],
            "AddressPostalCode": contact["postal_code"],
        }
        if field in person:
            value = person[field]
        elif field in ("MiddleName", "AddressStreet2"):
            value = None
        elif field == "PhoneCountryCode":
            value = (us_option(options) or pick(options, "+1", "1")) if options else "+1"
        elif field == "PhoneType":
            value = pick(options, "Mobile", "Cell") if options else "Mobile"
        elif field == "AddressType":
            value = pick(options, "Home", "Primary", "Current") if options else None
        elif field == "AddressCountry":
            value = us_option(options) if options else contact["country"]
        elif field == "AddressState":
            value = pick(options, contact["state"], contact["state_code"]) if options else contact["state"]
        elif m["row"] in rows["education"]:
            index = rows["education"].index(m["row"])
            entries = p.get("education") or []
            if index >= len(entries):
                answers[f["key"]] = Answer(None, "ask", "an extra education row; your profile has fewer schools")
                continue
            e = entries[index]
            names, majors = e.get("names") or [e["school"]], e.get("majors") or [e["discipline"]]
            if field == "School":
                value = next((o for n in names for o in options if same_name(o, n)), None) if options else e["school"]
                if options and not value:
                    value = pick(options, "Other")  # with the name typed into OtherSchool
            elif field == "OtherSchool":
                value = e["school"]
            elif field == "Degree":
                level = e["degree_level"].split("'")[0]
                value = pick(options, e["degree_level"], level + "s", level) if options else e["degree_level"]
            elif field == "Major":
                value = next((o for n in majors for o in options if same_name(o, n)), None) if options else majors[0]
            elif field in ("GPA", "GradeAverage"):
                value = str(e.get("gpa", "")).split("/")[0].strip() or None
            elif "Start" in field and m["part"]:
                value = date_part(e["start"], m["part"], options)
            elif ("End" in field or "Graduat" in field) and m["part"]:
                value = date_part(e["end"], m["part"], options)
        elif m["row"] in rows["work"]:
            index = rows["work"].index(m["row"])
            entries = p.get("experience") or []
            if index >= len(entries):
                answers[f["key"]] = Answer(None, "ask", "an extra work row; your profile has fewer jobs")
                continue
            e = entries[index]
            if re.search(r"Employer|Company", field):
                value = e["company"]
            elif re.search(r"Title|Position", field):
                value = e["title"]
            elif re.search(r"Description|Responsib|Duties", field):
                value = " ".join(str(e.get("description", "")).split())
            elif "Current" in field:
                value = yes_no_value(options, not e.get("end"))
            elif "Start" in field and m["part"]:
                value = date_part(e["start"], m["part"], options)
            elif "End" in field and m["part"]:
                value = date_part(e["end"], m["part"], options) if e.get("end") else None
        if value != "__none":
            answers[f["key"]] = Answer(value, "profile", "from your profile") if value else Answer(None, "skip" if not f.get("required") else "ask", "not in your profile")
    return answers


def yes_no_value(options: list[str], yes: bool) -> str:
    from jobautomate.answers import yes_no

    return (yes_no(options, yes) if options else None) or ("Yes" if yes else "No")


def log_application(record: dict) -> None:
    record = {"date": datetime.now().isoformat(timespec="seconds"), **record}
    with open(ROOT / "data" / "applications.jsonl", "a") as log:
        log.write(json.dumps(record) + "\n")

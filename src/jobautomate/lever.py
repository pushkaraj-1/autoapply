"""Lever job boards: read the form from the apply page and plan every answer.

The apply page is rendered on the server, and each custom question card carries
its definition as JSON in a hidden baseTemplate input. The extension finds
every field by its name attribute.
"""

import json
import re
from html.parser import HTMLParser

import httpx

from jobautomate.answers import Answer, resolve_later
from jobautomate.cover_letter import job_description_text
from jobautomate.greenhouse import Field
from jobautomate.profile import load_profile

POSTING_API = "https://api.lever.co/v0/postings/{board}/{job_id}"
APPLY_PAGE = "https://jobs.lever.co/{board}/{job_id}/apply"

STANDARD = {  # name attribute -> (label, kind)
    "resume": ("Resume/CV", "file"),
    "name": ("Full name", "text"),
    "email": ("Email", "text"),
    "phone": ("Phone", "text"),
    "location": ("Current location", "location"),
    "org": ("Current company", "text"),
}
CARD_KINDS = {"dropdown": "select", "multiple-choice": "radio", "multiple-select": "checkboxes", "text": "text", "textarea": "text"}
EEO_LABELS = {"eeo[gender]": "Gender", "eeo[race]": "Race", "eeo[veteran]": "Veteran status", "eeo[disability]": "Disability status"}


class FormParser(HTMLParser):
    """Collects inputs, textareas and selects (with their options) in page order."""

    def __init__(self) -> None:
        super().__init__()
        self.items: list[tuple[str, dict]] = []
        self.select: tuple[dict, list[str]] | None = None

    def handle_starttag(self, tag: str, attrs: list) -> None:
        a = dict(attrs)
        if tag in ("input", "textarea"):
            self.items.append((tag, a))
        elif tag == "select":
            self.select = (a, [])
        elif tag == "option" and self.select and a.get("value"):
            self.select[1].append(a["value"])

    def handle_endtag(self, tag: str) -> None:
        if tag == "select" and self.select:
            self.items.append(("select", {**self.select[0], "options": self.select[1]}))
            self.select = None


def parse_url(url: str) -> tuple[str, str]:
    match = re.search(r"jobs\.lever\.co/([^/?#]+)/([0-9a-f-]{36})", url)
    if not match:
        raise ValueError(f"Not a Lever link I understand: {url}")
    return match.group(1), match.group(2)


def fetch_job(board: str, job_id: str) -> dict:
    posting = httpx.get(POSTING_API.format(board=board, job_id=job_id), timeout=30).raise_for_status().json()
    page = httpx.get(APPLY_PAGE.format(board=board, job_id=job_id), timeout=30, headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True)
    parser = FormParser()
    parser.feed(page.raise_for_status().text)
    company = re.search(r"<title>([^<]*?)\s+-\s", page.text)
    posting["company_name"] = company.group(1).strip() if company else board.title()
    posting["form"] = parser.items
    return posting


def build_plan(posting: dict, saved: dict | None = None) -> list[Field]:
    saved = saved or {}
    company = posting["company_name"]
    context = job_description_text(summary(posting)[3])
    p = load_profile()
    fields = []

    def add(field_id: str, label: str, kind: str, required: bool, options: list[str], field_type: str) -> None:
        if field_id in saved:
            answer = Answer(saved[field_id], "saved", "from answers.yaml")
        elif kind == "location":
            answer = Answer(f"{p['contact']['city']}, {p['contact']['state']}", "profile")
        else:
            answer = resolve_later(label, field_id, field_type, options, company, context, required=required)
        fields.append(Field(field_id, label, kind, required, options, answer))

    for tag, a in posting["form"]:
        name = a.get("name", "")
        if name in STANDARD:
            label, kind = STANDARD[name]
            add(name, label, kind, "required" in a, [], "File" if kind == "file" else "String")
        elif name.startswith("urls["):
            site = name[5:-1]
            add(name, f"{site} URL" if site != "Other" else "Other website", "text", "required" in a, [], "String")
        elif name.endswith("[baseTemplate]"):
            card = json.loads(a["value"])
            prefix = name.removesuffix("[baseTemplate]")
            for i, f in enumerate(card["fields"]):
                kind = CARD_KINDS.get(f["type"], f["type"])
                options = [o["text"] for o in f.get("options", [])]
                field_type = "MultiValueSelect" if kind == "checkboxes" else ("LongText" if f["type"] == "textarea" else "String")
                add(f"{prefix}[field{i}]", f["text"], kind, bool(f.get("required")), options, field_type)
        elif name in EEO_LABELS and tag == "select":
            add(name, EEO_LABELS[name], "select", False, a["options"], "ValueSelect")
        elif name == "comments":
            add(name, "Additional information", "text", False, [], "LongText")
    return fields


def summary(posting: dict) -> tuple[str, str, str, str]:
    """(company, title, location, description html)"""
    lists = "".join(f"<h3>{l['text']}</h3><ul>{l['content']}</ul>" for l in posting.get("lists", []))
    description = posting.get("description", "") + lists + posting.get("additional", "")
    return posting["company_name"], posting["text"].strip(), posting["categories"].get("location", ""), description

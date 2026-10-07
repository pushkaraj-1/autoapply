"""Writes a cover letter in the applicant's own format and renders it to PDF."""

import html
import re
from pathlib import Path

from playwright.sync_api import sync_playwright

from jobautomate import llm
from jobautomate.profile import content_text, cover_letter_sample, load_profile, resume_text

BANNED = {
    "em dash": "—",
    "en dash": "–",
    "markdown bold": "**",
}
CLOSING = "I've attached my resume. Thank you for your time."


def problems(letter: str) -> list[str]:
    found = [name for name, mark in BANNED.items() if mark in letter]
    if re.search(r"^\s*(#|[-*] )", letter, re.M):
        found.append("markdown heading or bullet")
    if not letter.startswith("Dear Hiring Team,"):
        found.append("does not start with 'Dear Hiring Team,'")
    if CLOSING not in letter:
        found.append("missing the closing line")
    if not letter.rstrip().endswith(load_profile()["name"]["first"] + " " + load_profile()["name"]["last"]):
        found.append("does not end with the name")
    return found


def generate(company: str, title: str, location: str, description: str, model: str | None = None) -> str:
    # The applicant's own notes for the AI (profile.yaml, ai_context).
    ai = load_profile().get("ai_context") or {}
    opening = ai.get("cover_letter_opening") or "where the applicant is in their studies or career"
    notes = "".join(f"\n- {note}" for note in ai.get("cover_letter_notes") or [])
    prompt = f"""Write a cover letter for {load_profile()["name"]["first"]} {load_profile()["name"]["last"]} for the "{title}" role at {company} ({location}).

Follow the SAMPLE exactly in format, tone and structure:
- "Dear Hiring Team," on its own line.
- Paragraph 1: {opening}, applying for this role at this company, and what about this team's work appeals, in plain words.
- Paragraphs 2 to 4: the most relevant work for THIS job, with concrete numbers, each tied to what the job needs. Pick facts that match the job description; do not just copy the sample's choices.
- Paragraph 5: what draws the applicant to this company and team specifically, and what they would bring.
- Then exactly: "{CLOSING}" then "Best," then the full name, each on its own line.
- No blank lines between paragraphs, same as the sample. About the same length as the sample.

Hard rules:
- Use only facts from the RESUME and EXTRA MATERIAL. Never invent numbers, tools, people or experience. Never claim experience with a tool the facts do not mention.
- Quote numbers exactly as the facts give them. Do not add words like "average" or "about" that change their meaning.
- Choose the facts closest to the job's main work.{notes}
- Simple, polite, human-sounding English in full sentences. No em dashes, no en dashes, no bold, no markdown, no bullet points, no short punchline sentences, no buzzwords like "passionate", "leverage", "synergy", "thrilled", "paramount", "keen", "eager", "rewarding" or "I am excited by the prospect".
- Output only the letter text.

SAMPLE:
{cover_letter_sample()}

JOB DESCRIPTION:
{description}

RESUME:
{resume_text()}

EXTRA MATERIAL:
{content_text()}"""
    messages = [{"role": "user", "content": prompt}]
    model = model or llm.WRITING_MODEL
    letter = llm.chat(messages, temperature=0.5, max_tokens=6000, model=model)
    for _ in range(2):
        issues = problems(letter)
        if not issues:
            break
        messages += [
            {"role": "assistant", "content": letter},
            {"role": "user", "content": f"Fix these problems and output only the full corrected letter: {', '.join(issues)}."},
        ]
        letter = llm.chat(messages, temperature=0.3, max_tokens=6000, model=model)
    return letter.strip()


def to_pdf(letter: str, path: Path) -> Path:
    paragraphs = "".join(f"<p>{html.escape(line)}</p>" for line in letter.splitlines() if line.strip())
    page_html = f"""<!doctype html><html><head><meta charset="utf-8"><style>
body {{ font-family: Helvetica, Arial, sans-serif; font-size: 11pt; line-height: 1.45; color: #111; }}
p {{ margin: 0 0 10pt 0; }}
</style></head><body>{paragraphs}</body></html>"""
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page()
        page.set_content(page_html)
        page.pdf(path=str(path), format="Letter", margin={"top": "0.9in", "bottom": "0.9in", "left": "1in", "right": "1in"})
        browser.close()
    return path


def job_description_text(content_html: str) -> str:
    text = html.unescape(content_html)
    text = re.sub(r"<(br|/p|/li|/h\d)[^>]*>", "\n", text)
    text = re.sub(r"<li[^>]*>", "- ", text)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"\n\s*\n+", "\n", text).strip()

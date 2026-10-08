"""Reads the security code a job site emails after Submit, from the application inbox.

Uses Gmail's IMAP with an app password (OTP_EMAIL_ADDRESS and OTP_EMAIL_APP_PASSWORD
in .env). The inbox is opened read-only, and only code emails from the job site that
arrived after Submit, for the same company, are read; nothing else is looked at.
"""

import email
import html
import imaplib
import re
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime

from dotenv import dotenv_values

from jobautomate.profile import ROOT

# What each site's code email looks like. Greenhouse: "Security code for your
# application to <company>", and in the body "...security code field on your
# application: <code> After you enter the code...". Oracle Recruiting Cloud sends
# from each company's own address with the company's own wording (Texas
# Instruments: "Confirm your identity", "...using the one-time pass code : 123456"),
# so its emails are found by the company's name and a code phrase. {company} in a
# search is the company's name.
CODE_WORDS = r"(?:verification|security|one[- ]time|pass|access|confirmation)[ -]?(?:pass ?)?code|passcode|\bPIN\b"
SITES = {
    "greenhouse": {
        "searches": [f'FROM "{sender}" SUBJECT "security code"' for sender in ("greenhouse-mail.io", "greenhouse.io")],
        "code": re.compile(r"security code field on your application:\s*([A-Za-z0-9]{6,12})\b", re.I),
        "company_in": "subject",
    },
    "oracle": {
        "searches": ['SUBJECT "confirm your identity"', 'TEXT "{company}" BODY "code"'],
        "code": re.compile(rf"(?:{CODE_WORDS})\b[^0-9]{{0,120}}?\b(\d{{6}})\b|\b(\d{{6}})\b[^0-9]{{0,60}}?(?:{CODE_WORDS})", re.I),
        "company_in": "anywhere",
    },
}

def configured() -> bool:
    env = dotenv_values(ROOT / ".env")
    return bool(env.get("OTP_EMAIL_ADDRESS") and env.get("OTP_EMAIL_APP_PASSWORD"))


def _text(message: Message) -> str:
    for kind in ("text/plain", "text/html"):
        for part in message.walk():
            if part.get_content_type() == kind:
                body = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
                if kind == "text/html":  # the words only: no styles, tags or &nbsp;
                    body = html.unescape(re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(style|script|head)\b.*?</\1>", " ", body)))
                return body
    return ""


def _same_company(text: str, company: str) -> bool:
    words = [w for w in re.findall(r"[a-z0-9]+", company.lower()) if w not in ("inc", "llc", "ltd", "corp", "the", "co")]
    return not words or words[0] in text.lower()


def find_code(since: datetime, company: str, site: str = "greenhouse") -> str | None:
    """The newest code `site` emailed for `company` after `since`, or None."""
    rules = SITES[site]
    env = dotenv_values(ROOT / ".env")
    box = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=20)
    try:
        box.login(env["OTP_EMAIL_ADDRESS"], env["OTP_EMAIL_APP_PASSWORD"])
        box.select("INBOX", readonly=True)
        day = (since - timedelta(days=1)).strftime("%d-%b-%Y")
        found = []
        name = re.sub(r'["\\]', "", company)[:60]
        for search in rules["searches"]:
            status, data = box.search(None, f'(SINCE "{day}" {search.replace("{company}", name)})')
            found += data[0].split() if status == "OK" and data and data[0] else []
        for msg_id in reversed(sorted(set(found), key=int)):
            status, parts = box.fetch(msg_id, "(BODY.PEEK[])")
            if status != "OK" or not parts or not isinstance(parts[0], tuple):
                continue
            message = email.message_from_bytes(parts[0][1])
            sent = parsedate_to_datetime(message.get("Date")) if message.get("Date") else None
            if sent is None or sent < since - timedelta(seconds=30):
                break  # newest first, so everything after this is older
            subject = str(make_header(decode_header(message.get("Subject", ""))))
            body = " ".join(_text(message).split())
            sender = str(make_header(decode_header(message.get("From", ""))))
            where = subject if rules["company_in"] == "subject" else f"{sender} {subject} {body}"
            if not _same_company(where, company):
                continue
            match = rules["code"].search(body)
            if match:
                return next(g for g in match.groups() if g)
        return None
    finally:
        try:
            box.logout()
        except Exception:
            pass


def now() -> datetime:
    return datetime.now(timezone.utc)

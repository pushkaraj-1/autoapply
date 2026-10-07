"""Title and location filters, recency and the duplicate key (port of lib/scan-io.mjs)
plus the title fit score (port of lib/fit-score.mjs)."""

import re
from datetime import date, datetime

US_STATE = re.compile(
    r",\s*(?:AL|AK|AZ|AR|CA|CO|CT|DC|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)(?:[,\s]|$)"
)
US_KEYWORD = re.compile(r"\b(United States|Remote\s*[-,]\s*US|US\s*Remote|USA)\b", re.I)
# Remote with no country attached; someone in the US can take these.
REMOTE = re.compile(r"\b(remote|anywhere|worldwide|distributed|work from home|wfh)\b", re.I)
# A named non-US place wins even when "remote" appears ("Remote, Europe").
NON_US = re.compile(
    r"\b(europe|EMEA|APAC|LATAM|United Kingdom|UK|Ireland|India|Canada|Germany|France|Spain|Netherlands|Australia|Singapore|Brazil|Mexico|Poland|Portugal|Nigeria|Pakistan|Philippines|Ukraine|Japan|China|Israel|Argentina|Colombia|Sweden|Switzerland|Austria|Belgium|Denmark|Norway|Finland|Italy|Romania|Turkey|Egypt|Kenya|Vietnam|Indonesia|Thailand|Malaysia|Bangladesh|EU)\b",
    re.I,
)


def word_patterns(words, flexible: bool = False) -> list[tuple[str, re.Pattern]]:
    # flexible lets role nouns match variants: "Software Engineer" also matches "Software Engineering".
    suffix = r"(?:s|ing)?" if flexible else ""
    return [(w, re.compile(rf"\b{re.escape(w)}{suffix}\b", re.I)) for w in words or []]


def title_filter(cfg: dict | None):
    positive = [p for _, p in word_patterns((cfg or {}).get("positive"))]
    negative = [p for _, p in word_patterns((cfg or {}).get("negative"))]
    return lambda title: (not positive or any(p.search(title) for p in positive)) and not any(p.search(title) for p in negative)


def location_filter(cfg: dict | None):
    if not cfg or cfg.get("us_only") is False:
        return lambda location: True
    allow_empty = cfg.get("allow_empty", True)
    allow_remote = cfg.get("allow_remote", True)

    def keep(location: str) -> bool:
        if not location or not location.strip():
            return allow_empty
        if US_STATE.search(location) or US_KEYWORD.search(location):
            return True
        if NON_US.search(location):
            return False
        return bool(allow_remote and REMOTE.search(location))

    return keep


def within_days(posted: str, max_days: int, today: date | None = None) -> bool:
    """Undated jobs are kept; max_days <= 0 turns the check off."""
    if not max_days or max_days <= 0 or not posted:
        return True
    try:
        day = datetime.strptime(posted[:10], "%Y-%m-%d").date()
    except ValueError:
        return True
    age = ((today or date.today()) - day).days
    return -1 <= age <= max_days


def company_role_key(company: str, title: str) -> str:
    """The same job posted on several boards collapses to one key."""
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
    return f"{norm(company)}::{norm(title)}"


# ---- title fit score ----

BASE, ROLE_BONUS, SKILL_EACH, SKILL_CAP, LEVEL_BONUS, OFF_PENALTY = 20, 45, 8, 32, 15, 40


def fit_scorer(cfg: dict | None):
    """0-100 from the title alone: strong role +45, each core skill +8 (max +32),
    target level +15, off-target word -40, starting at 20."""
    cfg = cfg or {}
    groups = {
        "role": word_patterns(cfg.get("strong_roles"), flexible=True),
        "skills": word_patterns(cfg.get("core_skills")),
        "level": word_patterns(cfg.get("boost_levels")),
        "off": word_patterns(cfg.get("off_target")),
    }

    def score(title: str) -> tuple[int, dict]:
        matched = {name: [w for w, p in patterns if p.search(title or "")] for name, patterns in groups.items()}
        value = BASE + (ROLE_BONUS if matched["role"] else 0) + min(len(matched["skills"]) * SKILL_EACH, SKILL_CAP)
        value += LEVEL_BONUS if matched["level"] else 0
        value -= OFF_PENALTY if matched["off"] else 0
        return max(0, min(100, value)), matched

    return score

"""AI fit check (port of deep-score.mjs + lib/llm-score.mjs): reads the full job
description and judges it against the resume like a strict recruiter."""

import json
import os
import re
from datetime import datetime

from jobautomate.llm import chat
from jobautomate.prepare import CITIZENS_ONLY, NO_SPONSORSHIP
from jobautomate.profile import load_profile, resume_text

JD_CHARS = 8000  # descriptions are cut to keep each check cheap
CV_CHARS = 3500
MODEL = os.environ.get("OPENROUTER_SCORING_MODEL", "google/gemini-2.5-flash")


def candidate_summary() -> str:
    """Who the candidate is, for the recruiter prompt: the applicant's own summary
    (profile.yaml, ai_context.candidate_summary), else one built from the profile."""
    p = load_profile()
    if summary := (p.get("ai_context") or {}).get("candidate_summary"):
        return " ".join(summary.split())
    work = p["work_authorization"]
    school = p["education"][0]
    return (
        f"{school['degree_level']} in {school['discipline']} from {school['school']}, finishing {school['end']}. "
        f"{work['citizenship']} citizen on {work['us_status']}; "
        f"{'will need visa sponsorship' if work['needs_sponsorship_now_or_future'] else 'does not need visa sponsorship'}. "
        f"About {p.get('years_of_experience', 0)} years of experience."
    )


def build_prompt(job: dict, jd_text: str) -> str:
    jd = (jd_text or "")[:JD_CHARS].strip()
    return f"""You are a strict, experienced technical recruiter. Score how well THIS candidate fits THIS job for a real application decision. The candidate is a new-grad / intern / early-career seeker. Penalize roles that require senior levels, many years of experience, active security clearance, US citizenship, or no visa sponsorship, even if the topic matches.

Return ONLY a JSON object (no prose, no markdown) with exactly:
{{
  "fit": <integer 0-100>,
  "verdict": "strong" | "possible" | "weak" | "no",
  "recommend_apply": <boolean>,
  "reasons": [<up to 3 short strings: concrete matches>],
  "gaps": [<up to 3 short strings: missing/weak requirements>],
  "red_flags": [<0-3 short strings: disqualifiers e.g. "requires 8+ yrs", "US citizenship/clearance", "senior-only", "no visa sponsorship", "on-site only">]
}}

Scoring guide: 80-100 strong (clear match, apply), 55-79 possible (worth a tailored application), 30-54 weak, 0-29 no. Be realistic, not generous.

Red flags must come from what the job description says. What the CANDIDATE section says about the candidate's own visa or sponsorship needs is already known; do not list it as a red flag. Flag sponsorship only when the description says it will not sponsor visas, or requires US citizenship, a green card or a security clearance.

CANDIDATE
{candidate_summary()}
\"\"\"
{resume_text()[:CV_CHARS].strip()}
\"\"\"

JOB: {job['title']} @ {job.get('company') or 'unknown'}
JOB DESCRIPTION:
\"\"\"
{jd or '(job description could not be retrieved; score conservatively from the title only and add the red flag "JD unavailable")'}
\"\"\""""


def clamp(result: dict) -> dict:
    lists = lambda v: [x for x in v if isinstance(x, str)][:3] if isinstance(v, list) else []
    try:
        fit = max(0, min(100, round(float(result.get("fit")))))
    except (TypeError, ValueError):
        fit = 0
    verdict = result.get("verdict")
    if verdict not in ("strong", "possible", "weak", "no"):
        verdict = "strong" if fit >= 80 else "possible" if fit >= 55 else "weak" if fit >= 30 else "no"
    recommend = result.get("recommend_apply")
    return {
        "fit": fit,
        "verdict": verdict,
        "recommend": recommend if isinstance(recommend, bool) else fit >= 55,
        "reasons": lists(result.get("reasons")),
        "gaps": lists(result.get("gaps")),
        "red_flags": lists(result.get("red_flags")),
    }


def parse_score(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.I).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON in the reply: {text[:80]}")
    return clamp(json.loads(text[start : end + 1]))


def known_flags(jd_text: str) -> list[str]:
    """Free checks that don't depend on the AI noticing them."""
    flags = []
    if NO_SPONSORSHIP.search(jd_text or ""):
        flags.append("No visa sponsorship")
    if CITIZENS_ONLY.search(jd_text or ""):
        flags.append("US citizenship or clearance required")
    return flags


def score(job: dict, jd_text: str, jd_via: str) -> dict:
    result = parse_score(chat([{"role": "user", "content": build_prompt(job, jd_text)}], temperature=0.2, max_tokens=700, model=MODEL))
    for flag in known_flags(jd_text):
        if not any(flag.split()[0].lower() in f.lower() for f in result["red_flags"]):
            result["red_flags"].append(flag)
    if result["red_flags"] and known_flags(jd_text):
        result["recommend"] = False
    return {**result, "jd_via": jd_via if len(jd_text or "") >= 200 else "no-jd", "scored_at": datetime.now().isoformat(timespec="seconds")}

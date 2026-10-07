"""Decides the answer for each application question.

Known questions are answered by rules over profile.yaml. Anything else goes to
the LLM with the profile, resume and extra material as context, and is marked
for review.
"""

import json
import re
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

from jobautomate import llm
from jobautomate.profile import content_text, experience_answer_sample, load_profile, resume_text

SANCTIONED = ("cuba", "iran", "north korea", "syria", "crimea")


@dataclass
class Answer:
    value: str | list[str] | None  # None means leave the field empty
    source: str  # "profile", "rule", "llm", or "skip"
    note: str = ""


def pick(options: list[str], *wanted: str) -> str | None:
    """Returns the first option that equals, starts with, or contains a wanted string."""
    lowered = [o.lower().strip() for o in options]
    for w in wanted:
        w = w.lower()
        for test in (lambda o: o == w, lambda o: o.startswith(w), lambda o: w in o):
            for option, low in zip(options, lowered):
                if test(low):
                    return option
    return None


def yes_no(options: list[str], yes: bool) -> str | None:
    if yes:
        return pick(options, "yes", "i agree", "agree", "i accept", "accept", "true")
    return pick(options, "no", "i do not", "i don't", "disagree", "decline", "false")


def us_option(options: list[str]) -> str | None:
    """The United States in a country list, even when written "🇺🇸 (+1) United States".
    Checked before looser matches so "United States Minor Outlying Islands" is not picked."""
    for option in options:
        if re.search(r"\bunited states( of america)?\s*(\(?\+1\)?)?\s*$|^\W*(usa|us|u\.s\.a?\.?)\s*$", option.lower()):
            return option
    return pick(options, "United States", "United States of America", "USA")


# Questions are answered side by side: an AI answer waits a few seconds on the
# network, and one after another they added up to half a minute on a long form.
_pool = ThreadPoolExecutor(8)


def resolve_later(*args, **kwargs) -> Future:
    """resolve() in the background; settle() collects the answers."""
    return _pool.submit(resolve, *args, **kwargs)


def settle(fields: list) -> list:
    """Waits for answers started with resolve_later and puts them on their fields."""
    for field in fields:
        if isinstance(field.answer, Future):
            field.answer = field.answer.result()
    return fields


def resolve(label: str, field_name: str, field_type: str, options: list[str], company: str, context: str = "", required: bool = True) -> Answer:
    p = load_profile()
    q = " ".join(label.lower().split())
    multi = field_type in ("multi_value_multi_select", "MultiValueSelect")

    def choice(option: str | None) -> str | list[str] | None:
        """Wraps a picked option in a list for fields that accept several."""
        return [option] if multi and option else option

    full_name = f"{p['name']['first']} {p['name']['last']}"
    # Phone boxes get the 10-digit number (user, 2026-10-06): masked boxes and boxes
    # with their own country picker turn "+1 555 123 4567" into nonsense.
    phone = re.sub(r"^1(?=\d{10}$)", "", re.sub(r"\D", "", p["contact"]["phone"]))
    direct = {
        "first_name": p["name"]["first"],
        "last_name": p["name"]["last"],
        "email": p["contact"]["email"],
        "phone": phone,
        "_systemfield_name": full_name,
        "name": full_name,
        "_systemfield_email": p["contact"]["email"],
    }
    if field_name in direct:
        return Answer(direct[field_name], "profile")
    if field_name in ("resume_text", "cover_letter_text"):
        return Answer(None, "skip", "a file is uploaded instead")
    if field_type in ("input_file", "File"):
        # The answer names which file to upload.
        if "cover" in q:
            return Answer("cover_letter", "profile", "file upload")
        if "resume" in q or "cv" in q.split() or field_name in ("resume", "_systemfield_resume"):
            return Answer("resume", "profile", "file upload")
        if re.search(r"photo|picture|avatar|headshot|image", q):
            return Answer(None, "skip", "no photo is uploaded")
        return Answer(None, "ask", "unknown file upload, needs your answer")
    if q in ("additional information", "anything else you would like to share?", "comments"):
        return Answer(None, "skip", "optional")
    if q in ("first name", "given name", "legal first name", "first"):
        return Answer(p["name"]["first"], "profile")
    if q in ("last name", "surname", "family name", "legal last name", "last"):
        return Answer(p["name"]["last"], "profile")
    contact = p["contact"]
    if re.fullmatch(r"(street )?address( line)?( 1)?|street|address line one", q):
        return Answer(contact["full_address"] if q in ("address",) else contact["address_line1"], "profile")
    if re.fullmatch(r"(full |home |current |mailing )?address", q):
        return Answer(contact["full_address"], "profile")
    if re.fullmatch(r"(postal|zip|zip code|postal code|zip/postal code|postcode)", q):
        return Answer(contact["postal_code"], "profile")
    if re.fullmatch(r"address line 2|apartment.*|suite.*|county", q):
        return Answer(None, "skip", "optional")
    if re.fullmatch(r"(set )?(your |current )?location( \(city\))?|city, state|current city|where are you (currently )?(located|based)\??", q) and not options:
        return Answer(f"{contact['city']}, {contact['state']}", "profile")
    if q == "city":
        return Answer(contact["city"], "profile")
    if re.fullmatch(r"(county/)?state(/province| or province|/region)?|province", q):
        return Answer(pick(options, contact["state"], contact["state_code"]) if options else contact["state"], "profile")
    if q in ("middle name", "middle initial"):
        return Answer(None, "skip", "optional")
    # "Full legal name in native language (e.g. Chinese characters)": the English name (user, 2026-10-05).
    if "name" in q and re.search(r"native (language|script)|chinese characters|kanji|local (language|script)|non-latin|original script", q) and not options:
        return Answer(full_name, "profile")
    # Permanent address is the current home address (user, 2026-10-05).
    if re.search(r"permanent (residen\w*|home|mailing)?\s*(street )?address", q) and not options:
        return Answer(contact["address_line1"] if "street" in q else contact["full_address"], "profile")
    if q in ("name", "full name", "legal name", "full legal name"):
        return Answer(full_name, "profile")
    if not options and ("phone" in q or (len(q) <= 40 and re.search(r"\b(mobile|cell|telephone)\b", q) and not re.search(r"experience|develop|app", q))):
        return Answer(phone, "profile")
    # Pay is in U.S. dollars.
    if re.search(r"\bcurrenc(y|ies)\b", q) and (options or len(q) <= 30):
        if not options:
            return Answer("USD", "profile")
        found = next((o for o in options if re.search(r"\busd\b|\bu\.?s\.? dollars?\b|united states dollars?", o, re.I)), None) or next((o for o in options if o.strip() in ("$", "US$")), None)
        return Answer(choice(found), "profile" if found else "ask", "" if found else "needs your answer: USD is not in the list")
    if q in ("email", "email address", "confirm email", "confirm your email", "confirm email address", "re-enter email", "repeat email", "verify email"):
        return Answer(p["contact"]["email"], "profile")
    if re.fullmatch(r"(phone |mobile )?country( code)?|country( of residence)?|country/(region|territory)|country or region", q):
        if not options:
            return Answer("+1" if "code" in q else contact["country"], "profile")
        found = us_option(options)
        return Answer(choice(found), "profile" if found else "ask", "" if found else "needs your answer: United States is not in the list")

    link_field = len(q) <= 60 and not options  # long questions can mention a website without asking for one
    if link_field and "linkedin" in q:
        return Answer(p["links"]["linkedin"], "profile")
    if link_field and "github" in q:
        return Answer(p["links"]["github"], "profile")
    if link_field and ("website" in q or "portfolio" in q):
        return Answer(p["links"]["website"], "profile")
    # Other profile links (Google Scholar, X, Kaggle, ...): there is none to give.
    if link_field and re.search(r"scholar|twitter|\bx\b|kaggle|dribbble|behance|stack ?overflow|medium|leetcode|hugging ?face|profile|url|\blink\b|handle", q) and not re.search(r"name|email|phone|address|why|describe", q):
        if not required:
            return Answer(None, "skip", "optional; no such profile to link")
        return Answer(None, "ask", "needs your answer: asks for a profile link that is not in your profile")
    # "Skills" / "List your technical skills": the resume's skills. A list of skills to
    # tick gets every one that is on the resume.
    skills = p.get("skills") or []
    if skills and re.fullmatch(r"((technical|key|relevant|top|core) )?skills( and (tools|technologies))?( \(.*\))?|(please )?list (your|any) (relevant |technical )?skills.*|what (technical )?skills do you have\??", q):
        if not options:
            return Answer(", ".join(skills), "profile")
        have = {s.lower() for s in skills}
        found = [o for o in options if o.lower().strip() in have]
        if found:
            return Answer(found if multi else found[0], "profile")
    if "pronoun" in q:
        return Answer(p.get("pronouns"), "skip" if not p.get("pronouns") else "profile", "optional")
    if "preferred" in q and "name" in q:
        return Answer(p["name"].get("preferred"), "skip", "optional")

    # Not a U.S. citizen; jobs that ask are skipped by the queue (prepare.form_warnings).
    if re.search(US_CITIZEN, q):
        citizen = "united states" in p["work_authorization"]["citizenship"].lower()
        if not options:
            return Answer("Yes" if citizen else "No", "rule")
        found = yes_no(options, citizen) if pick(options, "yes") else (None if citizen else pick(options, "f-1", "visa", "non-citizen", "not a", "none of", "other"))
        if found:
            return Answer(choice(found), "rule")
    if options and (re.search(r"\bsponsor", q) or "visa" in q):
        return Answer(yes_no(options, p["work_authorization"]["needs_sponsorship_now_or_future"]), "rule")
    # OPT starts after graduation, so "are you currently on OPT or CPT" is No for now.
    if options and re.search(r"\b(currently|now|presently)\b.{0,40}\b(opt|cpt)\b", q):
        return Answer(yes_no(options, p["work_authorization"].get("currently_on_opt_or_cpt", False)), "rule")
    if re.search(r"without (any )?restriction|unrestricted|without (the need for |requiring )?(visa )?sponsorship", q) and options:
        unrestricted = p["work_authorization"].get("authorized_without_restriction")
        if unrestricted is None:
            return Answer(None, "ask", "needs your answer: work authorization 'without restriction' (you will need sponsorship later)")
        return Answer(yes_no(options, unrestricted), "rule")
    sponsor_option = next((o for o in options if re.search(r"sponsor", o, re.I)), None)
    if re.search(r"work authori[sz]ation|authori[sz]ation status|work status|visa status", q) and sponsor_option:
        unrestricted = p["work_authorization"].get("authorized_without_restriction")
        if unrestricted is None:
            return Answer(None, "ask", "needs your answer: work authorization status (one option says you require sponsorship)")
        any_employer = pick(options, "any employer", "authorized to work", "citizen")
        return Answer(choice(any_employer if unrestricted else sponsor_option), "rule")
    if re.search(r"(authori[sz]ed|eligible|legally able|right) to work|work authori[sz]ation", q) and pick(options, "yes"):
        return Answer(yes_no(options, p["work_authorization"]["authorized_to_work_us"]), "rule")
    # Written questions about visa, sponsorship or work authorization get the user's
    # own wording, but only when the field is compulsory (user, 2026-10-05).
    if not options and re.search(r"\bsponsor|\bvisa\b|immigration|work authori[sz]ation|authori[sz]ed to work|work (permit|status)|\bopt\b(?! (in|out))|\bcpt\b", q):
        if not required:
            return Answer(None, "skip", "optional; work authorization is only explained when required")
        return Answer(p["work_authorization"]["statement"], "rule")

    # SAT / ACT were never taken; a compulsory question open to any test gets the GRE.
    scores = p["standing_answers"].get("test_scores") or {}
    if not options and re.search(r"\b(sat|act|gre|gmat)\b|standardi[sz]ed test|test scores?", q) and len(q) < 200:
        if "gre" in q.split() or re.search(r"\bgre\b", q):
            if re.fullmatch(r"(your )?gre( total)?( score)?\??", q) and scores.get("gre"):
                return Answer(str(scores["gre"]), "rule")
        any_test = re.search(r"\bother\b|\bany\b|standardi[sz]ed|\bgre\b|gmat|test scores?", q)
        if any_test and required and scores.get("gre"):
            return Answer(f"GRE: {scores['gre']}", "rule")
        if re.search(r"\b(sat|act)\b", q):
            return Answer("N/A", "rule")
        if not required:
            return Answer(None, "skip", "optional test score")
    if re.search(r"relocation (assistance|package|support|help|benefits?|reimbursement)", q) and options and pick(options, "yes"):
        return Answer(yes_no(options, p["preferences"].get("relocation_assistance", False)), "rule")
    on_own = next((o for o in options if re.search(r"relocat", o, re.I) and re.search(r"\b(not|n't|without|no)\b.{0,25}(assistance|support|package)|on my own|own expense|self[- ]?fund", o, re.I)), None)
    if on_own and not p["preferences"].get("relocation_assistance"):
        return Answer(choice(on_own), "rule")
    if re.search(r"salary|compensation|\bpay\b|hourly rate|rate per hour|desired rate|expected rate", q):
        if options:
            # "Are you comfortable with the salary range / fixed salary?" (user, 2026-10-05: yes).
            if pick(options, "yes") and pick(options, "no"):
                return Answer(yes_no(options, p["standing_answers"]["accept_stated_salary"]), "rule")
            return Answer(None, "ask", "needs your answer: this asks you to pick a salary range")
        salary = p["standing_answers"]["salary"]
        hourly = re.search(r"hour|hourly|/hr|per hr", q)
        numbers_only = field_type == "Number"
        if hourly:
            return Answer(str(salary["hourly_number"]) if numbers_only else salary["hourly_text"], "rule")
        return Answer(str(salary["number"]) if numbers_only else salary["text"], "rule")
    if re.search(r"how did you (hear|find|learn|come across)|where did you (hear|find|see)|referr?al source|source of (this )?application", q):
        sources = p["standing_answers"]["job_source"]
        if not options:
            return Answer(sources[0], "rule")
        found = pick(options, *sources) or pick(options, "job board", "online", "website", "internet", "other")
        return Answer(choice(found), "rule" if found else "ask", "" if found else "needs your answer: no familiar source listed")
    if re.search(r"\btravel", q) and options and pick(options, "yes"):
        return Answer(yes_no(options, p["preferences"]["willing_to_work_onsite"]), "rule")
    # "If you are a California resident, please check this box" (privacy notices).
    state_resident = re.search(r"\b(california|ca|new york|texas|washington|colorado|illinois|virginia|massachusetts) resident", q)
    if state_resident and options:
        home_state = state_resident.group(1) in (p["contact"]["state"].lower(), p["contact"]["state_code"].lower())
        return Answer(yes_no(options, home_state), "rule")
    if options and re.search(r"(open|willing) to (relocat|move)|or relocat", q) and pick(options, "yes"):
        return Answer(yes_no(options, p["preferences"]["willing_to_work_onsite"]), "rule")
    residence = re.search(r"(resid\w*|live|living|located|based) in ([^?]+)", q)
    if residence and options and not re.search(r"if you (are|do) not|if not", q):
        place = residence.group(2)
        home = (p["contact"]["city"].lower(), p["contact"]["state"].lower(), "united states", "the us", "u.s.", "usa")
        if not any(h in place for h in home):
            return Answer(None, "ask", f"needs your answer: asks if you currently live in {place.strip()[:40]}; you live in {p['contact']['city']}")
    arrangement = re.search(r"remote|hybrid|on-?site|in[- ]office|in person|relocat|commut|work(ing)? (model|arrangement|environment)", q)
    location_preference = (
        re.search(r"prefer|preference|open to|willing to work", q) and re.search(r"location|office|city|cities|where", q)
    ) or re.search(r"which (office|location)|where would you (like|prefer) to work", q)
    if arrangement or location_preference:
        prefs = p["preferences"]
        if options and pick(options, "yes") and pick(options, "no"):
            return Answer(yes_no(options, prefs["willing_to_work_onsite"]), "rule")
        if options and prefs["open_to_any_arrangement"]:
            if multi:  # select every option that is a work arrangement or place
                return Answer([o for o in options if not re.search(r"\bnone\b|\bnot\b|decline|neither|only interested in remote", o, re.I)], "rule")
            flexible = pick(options, "both", "any", "either", "all of", "flexible", "open to", "no preference")
            if flexible:
                return Answer(flexible, "rule")
            if arrangement:
                return Answer(pick(options, "hybrid", "on-site", "onsite", "in office", "in-office"), "rule")
            return Answer(None, "ask", "needs your answer: this asks you to pick one location")
        if not options and location_preference:
            return Answer(prefs["location_text"], "rule")
        if not options and re.search(r"prefer|preference|willing|open to|able to", q):
            return Answer("Open to onsite, hybrid or remote work at any location.", "rule")
    if "recording" in q and "consent" in q:
        consent = p["standing_answers"].get("interview_recording_consent")
        if consent is None:
            return Answer(None, "skip", "optional, left for you to choose")
        return Answer(pick(options, "yes") if consent else pick(options, "opt out", "no"), "rule")
    if re.search(r"(current|most recent|latest) (company|employer)", q) and not re.search(r"non-?\s?compete", q):
        return Answer(p["current_role"]["company"], "profile")
    if "current title" in q or "current job title" in q:
        return Answer(p["current_role"]["title"], "profile")

    if sum(country in q for country in SANCTIONED) >= 2:
        sanctioned = any(c in p["work_authorization"]["citizenship"].lower() for c in SANCTIONED)
        return Answer(yes_no(options, sanctioned), "rule", f"citizenship: {p['work_authorization']['citizenship']}")

    if "government" in q:
        worked = p["history"]["worked_for_us_government"]
        if not options:  # the "please list the entity" follow-up
            return Answer("N/A" if not worked else None, "rule")
        return Answer(pick(options, "no") if not worked else pick(options, "yes"), "rule")

    company_core = re.sub(r",?\s+(inc|llc|ltd|corp|corporation|co|company)\.?$", "", company.lower().strip())
    if company_core and company_core in q and re.search(r"employed|worked|engaged|work for", q):
        employers = [e.lower() for e in p["history"]["employers"]]
        worked = any(company_core in e or e in company_core for e in employers)
        if options:
            return Answer(yes_no(options, worked), "rule")
        return Answer("Yes" if worked else "No", "rule")
    if re.fullmatch(r"(total |overall |professional )?years of (professional |relevant |work )?experience", q):
        years = p["years_of_experience"]
        if options:
            return Answer(choice(next((o for o in options if re.search(rf"\b{years}\b", o)), None) or pick(options, str(years))), "rule")
        return Answer(str(years), "rule")
    if re.search(r"(earliest|soonest|available|availability|when can you|when could you).{0,30}start|start date|date (you are|you're) available", q) and not options:
        return Answer(p["available_start"], "rule")
    if re.search(r"degree (received|completed|conferred|awarded|obtained)|received (your|the) degree", q):
        # User's choice (2026-10-04): always Yes.
        return Answer(yes_no(options, True) if options else "Yes", "rule")
    if re.search(r"clearance", q):
        # User's choice (2026-10-04): no clearance; answer No / None / N/A.
        if options:
            return Answer(choice(pick(options, "no", "none", "n/a", "not applicable", "do not")), "rule")
        return Answer("None", "rule")
    if re.search(r"smok|tobacco|nicotine|vap(e|ing)", q):
        habit = p["standing_answers"].get("smoker")
        if habit is None:
            return Answer(None, "ask", "needs your answer: personal question")
        return Answer(yes_no(options, habit) if options else ("Yes" if habit else "No"), "rule")
    if re.search(r"alcohol|do you drink|drinker", q):
        habit = p["standing_answers"].get("drinks")
        if habit is None:
            return Answer(None, "ask", "needs your answer: personal question")
        return Answer(yes_no(options, habit) if options else ("Yes" if habit else "No"), "rule")
    if re.search(r"highest (level of )?(education|degree)", q):
        completed = p["highest_education_completed"]
        level = completed.split("'")[0].split()[0]  # "Bachelor"
        if options:
            return Answer(choice(pick(options, completed, level, level + "s")), "rule")
        return Answer(completed, "rule")

    if re.search(r"non-?\s?compete", q):
        return Answer(yes_no(options, p["standing_answers"]["non_compete"]), "rule")
    if re.search(r"\bcertify\b|arbitration|by agreeing|^i (accept|agree|consent|acknowledge)\b", q) and options:
        return Answer(yes_no(options, p["standing_answers"]["agree_to_certifications"]), "rule")

    demo = p["demographics"]
    if "identify your sex" in q or q.startswith("gender") or "your gender" in q:
        return Answer(choice(pick(options, demo["sex"])), "rule")
    if "sexual orientation" in q and options and demo.get("sexual_orientation"):
        # Lists say "Heterosexual" or "Straight" for the same answer.
        same = {"heterosexual": ["Straight"], "straight": ["Heterosexual"]}.get(demo["sexual_orientation"].lower(), [])
        found = pick(options, demo["sexual_orientation"], *same)
        if found:
            return Answer(choice(found), "rule")
    if re.search(r"hispanic or latino\??$", q) and options and pick(options, "no"):
        hispanic = "hispanic" in demo["ethnicity"].lower() and not demo["ethnicity"].lower().startswith("not")
        return Answer(choice(yes_no(options, hispanic)), "rule")
    if re.search(r"\brac(e|ial)\b", q) and options:
        found = pick(options, demo.get("race_detail") or demo["race"], demo["race"])
        if found:
            return Answer(choice(found), "rule")
    if "ethnicity" in q and options:
        # Some "Ethnicity" lists are really race lists (SuccessFactors: "South Asian (e.g. Indian)").
        found = pick(options, demo["ethnicity"]) or pick(options, demo.get("race_detail") or demo["race"], demo["race"])
        return Answer(choice(found), "rule")
    if "veteran" in q and options:
        wanted = ("i am not a veteran", "i am not a protected veteran", "not a protected veteran", "no")
        return Answer(choice(pick(options, *wanted)) if not demo["veteran"] else None, "rule")
    if "disability" in q and options:
        return Answer(choice(pick(options, "no, i do not", "no")) if not demo["disability"] else None, "rule")

    # Applications go in directly, so nobody referred you.
    if re.search(r"\b(were you|have you been) referred|referred (by|to this)|employee referral|referral (from|by)|who referred you", q):
        return Answer(yes_no(options, False) if options else "No", "rule")
    # Criminal history is a personal fact; never guessed.
    if re.search(r"convicted|felony|misdemeanor|criminal (record|history|offen[cs]e)|plea of .?guilty|no contest", q):
        record = p["standing_answers"].get("criminal_record")
        if record is None:
            return Answer(None, "ask", "needs your answer: criminal history question")
        return Answer(yes_no(options, record) if options else ("Yes" if record else "No"), "rule")
    # Follow-ups that only apply when the previous answer was yes.
    if re.match(r"(if (yes|so)\b|if you answered (yes|\"yes\")|please (provide|explain|describe|list).{0,80}\bif (yes|so|you answered yes)\b)", q) and not options:
        return Answer(None, "skip", "only needed if the answer above was yes")

    # A bare box label ("From", "Company", "Job Title") belongs to a form section about
    # your own history; on its own it says nothing the AI can answer safely.
    if not options and re.fullmatch(BARE_LABEL, q):
        return Answer(None, "ask" if required else "skip", "a box about your own history that could not be matched to your profile")
    # The company asks for answers not written with AI, so they are yours to write.
    if not options and re.search(r"\bno ai\b|not (be )?(written|generated) (by|with|using) ai|without (the )?(use of |help of )?(ai|chatgpt|llms?)\b|\bai[- ]generated|don'?t use (ai|chatgpt)", q):
        return Answer(None, "ask" if required else "skip", "the company asks for answers not written by AI")
    answer = ask_llm(label, options, company, context, essay=is_essay(label, field_type, options))
    # An optional question the facts don't settle is left blank rather than asked.
    if answer.source == "ask" and not required:
        return Answer(None, "skip", f"optional, left blank ({answer.note})")
    return answer


BARE_LABEL = r"(from|to|start( date)?|end( date)?|company( name)?|employer( name)?|job title|title|position( title)?|role( description)?|description|school( name)?|degree|field of study|major|gpa|grade average|dates?|duration|month|year)\??"
US_CITIZEN = r"\b(u\.?\s?s\.?|united states|american)\s+citizen(ship)?\b|citizen(ship)? of the (u\.?s\.?|united states)|\bu\.?s\.? person\b"
LONG_TYPES = ("textarea", "LongText")
ESSAY_WORDS = re.compile(r"\b(why|describe|tell us|tell me|explain|share|walk us through|what (excites|interests|draws|motivates)|how (would|did|do) you|essay|cover letter)\b")


def length_limits(label: str) -> tuple[int | None, int | None, str]:
    """(least, most, "words" or "characters") the question asks for, where it says."""
    q = label.lower().replace(",", "")
    unit = r"(words?|characters?|chars?)"
    if m := re.search(rf"(\d{{2,5}})\s*(?:-|–|to)\s*(\d{{2,5}})\s*{unit}", q):
        return int(m.group(1)), int(m.group(2)), "words" if m.group(3).startswith("w") else "characters"
    if m := re.search(rf"(?:max(?:imum)?|up to|no more than|not exceed(?:ing)?|under|less than|fewer than|limit(?:ed)? to|at most|within)\s*(?:of\s*)?(\d{{2,5}})\s*{unit}", q):
        return None, int(m.group(1)), "words" if m.group(2).startswith("w") else "characters"
    if m := re.search(rf"(\d{{2,5}})\s*{unit}\s*(?:or (?:less|fewer)|max(?:imum)?|limit)", q):
        return None, int(m.group(1)), "words" if m.group(2).startswith("w") else "characters"
    if m := re.search(rf"(?:at least|minimum(?: of)?)\s*(\d{{2,4}})\s*{unit}", q):
        return int(m.group(1)), None, "words" if m.group(2).startswith("w") else "characters"
    if m := re.search(r"(?:in|about|around|approximately)\s*(\d{2,4})\s*words", q):
        return int(int(m.group(1)) * 0.8), int(m.group(1)), "words"
    return None, None, "words"


def is_essay(label: str, field_type: str, options: list[str]) -> bool:
    """A written answer that deserves a proper paragraph or more, not a one-liner."""
    if options:
        return False
    least, most, _ = length_limits(label)
    if least or (most and most >= 50):
        return True
    return field_type in LONG_TYPES and (len(label) > 60 or bool(ESSAY_WORDS.search(label.lower())))


def measure(text: str, unit: str) -> int:
    return len(text.split()) if unit == "words" else len(text)


def tidy(text: str) -> str:
    """The writing rules, enforced: no em or en dashes, no markdown bold."""
    text = re.sub(r"\s*—\s*", ", ", text)
    text = re.sub(r"(\d)\s*–\s*(\d)", r"\1-\2", text)
    text = re.sub(r"\s*–\s*", ", ", text)
    return text.replace("**", "").strip()


def essay_rule(label: str) -> str:
    least, most, unit = length_limits(label)
    if most:
        # Aim a little under the limit, because sites count differently.
        target = f"between {least} and {int(most * 0.9)} {unit}" if least else f"no more than {int(most * 0.9)} {unit}"
    elif least:
        target = f"at least {least} {unit}, and not much more than {int(least * 1.4)}"
    elif re.search(r"\b(briefly|brief|short|a few sentences|one sentence|1-2 sentences|two sentences)\b", label.lower()):
        target = "two to four sentences"
    else:
        target = "about 120 to 200 words"
    return f"""This is a written answer, so write it properly.
- Length: {target}. Count carefully.
- Answer every part of the question, in the order it asks. If it asks for an example, give one real example from the RESUME or EXTRA MATERIAL with what the applicant did and the real result or number.
- Open with a direct answer, not a restatement of the question. Do not start with "I am writing" or "As a".
- Tie it to this job: name something specific from the JOB DESCRIPTION (the product, the team's problem, a tool or a responsibility) and connect it to a specific fact about the applicant.
- Use one to three short paragraphs separated by a blank line. No lists, no headings, no sign-off.
- End on a plain, concrete sentence about the work, not a slogan or a summary of adjectives."""


# "Describe your hands-on experience with AI/ML, big data and AWS", "Tell us about your
# background in ...": answered in the shape of the user's own sample (2026-10-06).
EXPERIENCE_QUESTION = re.compile(r"\b(describe|tell (us|me) about|walk (us|me) through|share|summari[sz]e|elaborate on|explain)\b.{0,60}\b(experience|background|exposure|familiarity|expertise|work with)\b", re.I)


def experience_rule(label: str) -> str:
    least, most, unit = length_limits(label)
    if most:
        target = f"between {least} and {int(most * 0.9)} {unit}" if least else f"no more than {int(most * 0.9)} {unit}"
    elif least:
        target = f"at least {least} {unit}, and not much more than {int(least * 1.4)}"
    else:
        target = "about 180 to 320 words"
    sample = experience_answer_sample()
    return f"""This question asks about the applicant's experience. Answer it the way the SAMPLE below does.
- Length: {target}. Count carefully.
- Start with one sentence that sums up what the applicant builds, written for the areas this question names.
- Then one short paragraph for each area the question names, in the order it names them. Start each paragraph with the area's name and a colon (for example "AI/ML:" or "AWS and cloud:"). Areas that belong together may share a paragraph ("Big data and data pipelines:").
- In each paragraph give the applicant's real projects for that area: where, what they built, and the real numbers, taken from the RESUME and EXTRA MATERIAL. Every fact must come from those; do not copy the SAMPLE's facts into an area they don't fit.
- Confident and positive throughout. Never mention a gap, never say "I don't have" or "limited", and never apologise. For an area the facts cover only partly, describe the closest real work and the tools the applicant uses.
- No lists, no headings beyond the "Area:" openings, no sign-off.

SAMPLE (a real answer by the applicant to "Can you describe your hands-on experience with AI/ML, big data, data pipelines, and AWS?"):
{sample}"""


def ask_llm(label: str, options: list[str], company: str, context: str = "", model: str | None = None, essay: bool = False) -> Answer:
    """Drafts an answer. Anything the model is unsure of is left blank for the user."""
    p = load_profile()
    if options:
        choice_rule = f"You must answer with exactly one of these options, copied character for character: {json.dumps(options)}"
    elif EXPERIENCE_QUESTION.search(label):
        choice_rule = experience_rule(label)
        essay = True
    elif essay:
        choice_rule = essay_rule(label)
    else:
        choice_rule = "Answer in plain text. Keep short factual answers short. For open questions write two to four full sentences, unless the question asks for a different length."
    # The applicant's own notes for the AI (profile.yaml, ai_context).
    ai = p.get("ai_context") or {}
    work_note = f" {ai['work_authorization']}" if ai.get("work_authorization") else ""
    answer_notes = "".join(f"\n- {note}" for note in ai.get("answer_notes") or [])
    prompt = f"""You are filling out a job application for {company} on behalf of the applicant, writing in the first person as the applicant.

Rules for facts:
- You are applying TO {company}. Questions about the applicant's own past (their job titles, employers, company names, dates, schools, what they did) are answered only from the PROFILE and RESUME. Never answer them with {company}, with the role being applied for, or with anything from the JOB DESCRIPTION.
- A bare box label with no question (for example "From", "To", "Company", "Title") cannot be answered safely: set "confident" to false. Never put names, job sources or sentences into a date box.
- The PROFILE is the final word. Never contradict it with your own reasoning.{work_note}
- Use only facts from the PROFILE, RESUME and EXTRA MATERIAL. Never invent experiences, numbers, sources or opinions.
- If the facts do not settle the question (for example how the applicant heard about the job, or accepting a salary), set "confident" to false.
- Never guess personal facts: habits, health, family, criminal history, relatives at the company, or anything else about the applicant's private life that the facts do not state. Set "confident" to false for these.
- Yes/No and pick-an-option questions only: never infer experience. If such a question asks about a specific tool, practice, kind of work or number of years that the facts do not name directly, the answer is the "No" option or the lowest amount offered, with "confident" true. Having a related skill does not count; for example SQL is not dbt, and analytics work is not an event tracking spec.

Rules for open questions:
- Answer the exact question asked. Pick the one or two examples that fit it best, with their real numbers.{answer_notes}
- Always write about the applicant positively and confidently. Never point out a gap or a weakness: no "I don't have", "I haven't", "limited", "not much", "although", "while I lack", "I am still learning", "my experience is mostly academic". When the question names an area the facts cover only partly, describe the applicant's real work closest to it and what it shows, without claiming anything the facts don't state and without saying what is missing. This never makes an open question uncertain: write the answer and set "confident" to true.
- For "why this company" questions, name something specific from the JOB DESCRIPTION and connect it to a specific fact about the applicant.

Writing rules: simple, polite, human-sounding English in full sentences. No em dashes, no en dashes, no bold, no markdown, no short punchline sentences. Never use these words or phrases: passionate, deeply, resonates, impactful, meaningful change, innovative, leverage, eager, keen, thrilled, excited to, robust, cutting-edge, drive change, make a difference.

Question: {label}
{choice_rule}

Reply with JSON only: {{"answer": "...", "confident": true or false, "reason": "one short sentence"}}

JOB DESCRIPTION:
{context or "(not available)"}

PROFILE (YAML):
{json.dumps(p, default=str)}

RESUME:
{resume_text()}

EXTRA MATERIAL:
{content_text()}"""
    messages = [{"role": "user", "content": prompt}]
    raw = llm.chat(messages, temperature=0.2 if not essay else 0.4, max_tokens=4000, model=model or (None if options else llm.WRITING_MODEL))
    match = re.search(r"\{.*\}", raw, re.S)
    if not match:
        return Answer(None, "llm", f"could not parse model reply: {raw[:200]}")
    data = json.loads(match.group(0))
    answer = data.get("answer")
    if essay and isinstance(answer, str) and data.get("confident"):
        answer = fit_length(answer, label, messages + [{"role": "assistant", "content": raw}], model)
    if isinstance(answer, str) and not options:
        answer = tidy(answer)
    # A short answer that is just the hiring company's name, to a question that is not
    # about that company, is the model mixing up whose history is asked for.
    core = re.sub(r",?\s+(inc|llc|ltd|corp|corporation|co|company)\.?$", "", company.lower().strip())
    if isinstance(answer, str) and core and len(answer) < 80 and core in answer.lower() and core not in label.lower():
        return Answer(None, "ask", f"needs your answer (the AI's answer named {company}, which this question doesn't ask about)")
    if options and answer not in options:
        answer = pick(options, str(answer))
    if not data.get("confident"):
        return Answer(None, "ask", f"needs your answer (the AI suggested {answer!r}: {data.get('reason', '')})")
    return Answer(answer, "llm", data.get("reason", ""))


def fit_length(answer: str, label: str, messages: list[dict], model: str | None) -> str:
    """One rewrite when an essay misses the length the question asks for; a character
    limit that is still exceeded after that is cut at the last full sentence."""
    least, most, unit = length_limits(label)
    size = measure(answer, unit)
    if (most and size > most * 0.95) or (least and size < least):
        wanted = f"between {least} and {int(most * 0.9)}" if least and most else f"at most {int(most * 0.9)}" if most else f"at least {least}"
        ask = f"Your answer has {size} {unit}. The question needs {wanted} {unit}. Rewrite it to fit, keeping the same facts and rules. Reply with the same JSON shape."
        raw = llm.chat(messages + [{"role": "user", "content": ask}], temperature=0.3, max_tokens=4000, model=model or llm.WRITING_MODEL)
        if match := re.search(r"\{.*\}", raw, re.S):
            try:
                answer = json.loads(match.group(0)).get("answer") or answer
            except json.JSONDecodeError:
                pass
    if most and unit == "characters" and len(answer) > most:
        cut = answer[:most]
        answer = cut[: cut.rfind(".") + 1] if "." in cut else cut
    return answer

import os
import re
import json
import time
import sqlite3
from datetime import date

import requests
from dotenv import load_dotenv
from google import genai
from google.genai import types, errors
from pydantic import BaseModel, Field

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
with open("config.json", "r") as f:
    CONFIG = json.load(f)

PROFILE = CONFIG.get("candidate_profile", {})
CRITERIA = CONFIG.get("search_and_matching_criteria", {})
SETTINGS = CONFIG.get("pipeline_settings", {})  # optional block; defaults below

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ADZUNA_APP_ID = os.getenv("ADZUNA_APP_ID")
ADZUNA_APP_KEY = os.getenv("ADZUNA_APP_KEY")

MODEL_NAME = SETTINGS.get("gemini_model", "gemini-3.5-flash-lite")
MIN_FIT_SCORE = CRITERIA.get("minimum_fit_score_to_save", 75)
MAX_PAGES = SETTINGS.get("max_pages_per_search", 2)
MAX_DAYS_OLD = SETTINGS.get("max_days_old", 14)
# Free tier for Flash-Lite is ~15 requests/min; 4.5s keeps us safely under.
SECONDS_BETWEEN_CALLS = SETTINGS.get("seconds_between_gemini_calls", 4.5)
# Caps Gemini usage per run so one run can't burn the whole daily quota.
MAX_EVALS_PER_RUN = SETTINGS.get("max_evaluations_per_run", 150)

DB_PATH = "matches.db"
RESULTS_PER_PAGE = 50

gemini_client = genai.Client(api_key=GEMINI_API_KEY)

# Adzuna: `what` words are ANDed, `what_or` words are ORed.
KEYWORDS_OR = "software data analytics analyst python developer statistics quantitative"
SEARCH_PLANS = [
    {"label": "Michigan", "what": "intern", "where": "Michigan"},
    {"label": "Colorado", "what": "intern", "where": "Colorado"},
    {"label": "Remote", "what": "intern remote", "where": None},
]


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def init_db(conn: sqlite3.Connection) -> None:
    """Idempotent: safe to run every time. job_postings matches your init script."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS job_postings (
            job_id TEXT PRIMARY KEY,
            title TEXT,
            company TEXT,
            location TEXT,
            term TEXT,
            url TEXT UNIQUE,
            fit_score INTEGER,
            matched_major TEXT,
            key_strengths TEXT,
            date_logged TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'Unreviewed'
        )
    """)
    # Every job the pipeline has processed, match or not, so nothing is
    # sent to Gemini twice.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_jobs (
            job_id TEXT PRIMARY KEY,
            title TEXT,
            company TEXT,
            outcome TEXT NOT NULL,      -- 'prefiltered_out' | 'evaluated'
            is_match INTEGER,
            fit_score INTEGER,
            reasoning TEXT,
            description TEXT,
            first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(seen_jobs)")}
    if "url" not in cols:
        conn.execute("ALTER TABLE seen_jobs ADD COLUMN url TEXT")
    if "description" not in cols:
        conn.execute("ALTER TABLE seen_jobs ADD COLUMN description TEXT")
    if "location" not in cols:
        conn.execute("ALTER TABLE seen_jobs ADD COLUMN location TEXT" )
    if "model" not in cols:
        conn.execute("ALTER TABLE seen_jobs ADD COLUMN model TEXT") 
    conn.commit()


def mark_seen(conn, job_id, title, company, location, outcome, url, description, model, ev=None) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO seen_jobs
   (job_id, title, company, location, outcome, url, description, model, is_match, fit_score, reasoning)
   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            job_id, title, company, location, outcome, url, description, model,
            int(ev.is_match) if ev else None,
            ev.fit_score if ev else None,
            ev.reasoning if ev else None,
        ),
    )


def save_match(conn, job_id, title, company, location, url, ev) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO job_postings
           (job_id, title, company, location, term, url, fit_score, matched_major, key_strengths)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            job_id, title, company, location, ev.term,
            url or None,  # NULL rather than '' so the UNIQUE constraint can't collide
            ev.fit_score, ev.matched_major, ", ".join(ev.key_strengths),
        ),
    )


# ---------------------------------------------------------------------------
# Adzuna
# ---------------------------------------------------------------------------
def fetch_adzuna_page(plan: dict, page: int) -> list[dict]:
    url = f"https://api.adzuna.com/v1/api/jobs/us/search/{page}"
    params = {
        "app_id": ADZUNA_APP_ID,
        "app_key": ADZUNA_APP_KEY,
        "what": plan["what"],
        "what_or": KEYWORDS_OR,
        "results_per_page": RESULTS_PER_PAGE,
        "max_days_old": MAX_DAYS_OLD,
        "content-type": "application/json",
    }
    if plan["where"]:
        params["where"] = plan["where"]
    res = requests.get(url, params=params, timeout=12)
    res.raise_for_status()
    return res.json().get("results", [])


def fetch_all(plan: dict) -> list[dict]:
    jobs = []
    for page in range(1, MAX_PAGES + 1):
        try:
            results = fetch_adzuna_page(plan, page)
        except requests.HTTPError as e:
            # Don't print the URL: it contains the API key.
            code = e.response.status_code if e.response is not None else "?"
            hint = " (check ADZUNA_APP_ID / ADZUNA_APP_KEY in .env)" if code in (401, 403) else ""
            print(f" Adzuna returned HTTP {code} ({plan['label']}, page {page}){hint}")
            break
        except requests.RequestException as e:
            print(f" Adzuna request failed ({plan['label']}, page {page}): {type(e).__name__}")
            break
        print(f"From Adzuna | {plan['label']} | page {page} | {len(results)} jobs")
        jobs.extend(results)
        if len(results) < RESULTS_PER_PAGE:
            break
    return jobs


# ---------------------------------------------------------------------------
# Local prefilter (free)
# ---------------------------------------------------------------------------
INTERN_RE = re.compile(r"\b(intern|interns|internship|co-?op)\b", re.I)
SENIOR_RE = re.compile(r"\b(senior|sr\.?|principal|staff|director|lead|head of)\b", re.I)
DOMAIN_RE = re.compile(
    r"\b(software|data|analytics?|analyst|machine learning|ml|ai|python|"
    r"statistics?|statistical|quant|quantitative|developer|engineer|engineering|"
    r"math|mathematics|computer science)\b",
    re.I,
)


def prefilter_job(title: str, description: str) -> bool:
    """Skip obvious non-fits before spending a Gemini call."""
    if not DOMAIN_RE.search(f"{title} {description}"):
        return False                      # e.g. marketing / HR internships
    if INTERN_RE.search(title):
        return True                       # "Product Manager Intern" passes
    return bool(INTERN_RE.search(description)) and not SENIOR_RE.search(title)


# ---------------------------------------------------------------------------
# Gemini evaluation
# ---------------------------------------------------------------------------
class JobEvaluation(BaseModel):
    is_match: bool = Field(description="False only if a hard requirement is clearly violated")
    fit_score: int = Field(description="Weighted score between 0 and 100")
    matched_major: str = Field(description="One acceptable major the posting accepts, 'Not specified' if it names none, or 'None' if it names majors and none match")
    term: str = Field(description="Term identified, e.g. 'Summer 2027', 'Fall 2027 Co-op', 'Unspecified', or 'Other'")
    key_strengths: list[str] = Field(description="Candidate skills that are relevant to this posting")
    reasoning: str = Field(description="Brief explanation, including any requirements that couldn't be verified")


def build_evaluation_rules() -> str:
    edu = PROFILE.get("education", {})
    skills = PROFILE.get("skills", {})
    auth = PROFILE.get("work_authorization", {})
    elig = CRITERIA.get("eligibility_rules", {})
    terms = CRITERIA.get("target_terms", {})
    loc = CRITERIA.get("location_constraints", {})
    weights = CRITERIA.get("scoring_weights", {})

    grad = elig.get("graduation_window", {}).get("candidate_grad_date", edu.get("expected_graduation", ""))
    majors = elig.get("acceptable_majors", edu.get("majors", []))
    states = loc.get("allowed_states", [])

    auth_bits = []
    if auth.get("us_citizen_or_permanent_resident"):
        auth_bits.append("US citizen")
    if not auth.get("requires_sponsorship", True):
        auth_bits.append("no sponsorship needed")
    if auth.get("security_clearance_eligible"):
        auth_bits.append("eligible for security clearance")

    location_rule = f"The role must be located in {' or '.join(states)} (on-site or hybrid)"
    if loc.get("allow_remote"):
        location_rule += ", or be fully remote within the United States"
    location_rule += ". On-site or hybrid roles anywhere else are NOT a match."

    hard_rules = [
        f"Location: {location_rule}",
        f"Term: primary target {', '.join(terms.get('primary', []))}; also acceptable: "
        f"{'; '.join(terms.get('secondary_acceptable', []))}. Terms that have already started or ended "
        f"(e.g. Summer 2026, Fall 2026) are NOT a match.",
        f"Degree level: {', '.join(elig.get('degree_level', []))}. Roles requiring a graduate degree "
        f"or current graduate enrollment are NOT a match.",
    ]
    if elig.get("require_major_match"):
        hard_rules.append(
            f"Major: if the posting names required majors, at least one must be among: {', '.join(majors)}."
        )
    if elig.get("graduation_window", {}).get("reject_if_requires_earlier_grad"):
        hard_rules.append(f"Graduation: NOT a match if the posting requires graduating before {grad}.")

    sponsorship = elig.get("sponsorship_filter", {})
    ok_restrictions = []
    if sponsorship.get("allow_citizenship_required_roles"):
        ok_restrictions.append("US citizenship")
    if sponsorship.get("allow_clearance_required_roles"):
        ok_restrictions.append("security clearance")

    rules_text = "\n".join(f"{i}. {r}" for i, r in enumerate(hard_rules, 1))
    weights_text = "\n".join(f"- {k.replace('_', ' ')}: {int(v * 100)}%" for k, v in weights.items())

    return f"""You are screening internship postings for a candidate. Today's date is {date.today():%B %d, %Y}.

CANDIDATE
- {edu.get('degree_type', '')} student at {edu.get('institution', '')} ({edu.get('program', '')}), GPA {edu.get('gpa', '')}
- Majors: {', '.join(edu.get('majors', []))}
- Expected graduation: {grad}. In Summer 2027 the candidate will be a rising senior.
- Work authorization: {', '.join(auth_bits)}. Roles requiring {' or '.join(ok_restrictions) or 'citizenship'} are acceptable.
- Languages: {', '.join(skills.get('programming_languages', []))}
- Libraries/frameworks: {', '.join(skills.get('frameworks_and_libraries', []))}
- Domains: {', '.join(skills.get('domains', []))}
- Tools: {', '.join(skills.get('tools', []))}
- Target roles: {', '.join(CRITERIA.get('target_roles', []))}

HARD REQUIREMENTS (set is_match = false only if one is CLEARLY violated)
{rules_text}

MISSING INFORMATION
The description is a truncated snippet of roughly 500 characters. Do not reject a posting just because
a requirement isn't mentioned. If the term isn't stated, use "Unspecified". Note anything you couldn't
verify in the reasoning, and let uncertainty lower the relevant score component instead.

SCORING
Compute fit_score (0-100) using these weights:
{weights_text}
"""


EVALUATION_RULES = build_evaluation_rules()


def evaluate_job(title: str, company: str, location: str, description: str) -> JobEvaluation:
    prompt = f"""{EVALUATION_RULES}
POSTING
Title: {title}
Company: {company}
Listed location: {location}
Description snippet: {description}
"""
    response = gemini_client.models.generate_content(
        model=MODEL_NAME,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=JobEvaluation,
            temperature=0.1,
        ),
    )
    result = response.parsed
    if result is None:
        raise ValueError("Gemini returned an empty or unparseable response")
    result.fit_score = max(0, min(100, result.fit_score))
    return result


class QuotaExhausted(Exception):
    pass


RETRYABLE_CODES = {429, 500, 502, 503, 504}


def evaluate_with_retry(*args, attempts: int = 4) -> JobEvaluation:
    for attempt in range(attempts):
        try:
            return evaluate_job(*args)
        except errors.APIError as e:
            code = getattr(e, "code", None)
            if code not in RETRYABLE_CODES:
                raise
            if attempt == attempts - 1:
                # Per-minute limits reset well within the ~70s of backoff, so a
                # 429 that survives every retry means the daily quota is gone.
                if code == 429:
                    raise QuotaExhausted(str(e)) from e
                raise
            wait = 10 * 2 ** attempt
            print(f"   Time Gemini returned {code}; retrying in {wait}s...")
            time.sleep(wait)


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------
def run() -> None:
    if not all([GEMINI_API_KEY, ADZUNA_APP_ID, ADZUNA_APP_KEY]):
        raise SystemExit("Missing GEMINI_API_KEY, ADZUNA_APP_ID or ADZUNA_APP_KEY in .env")

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    stats = dict.fromkeys(
        ["fetched", "duplicates", "already_seen", "prefiltered_out",
         "evaluated", "matches", "errors", "deferred"], 0)

    print(" Starting ingestion...")

    # 1. Fetch every search and dedupe (remote jobs often appear in state searches too)
    jobs: dict[str, dict] = {}
    for plan in SEARCH_PLANS:
        for job in fetch_all(plan):
            stats["fetched"] += 1
            job_id = str(job.get("id"))
            if job_id in jobs:
                stats["duplicates"] += 1
                continue
            jobs[job_id] = job

    seen = {row[0] for row in conn.execute("SELECT job_id FROM seen_jobs")}
    last_call = 0.0
    quota_hit = False

    try:
        for job_id, job in jobs.items():
            if job_id in seen:
                stats["already_seen"] += 1
                continue

            title = job.get("title", "")
            company = job.get("company", {}).get("display_name", "Unknown")
            description = job.get("description", "")
            url = job.get("redirect_url", "")
            location_name = job.get("location", {}).get("display_name", "Unknown")

            # 2. Free local filter
            if not prefilter_job(title, description):
                mark_seen(conn, job_id, title, company, location_name,  "prefiltered_out",  url, description, None)
                stats["prefiltered_out"] += 1
                continue

            # 3. Respect quota and per-run cap; deferred jobs stay unseen for next run
            if quota_hit or stats["evaluated"] + stats["errors"] >= MAX_EVALS_PER_RUN:
                stats["deferred"] += 1
                continue

            wait = SECONDS_BETWEEN_CALLS - (time.monotonic() - last_call)
            if wait > 0:
                time.sleep(wait)
            last_call = time.monotonic()

            # 4. LLM scoring
            try:
                ev = evaluate_with_retry(title, company, location_name, description)
            except QuotaExhausted:
                print("Stop Gemini daily quota exhausted; remaining jobs will be evaluated next run.")
                quota_hit = True
                stats["deferred"] += 1
                continue
            except Exception as e:
                print(f"   Warning Evaluation error on {job_id} ({title}): {e}")
                stats["errors"] += 1  # not marked seen, so it's retried next run
                continue

            stats["evaluated"] += 1
            mark_seen(conn, job_id, title, company, location_name,"evaluated",   url,description, MODEL_NAME, ev)
            if ev.is_match and ev.fit_score >= MIN_FIT_SCORE:
                save_match(conn, job_id, title, company, location_name, url, ev)
                stats["matches"] += 1
                print(f"   MATCH ({ev.fit_score}/100): {title} @ {company} [{location_name}]")
            conn.commit()
    finally:
        conn.commit()
        conn.close()

    print("\n Run finished")
    for key, value in stats.items():
        print(f"   {key.replace('_', ' '):<16} {value}")


if __name__ == "__main__":
    run()
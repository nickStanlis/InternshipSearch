import os
import re
import json
import threading
import time
from dataclasses import dataclass, asdict
from datetime import date
from typing import Callable
import db
import hashlib
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



RESULTS_PER_PAGE = 50


def gemini_client() -> genai.Client:
    """Created on first use, so importing this module needs no API key (e.g. in tests)."""
    global _gemini_client
    if _gemini_client is None:
        _gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    return _gemini_client

# Adzuna: `what` words are ANDed, `what_or` words are ORed.
KEYWORDS_OR = SETTINGS.get("search_keywords_or", "software data analytics analyst python developer statistics quantitative")
SEARCH_PLANS = SETTINGS.get("search_plans", [])
if not SEARCH_PLANS:
    raise SystemExit("No search_plans found under pipeline_settings in config.json")


# ---------------------------------------------------------------------------
# Progress reporting and run results
# ---------------------------------------------------------------------------
@dataclass
class ProgressEvent:
    kind: str  # "stage", "fetched", "scored", "match", "retry", "warning", "info"
    message: str  # human-readable; the CLI prints it, the UI logs it
    done: int | None = None  # for progress bars, e.g. 12 of 40 scored
    total: int | None = None


ProgressCallback = Callable[[ProgressEvent], None]


@dataclass
class RunSummary:
    run_id: int | None = None
    status: str = "completed"  # "completed" | "stopped" | "failed"
    fetched: int = 0
    duplicates: int = 0
    already_seen: int = 0
    prefiltered_out: int = 0
    evaluated: int = 0
    matches: int = 0
    errors: int = 0
    deferred: int = 0

    def counts(self) -> dict:
        """Just the numbers, for db.finish_run and the end-of-run printout."""
        d = asdict(self)
        del d["run_id"], d["status"]
        return d


class Stopped(Exception):
    """Raised when the stop event is set during a wait."""


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


def fetch_plan(plan: dict, emit: ProgressCallback, stop: threading.Event) -> list[dict]:
    """Every page of one search plan."""
    jobs = []
    for page in range(1, MAX_PAGES + 1):
        if stop.is_set():
            break
        try:
            results = fetch_adzuna_page(plan, page)
        except requests.HTTPError as e:
            # Don't show the URL: it contains the API key.
            code = e.response.status_code if e.response is not None else "?"
            hint = " (check ADZUNA_APP_ID / ADZUNA_APP_KEY in .env)" if code in (401, 403) else ""
            emit(ProgressEvent("warning", f" Adzuna returned HTTP {code} ({plan['label']}, page {page}){hint}"))
            break
        except requests.RequestException as e:
            emit(ProgressEvent("warning", f" Adzuna request failed ({plan['label']}, page {page}): {type(e).__name__}"))
            break
        emit(ProgressEvent("fetched", f"From Adzuna | {plan['label']} | page {page} | {len(results)} jobs"))
        jobs.extend(results)
        if len(results) < RESULTS_PER_PAGE:
            break
    return jobs


def fetch_jobs(plans: list[dict], emit: ProgressCallback, stop: threading.Event) -> list[dict]:
    """Raw Adzuna results for every search plan, duplicates included."""
    jobs = []
    for plan in plans:
        jobs.extend(fetch_plan(plan, emit, stop))
    return jobs


def to_posting(job: dict) -> db.NewPosting:
    """Adzuna JSON -> NewPosting. The only place that knows Adzuna's field names."""
    return db.NewPosting(
        job_id=str(job.get("id")),
        title=job.get("title", ""),
        company=job.get("company", {}).get("display_name", "Unknown"),
        location=job.get("location", {}).get("display_name", "Unknown"),
        url=job.get("redirect_url", ""),
        description=job.get("description", ""),
    )


def dedupe(jobs: list[dict]) -> tuple[list[db.NewPosting], int]:
    """Unique postings (first occurrence wins) and how many duplicates were dropped.
    Remote jobs often show up in the state searches too."""
    unique: dict[str, db.NewPosting] = {}
    for job in jobs:
        posting = to_posting(job)
        unique.setdefault(posting.job_id, posting)
    return list(unique.values()), len(jobs) - len(unique)


def drop_seen(postings: list[db.NewPosting], seen_ids: set[str]) -> tuple[list[db.NewPosting], int]:
    """Postings not processed in an earlier run, and how many were skipped."""
    new = [p for p in postings if p.job_id not in seen_ids]
    return new, len(postings) - len(new)


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


def split_prefilter(postings: list[db.NewPosting]) -> tuple[list[db.NewPosting], list[db.NewPosting]]:
    """(keep, rejected) according to prefilter_job."""
    keep, rejected = [], []
    for p in postings:
        (keep if prefilter_job(p.title, p.description) else rejected).append(p)
    return keep, rejected


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
PROMPT_VERSION = hashlib.sha256(EVALUATION_RULES.encode()).hexdigest()[:12]

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



def evaluate_with_retry(posting: db.NewPosting, emit: ProgressCallback,
                        stop: threading.Event, attempts: int = 4) -> JobEvaluation:
    for attempt in range(attempts):
        try:
            return evaluate_job(posting.title, posting.company, posting.location, posting.description)
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
            if stop.wait(wait):  # interruptible sleep: returns True as soon as Stop is pressed
                raise Stopped()


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------
Evaluator = Callable[[db.NewPosting, ProgressCallback, threading.Event], JobEvaluation]



def score_and_save(conn, postings: list[db.NewPosting], summary: RunSummary,
                   emit: ProgressCallback, stop: threading.Event,
                   evaluate: Evaluator = evaluate_with_retry) -> None:
    """Score postings with Gemini under the rate limit, per-run cap and daily quota,
    and record each result. Postings that aren't scored stay unseen for next run.

    `evaluate` is a parameter so tests can pass a fake instead of calling Gemini.
    """
    to_score = min(len(postings), MAX_EVALS_PER_RUN)
    last_call = 0.0

    for i, posting in enumerate(postings):
        if summary.evaluated + summary.errors >= MAX_EVALS_PER_RUN:
            summary.deferred += len(postings) - i
            emit(ProgressEvent("info", f"Per-run cap of {MAX_EVALS_PER_RUN} reached; "
                                       f"{len(postings) - i} postings deferred to next run."))
            return

        wait = SECONDS_BETWEEN_CALLS - (time.monotonic() - last_call)
        if stop.is_set() or (wait > 0 and stop.wait(wait)):
            summary.status = "stopped"
            summary.deferred += len(postings) - i
            return
        last_call = time.monotonic()

        try:
            ev = evaluate(posting, emit, stop)
        except Stopped:
            summary.status = "stopped"
            summary.deferred += len(postings) - i
            return
        except QuotaExhausted:
            summary.deferred += len(postings) - i
            emit(ProgressEvent("warning", "Gemini daily quota exhausted; "
                                          "remaining jobs will be evaluated next run."))
            return
        except Exception as e:
            summary.errors += 1  # not marked seen, so it's retried next run
            emit(ProgressEvent("warning", f"   Evaluation error on {posting.job_id} ({posting.title}): {e}"))
            continue

        is_strong_match = ev.is_match and ev.fit_score >= MIN_FIT_SCORE
        db.record_scored(conn, posting, ev, model=MODEL_NAME, prompt_version=PROMPT_VERSION,
                         save_as_match=is_strong_match, run_id=summary.run_id)
        summary.evaluated += 1
        done = summary.evaluated + summary.errors
        if is_strong_match:
            summary.matches += 1
            emit(ProgressEvent("match", f"   MATCH ({ev.fit_score}/100): {posting.title} @ "
                                        f"{posting.company} [{posting.location}]", done, to_score))
        else:
            emit(ProgressEvent("scored", f"   scored {ev.fit_score}/100: {posting.title}", done, to_score))


def run(on_progress: ProgressCallback | None = None,
        stop: threading.Event | None = None) -> RunSummary:
    """Fetch, dedupe, prefilter, score and save. Returns the run's summary.

        on_progress: called with a ProgressEvent for everything worth showing.
        stop:        set() it from another thread (e.g. a Stop button) to end the run
                     early; work done so far is kept and the rest waits for next run.
        """
    if not all([GEMINI_API_KEY, ADZUNA_APP_ID, ADZUNA_APP_KEY]):
        raise SystemExit("Missing GEMINI_API_KEY, ADZUNA_APP_ID or ADZUNA_APP_KEY in .env")
    if not SEARCH_PLANS:
        raise SystemExit("No search_plans found under pipeline_settings in config.json")
    emit = on_progress or (lambda event: None)
    stop = stop or threading.Event()  # never set unless the caller sets it

    conn = db.connect()
    summary = RunSummary(run_id=db.start_run(conn, model=MODEL_NAME, prompt_version=PROMPT_VERSION))
    emit(ProgressEvent("stage", f" Starting ingestion (run {summary.run_id}, prompt {PROMPT_VERSION})..."))
    # 1. Fetch every search and dedupe (remote jobs often appear in state searches too)
    try:
        raw = fetch_jobs(SEARCH_PLANS, emit, stop)
        summary.fetched = len(raw)

        # 2. Dedupe, then skip anything processed in an earlier run
        postings, summary.duplicates = dedupe(raw)
        postings, summary.already_seen = drop_seen(postings, db.seen_job_ids(conn))

        # 3. Free local filter
        keep, rejected = split_prefilter(postings)
        for posting in rejected:
            db.record_prefiltered(conn, posting)
        summary.prefiltered_out = len(rejected)
        emit(ProgressEvent("stage", f" {len(keep)} postings to score with Gemini"))

        # 4 + 5. Score and save
        if stop.is_set():
            summary.status = "stopped"
            summary.deferred += len(keep)
        else:
            score_and_save(conn, keep, summary, emit, stop)

        if summary.status == "stopped":
            emit(ProgressEvent("stage", " Stopped; progress so far is saved."))
    except KeyboardInterrupt:  # Ctrl+C in the terminal
        summary.status = "stopped"
        emit(ProgressEvent("stage", "\n Stopped by user; progress so far is saved."))
    except Exception as e:
        summary.status, error = "failed", f"{type(e).__name__}: {e}"
        raise
    finally:
        db.finish_run(conn, summary.run_id, status=summary.status,
                      stats=summary.counts(), error=error)
        conn.close()

    return summary


def print_progress(event: ProgressEvent) -> None:
    """The CLI's progress callback."""
    print(event.message)


if __name__ == "__main__":
    result = run(on_progress=print_progress)
    print(f"\n Run {result.status}")
    for key, value in result.counts().items():
        print(f"   {key.replace('_', ' '):<16} {value}")
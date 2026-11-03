# Internship Search Pipeline

A Python tool that finds internship postings, screens them against my eligibility and interests with an LLM, and tracks the ones I apply to.

Job boards return hundreds of loosely matching "intern" results. This pipeline pulls fresh postings from the [Adzuna API](https://developer.adzuna.com/), drops obvious mismatches with a free local filter, asks Google Gemini to score the remaining ones against a configurable candidate profile, and saves strong matches to a SQLite database. A command-line tracker then lets me review those matches and record where I've applied.

## How it works

```
Adzuna API ──► dedupe ──► regex prefilter ──► Gemini scoring ──► SQLite ──► CLI tracker
               (across     (free; drops        (structured        (matches.db)
                searches)   non-tech or         JSON via
                            senior roles)       pydantic)
```

1. **Fetch.** `Pipeline.py` runs several Adzuna searches (Michigan, Colorado, and remote) and removes duplicates, since remote roles often show up in more than one search.
2. **Skip what's already been seen.** Every processed posting is stored in the `seen_jobs` table, so no posting is sent to Gemini twice across runs.
3. **Prefilter.** Regular expressions discard postings that aren't internships or co-ops, aren't in a technical field, or are senior roles. This step costs nothing.
4. **Score.** The remaining postings go to Gemini with a prompt built from `config.json`. A pydantic schema forces structured output: a match decision, a 0–100 fit score, the matched major, the term (e.g. Summer 2027), relevant strengths, and the reasoning. Hard requirements (location, term, degree level, major, graduation date) can rule a posting out, and the scoring weights decide the fit score.
5. **Save.** Postings that match and score at or above `minimum_fit_score_to_save` are written to the `job_postings` table.

### Working within free-tier limits

The pipeline is designed to run on free API tiers:

- Gemini calls are spaced out (4.5 s by default) to stay under the per-minute limit.
- 429 and 5xx errors are retried with exponential backoff. If a 429 survives every retry, the daily quota is treated as used up and the run stops calling Gemini.
- Each run evaluates at most a set number of postings (150 by default). Anything left over is deferred to the next run instead of being marked as seen.
- Failed evaluations aren't recorded as seen, so they're retried automatically.

## Setup

Requires Python 3.10+.

```bash
git clone https://github.com/nickStanlis/InternshipSearch.git
cd InternshipSearch
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

**API keys.** Copy `.env.example` to `.env` and fill in:

- `GEMINI_API_KEY`: free from [Google AI Studio](https://aistudio.google.com/apikey)
- `ADZUNA_APP_ID` and `ADZUNA_APP_KEY`: free from the [Adzuna developer portal](https://developer.adzuna.com/)

**Profile.** Copy `config.example.json` to `config.json` and replace the placeholders with your own education, skills, work authorization, target terms, locations, and scoring weights. Both `.env` and `config.json` are gitignored.

The search locations and keywords are set in `SEARCH_PLANS` and `KEYWORDS_OR` near the top of `Pipeline.py`. Edit them to search elsewhere.

## Usage

**Find and score new postings:**

```bash
python Pipeline.py
```

Each run prints a summary: postings fetched, duplicates, already seen, prefiltered out, evaluated, matched, deferred, and errors.

**Review and track applications:**

```bash
python Tracker.py review                 # go through new matches one at a time, best fit first
python Tracker.py list                   # open postings (hides rejected / not interested)
python Tracker.py list --all             # everything
python Tracker.py list --status applied  # filter by status
python Tracker.py show 4                 # full details, URL, and the model's reasoning
python Tracker.py applied 4 7 12         # update status for one or more postings
python Tracker.py interviewing 4         # also: rejected, offer, skip, reset
python Tracker.py note 4 "Follow up Nov 10"
python Tracker.py summary                # counts by status and response rate
```

Running `Tracker.py` with no arguments shows the open list.

## Evaluation (in progress)

`Evaluate.py` is a labeling tool for measuring how well Gemini's decisions agree with my own. Each evaluated posting gets two hand labels:

1. **Snippet only:** judged from the same ~500-character description Gemini sees, with no way to open the full posting.
2. **Full posting:** judged after reading the complete listing.

Comparing the model against both labels separates two kinds of error: mistakes the model makes, and mistakes that come from the snippet missing key information. Labeling is underway, and results will be added here.

```bash
python Evaluate.py
```

## Project structure

| File | Purpose |
| --- | --- |
| `Pipeline.py` | Fetches, filters, and scores postings; writes to `matches.db` |
| `Tracker.py` | CLI for reviewing matches and tracking application status |
| `Evaluate.py` | CLI for hand-labeling postings to measure model accuracy |
| `config.example.json` | Template for the candidate profile and matching rules |
| `.env.example` | Template for API keys |

## Tech

Python · SQLite · Google Gemini API (`google-genai`) · Adzuna REST API · pydantic · requests

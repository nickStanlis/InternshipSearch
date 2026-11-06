"""Database layer for the internship search pipeline.

Every table, migration and query lives here. Pipeline, Tracker, Evaluate and
the UI call these functions instead of writing SQL themselves.

Usage:
    import db

    conn = db.connect()                       # opens matches.db and migrates it
    for p in db.list_postings(conn):
        print(p.num, p.title, p.status)
    db.set_status(conn, [p.job_id], db.APPLIED)
    conn.close()

Conventions:
- Postings are identified by `job_id` (the Adzuna id). `num` is the short
  rowid shown in the CLI; `resolve_posting()` turns either into a job_id.
- Every public function that writes data runs in its own transaction and
  commits before returning, so each call is all-or-nothing.
- SQLite connections must not be shared between threads. In the UI, open a
  connection per request or per background task with `connect()`.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Iterable, Protocol

try:
    from config import DB_PATH  # preferred: one definition shared by every module
except ImportError:  # config.py not created yet
    DB_PATH = Path(__file__).resolve().parent / "matches.db"


# ---------------------------------------------------------------------------
# Statuses
# ---------------------------------------------------------------------------
UNREVIEWED = "Unreviewed"
APPLIED = "Applied"
INTERVIEWING = "Interviewing"
OFFER = "Offer"
REJECTED = "Rejected"
NOT_INTERESTED = "Not Interested"

STATUS_ORDER = [OFFER, INTERVIEWING, APPLIED, UNREVIEWED, REJECTED, NOT_INTERESTED]
CLOSED_STATUSES = (REJECTED, NOT_INTERESTED)
APPLIED_OR_LATER = (APPLIED, INTERVIEWING, OFFER, REJECTED)

# Hand labels are stored as text so existing databases keep working.
_LABEL_TO_DB = {True: "Valid", False: "Invalid", None: "Unlabeled"}
_DB_TO_LABEL = {"Valid": True, "Invalid": False, "Unlabeled": None, None: None}

LEGACY_PROMPT_VERSION = "legacy"  # scores recorded before prompt versions existed


# ---------------------------------------------------------------------------
# Data classes returned by queries
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class NewPosting:
    """A posting fetched from a job board, before it is stored."""
    job_id: str
    title: str
    company: str
    location: str
    url: str | None
    description: str


@dataclass(slots=True)
class Posting:
    """A saved match in job_postings, i.e. something you might apply to."""
    num: int
    job_id: str
    title: str
    company: str
    location: str
    term: str | None
    url: str | None
    fit_score: int | None
    matched_major: str | None
    key_strengths: list[str]
    status: str
    date_logged: str | None
    applied_date: str | None
    status_updated: str | None
    reasoning: str | None = None  # from the most recent evaluation


@dataclass(slots=True)
class Note:
    id: int
    job_id: str
    created_at: str | None
    body: str


@dataclass(slots=True)
class SeenJob:
    """Any posting the pipeline has processed, with your hand labels."""
    num: int
    job_id: str
    title: str
    company: str
    location: str | None
    url: str | None
    description: str | None
    outcome: str  # 'prefiltered_out' | 'evaluated'
    first_seen: str | None
    snippet_label: bool | None  # True = match, False = not a match, None = unlabeled
    full_label: bool | None
    audit_sampled_at: str | None = None  # set when drawn into the prefilter audit sample


@dataclass(slots=True)
class Evaluation:
    """One model score for one posting under one model + prompt version."""
    id: int
    job_id: str
    model: str
    prompt_version: str
    is_match: bool
    fit_score: int | None
    term: str | None
    matched_major: str | None
    key_strengths: list[str]
    reasoning: str | None
    run_id: int | None
    created_at: str | None


@dataclass(slots=True)
class LabeledEvaluation:
    """A model decision paired with your labels, for accuracy metrics."""
    job_id: str
    title: str
    company: str
    model: str
    prompt_version: str
    predicted_match: bool
    fit_score: int | None
    snippet_label: bool | None
    full_label: bool | None


@dataclass(slots=True)
class Run:
    id: int
    started_at: str | None
    finished_at: str | None
    status: str  # 'running' | 'completed' | 'stopped' | 'failed'
    model: str | None
    prompt_version: str | None
    stats: dict = field(default_factory=dict)
    error: str | None = None
    kind: str = "pipeline"  # 'pipeline' | 'rescore'


class EvaluationLike(Protocol):
    """Anything shaped like Pipeline's JobEvaluation pydantic model."""
    is_match: bool
    fit_score: int
    term: str
    matched_major: str
    key_strengths: list[str]
    reasoning: str


# ---------------------------------------------------------------------------
# Connection and migrations
# ---------------------------------------------------------------------------
def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Open the database, apply any pending migrations and return the connection."""
    conn = sqlite3.connect(path or DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets the UI read while a pipeline run is writing.
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn)
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection) -> int:
    """Apply migrations newer than the database's version. Returns the new version."""
    if conn.in_transaction:
        conn.commit()
    current = schema_version(conn)
    for version, step in enumerate(_MIGRATIONS, start=1):
        if version <= current:
            continue
        conn.execute("BEGIN")
        try:
            step(conn)
            conn.execute(f"PRAGMA user_version = {version}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        current = version
    return current


def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn, table: str, column: str, decl: str) -> None:
    if column not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _m1_baseline(conn) -> None:
    """Bring any existing database to one known shape.

    Before this module, columns were added ad hoc by Pipeline.init_db,
    Tracker.connect and Evaluate.set_unevaluated, so an old database may have
    any mix of them. Everything here is safe to run on an old or empty file.
    """
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
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen_jobs (
            job_id TEXT PRIMARY KEY,
            title TEXT,
            company TEXT,
            outcome TEXT NOT NULL,
            is_match INTEGER,
            fit_score INTEGER,
            reasoning TEXT,
            description TEXT,
            first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )""")
    for col in ("applied_date", "status_updated", "notes"):
        _add_column(conn, "job_postings", col, "TEXT")
    for col in ("url", "description", "location", "model"):
        _add_column(conn, "seen_jobs", col, "TEXT")
    for col in ("training_status_description", "training_status_url"):
        _add_column(conn, "seen_jobs", col, "TEXT DEFAULT 'Unlabeled'")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_postings_status ON job_postings(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_seen_outcome ON seen_jobs(outcome)")


def _m2_runs_and_evaluations(conn) -> None:
    """Record pipeline runs, and allow several scores per posting."""
    conn.execute("""
        CREATE TABLE runs (
            id INTEGER PRIMARY KEY,
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            finished_at TIMESTAMP,
            status TEXT NOT NULL DEFAULT 'running',
            model TEXT,
            prompt_version TEXT,
            stats TEXT,
            error TEXT
        )""")
    conn.execute("""
        CREATE TABLE evaluations (
            id INTEGER PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES seen_jobs(job_id) ON DELETE CASCADE,
            model TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            is_match INTEGER NOT NULL,
            fit_score INTEGER,
            term TEXT,
            matched_major TEXT,
            key_strengths TEXT,          -- JSON list
            reasoning TEXT,
            run_id INTEGER REFERENCES runs(id) ON DELETE SET NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (job_id, model, prompt_version)
        )""")
    conn.execute("CREATE INDEX idx_evaluations_job ON evaluations(job_id)")

    # Copy existing scores out of seen_jobs. Term, major and strengths were only
    # kept for saved matches, so they come from job_postings when present.
    rows = conn.execute("""
        SELECT s.job_id, s.model, s.is_match, s.fit_score, s.reasoning, s.first_seen,
               p.term, p.matched_major, p.key_strengths
        FROM seen_jobs s LEFT JOIN job_postings p ON p.job_id = s.job_id
        WHERE s.outcome = 'evaluated' AND s.is_match IS NOT NULL""").fetchall()
    conn.executemany(
        """INSERT INTO evaluations
           (job_id, model, prompt_version, is_match, fit_score, term, matched_major,
            key_strengths, reasoning, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [(r["job_id"], r["model"] or "unknown", LEGACY_PROMPT_VERSION, r["is_match"],
          r["fit_score"], r["term"], r["matched_major"],
          json.dumps(_split_strengths(r["key_strengths"])), r["reasoning"], r["first_seen"])
         for r in rows])


_NOTE_LINE = re.compile(r"^\[(\d{4}-\d{2}-\d{2})\]\s?(.*)$")


def _m3_notes(conn) -> None:
    """Move notes from one appended text column into their own rows."""
    conn.execute("""
        CREATE TABLE notes (
            id INTEGER PRIMARY KEY,
            job_id TEXT NOT NULL REFERENCES job_postings(job_id) ON DELETE CASCADE,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            body TEXT NOT NULL
        )""")
    conn.execute("CREATE INDEX idx_notes_job ON notes(job_id)")

    # Old notes look like "[2026-11-01] text\n[2026-11-03] more text".
    # The old column is left in place (not deleted) as a backup.
    for job_id, text in conn.execute(
            "SELECT job_id, notes FROM job_postings WHERE notes IS NOT NULL AND notes != ''").fetchall():
        parsed: list[list] = []
        for line in text.splitlines():
            m = _NOTE_LINE.match(line)
            if m:
                parsed.append([m.group(1), m.group(2)])
            elif parsed:
                parsed[-1][1] += "\n" + line  # continuation of a multi-line note
            else:
                parsed.append([None, line])
        conn.executemany(
            "INSERT INTO notes (job_id, created_at, body) VALUES (?, ?, ?)",
            [(job_id, created, body.strip()) for created, body in parsed if body.strip()])


def _m4_audit_and_run_kind(conn) -> None:
    """Prefilter audit sampling, and tell pipeline runs apart from rescore runs.

    audit_sampled_at is set only on prefiltered postings drawn at random into
    the audit sample, so the audit's miss rate is computed from a fair sample
    rather than from whichever postings happened to get looked at.
    """
    _add_column(conn, "seen_jobs", "audit_sampled_at", "TEXT")
    _add_column(conn, "runs", "kind", "TEXT NOT NULL DEFAULT 'pipeline'")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_seen_audit ON seen_jobs(audit_sampled_at)")


_MIGRATIONS = [_m1_baseline, _m2_runs_and_evaluations, _m3_notes, _m4_audit_and_run_kind]
SCHEMA_VERSION = len(_MIGRATIONS)


# ---------------------------------------------------------------------------
# Row conversion helpers
# ---------------------------------------------------------------------------
def _split_strengths(value: str | None) -> list[str]:
    """job_postings stores strengths as 'a, b, c'; evaluations store JSON."""
    if not value:
        return []
    if value.startswith("["):
        return json.loads(value)
    return [s.strip() for s in value.split(",") if s.strip()]


def _posting(row: sqlite3.Row) -> Posting:
    keys = row.keys()
    return Posting(
        num=row["num"], job_id=row["job_id"], title=row["title"] or "",
        company=row["company"] or "", location=row["location"] or "", term=row["term"],
        url=row["url"], fit_score=row["fit_score"], matched_major=row["matched_major"],
        key_strengths=_split_strengths(row["key_strengths"]),
        status=row["status"] or UNREVIEWED, date_logged=row["date_logged"],
        applied_date=row["applied_date"], status_updated=row["status_updated"],
        reasoning=row["reasoning"] if "reasoning" in keys else None,
    )


def _seen_job(row: sqlite3.Row) -> SeenJob:
    return SeenJob(
        num=row["num"], job_id=row["job_id"], title=row["title"] or "",
        company=row["company"] or "", location=row["location"], url=row["url"],
        description=row["description"], outcome=row["outcome"], first_seen=row["first_seen"],
        snippet_label=_DB_TO_LABEL.get(row["training_status_description"]),
        full_label=_DB_TO_LABEL.get(row["training_status_url"]),
        audit_sampled_at=row["audit_sampled_at"],
    )


def _evaluation(row: sqlite3.Row) -> Evaluation:
    return Evaluation(
        id=row["id"], job_id=row["job_id"], model=row["model"],
        prompt_version=row["prompt_version"], is_match=bool(row["is_match"]),
        fit_score=row["fit_score"], term=row["term"], matched_major=row["matched_major"],
        key_strengths=_split_strengths(row["key_strengths"]), reasoning=row["reasoning"],
        run_id=row["run_id"], created_at=row["created_at"],
    )


def _run(row: sqlite3.Row) -> Run:
    return Run(
        id=row["id"], started_at=row["started_at"], finished_at=row["finished_at"],
        status=row["status"], model=row["model"], prompt_version=row["prompt_version"],
        stats=json.loads(row["stats"]) if row["stats"] else {}, error=row["error"],
        kind=row["kind"],
    )


# The latest evaluation per posting supplies its reasoning.
_POSTING_SELECT = """
    SELECT p.rowid AS num, p.*,
           (SELECT e.reasoning FROM evaluations e WHERE e.job_id = p.job_id
            ORDER BY e.created_at DESC, e.id DESC LIMIT 1) AS reasoning
    FROM job_postings p
"""

_SEEN_SELECT = "SELECT rowid AS num, * FROM seen_jobs"


# ---------------------------------------------------------------------------
# Pipeline: recording what was fetched and scored
# ---------------------------------------------------------------------------
def seen_job_ids(conn) -> set[str]:
    """Every job_id already processed, so it isn't fetched into Gemini again."""
    return {row[0] for row in conn.execute("SELECT job_id FROM seen_jobs")}


def _insert_seen(conn, posting: NewPosting, outcome: str, model: str | None) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO seen_jobs
           (job_id, title, company, location, outcome, url, description, model)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (posting.job_id, posting.title, posting.company, posting.location, outcome,
         posting.url or None, posting.description, model))


def _upsert_evaluation(conn, job_id: str, ev: EvaluationLike, model: str,
                       prompt_version: str, run_id: int | None) -> int:
    conn.execute(
        """INSERT INTO evaluations
           (job_id, model, prompt_version, is_match, fit_score, term, matched_major,
            key_strengths, reasoning, run_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (job_id, model, prompt_version) DO UPDATE SET
               is_match = excluded.is_match, fit_score = excluded.fit_score,
               term = excluded.term, matched_major = excluded.matched_major,
               key_strengths = excluded.key_strengths, reasoning = excluded.reasoning,
               run_id = excluded.run_id, created_at = CURRENT_TIMESTAMP""",
        (job_id, model, prompt_version, int(ev.is_match), ev.fit_score, ev.term,
         ev.matched_major, json.dumps(list(ev.key_strengths)), ev.reasoning, run_id))
    return conn.execute(
        "SELECT id FROM evaluations WHERE job_id = ? AND model = ? AND prompt_version = ?",
        (job_id, model, prompt_version)).fetchone()[0]


def record_prefiltered(conn, posting: NewPosting) -> None:
    """Mark a posting as seen that the regex prefilter rejected."""
    with conn:
        _insert_seen(conn, posting, "prefiltered_out", None)


def record_scored(conn, posting: NewPosting, ev: EvaluationLike, *, model: str,
                  prompt_version: str, save_as_match: bool, run_id: int | None = None) -> None:
    """Store a Gemini score, and save the posting as a match if asked, in one transaction.

    The pipeline decides `save_as_match` (e.g. ev.is_match and score >= threshold).
    Doing all three writes together means a crash can never leave a posting
    marked seen without its score.
    """
    with conn:
        _insert_seen(conn, posting, "evaluated", model)
        _upsert_evaluation(conn, posting.job_id, ev, model, prompt_version, run_id)
        if save_as_match:
            conn.execute(
                """INSERT OR IGNORE INTO job_postings
                   (job_id, title, company, location, term, url, fit_score,
                    matched_major, key_strengths)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (posting.job_id, posting.title, posting.company, posting.location, ev.term,
                 posting.url or None,  # NULL rather than '' so the UNIQUE constraint can't collide
                 ev.fit_score, ev.matched_major, ", ".join(ev.key_strengths)))


def record_evaluation(conn, job_id: str, ev: EvaluationLike, *, model: str,
                      prompt_version: str, run_id: int | None = None) -> int:
    """Store a (re-)score of an already-seen posting, e.g. when testing a new prompt
    on your labeled set. Re-scoring with the same model and prompt replaces the old score."""
    with conn:
        return _upsert_evaluation(conn, job_id, ev, model, prompt_version, run_id)


def start_run(conn, *, model: str | None, prompt_version: str | None,
              kind: str = "pipeline") -> int:
    """kind: 'pipeline' for a normal fetch-and-score run, 'rescore' for re-scoring labels."""
    with conn:
        cur = conn.execute("INSERT INTO runs (model, prompt_version, kind) VALUES (?, ?, ?)",
                           (model, prompt_version, kind))
        return cur.lastrowid


def finish_run(conn, run_id: int, *, status: str, stats: dict, error: str | None = None) -> None:
    """status: 'completed', 'stopped' (user pressed Stop) or 'failed'."""
    with conn:
        conn.execute(
            "UPDATE runs SET finished_at = CURRENT_TIMESTAMP, status = ?, stats = ?, error = ? "
            "WHERE id = ?", (status, json.dumps(stats), error, run_id))


def list_runs(conn, limit: int = 50, kind: str | None = None) -> list[Run]:
    """Most recent runs first; pass kind='pipeline' or 'rescore' to show only one type."""
    if kind:
        rows = conn.execute("SELECT * FROM runs WHERE kind = ? ORDER BY id DESC LIMIT ?",
                            (kind, limit)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [_run(r) for r in rows]


# ---------------------------------------------------------------------------
# Tracker: saved postings, statuses and notes
# ---------------------------------------------------------------------------
def resolve_posting(conn, ref: str | int) -> str | None:
    """Turn a short # (rowid) or a full Adzuna job_id into a job_id."""
    ref = str(ref)
    row = None
    if ref.isdigit():
        row = conn.execute("SELECT job_id FROM job_postings WHERE rowid = ?", (int(ref),)).fetchone()
    if row is None:
        row = conn.execute("SELECT job_id FROM job_postings WHERE job_id = ?", (ref,)).fetchone()
    return row[0] if row else None


def _status_rank(p: Posting) -> tuple:
    rank = STATUS_ORDER.index(p.status) if p.status in STATUS_ORDER else len(STATUS_ORDER)
    return rank, -(p.fit_score or 0)


def list_postings(conn, *, status: str | None = None, include_closed: bool = False) -> list[Posting]:
    """Saved postings, ordered by status (Offer first) then fit score.

    By default Rejected and Not Interested are hidden; pass include_closed=True
    for everything, or status=... for one status only.
    """
    query, params = _POSTING_SELECT, []
    if status:
        query += " WHERE p.status = ?"
        params.append(status)
    elif not include_closed:
        query += f" WHERE p.status NOT IN ({','.join('?' * len(CLOSED_STATUSES))})"
        params.extend(CLOSED_STATUSES)
    postings = [_posting(r) for r in conn.execute(query, params)]
    return sorted(postings, key=_status_rank)


def get_posting(conn, job_id: str) -> Posting | None:
    row = conn.execute(_POSTING_SELECT + " WHERE p.job_id = ?", (job_id,)).fetchone()
    return _posting(row) if row else None


def review_queue(conn) -> list[Posting]:
    """Unreviewed postings, best fit first, then newest."""
    rows = conn.execute(_POSTING_SELECT + " WHERE p.status = ? "
                        "ORDER BY p.fit_score DESC, p.date_logged DESC", (UNREVIEWED,))
    return [_posting(r) for r in rows]


def set_status(conn, job_ids: Iterable[str], status: str) -> int:
    """Change status for one or more postings. Returns how many were updated.

    Marking a posting Applied records today's date as applied_date the first
    time; later status changes keep it.
    """
    if status not in STATUS_ORDER:
        raise ValueError(f"Unknown status {status!r}; expected one of {STATUS_ORDER}")
    today = date.today().isoformat()
    updated = 0
    with conn:
        for job_id in job_ids:
            if status == APPLIED:
                cur = conn.execute(
                    "UPDATE job_postings SET status = ?, status_updated = ?, "
                    "applied_date = COALESCE(applied_date, ?) WHERE job_id = ?",
                    (status, today, today, job_id))
            else:
                cur = conn.execute(
                    "UPDATE job_postings SET status = ?, status_updated = ? WHERE job_id = ?",
                    (status, today, job_id))
            updated += cur.rowcount
    return updated


def status_counts(conn) -> dict[str, int]:
    """Number of saved postings per status, in STATUS_ORDER (only non-zero statuses)."""
    counts = dict(conn.execute(
        "SELECT COALESCE(status, ?), COUNT(*) FROM job_postings GROUP BY 1", (UNREVIEWED,)).fetchall())
    ordered = STATUS_ORDER + sorted(set(counts) - set(STATUS_ORDER))
    return {s: counts[s] for s in ordered if counts.get(s)}


def add_note(conn, job_id: str, body: str) -> Note:
    body = body.strip()
    if not body:
        raise ValueError("Note is empty")
    created = date.today().isoformat()
    with conn:
        cur = conn.execute("INSERT INTO notes (job_id, created_at, body) VALUES (?, ?, ?)",
                           (job_id, created, body))
    return Note(id=cur.lastrowid, job_id=job_id, created_at=created, body=body)


def list_notes(conn, job_id: str) -> list[Note]:
    rows = conn.execute("SELECT * FROM notes WHERE job_id = ? ORDER BY created_at, id", (job_id,))
    return [Note(**dict(r)) for r in rows]


def update_note(conn, note_id: int, body: str) -> None:
    with conn:
        conn.execute("UPDATE notes SET body = ? WHERE id = ?", (body.strip(), note_id))


def delete_note(conn, note_id: int) -> None:
    with conn:
        conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))


# ---------------------------------------------------------------------------
# Evaluation cycle: hand labels and model scores
# ---------------------------------------------------------------------------
def labeling_queue(conn) -> list[SeenJob]:
    """Scored postings still missing a snippet or full-posting label, newest first."""
    rows = conn.execute(
        _SEEN_SELECT + """
        WHERE outcome = 'evaluated'
          AND (COALESCE(training_status_description, 'Unlabeled') = 'Unlabeled'
               OR COALESCE(training_status_url, 'Unlabeled') = 'Unlabeled')
        ORDER BY rowid DESC""")
    return [_seen_job(r) for r in rows]


def get_seen_job(conn, job_id: str) -> SeenJob | None:
    row = conn.execute(_SEEN_SELECT + " WHERE job_id = ?", (job_id,)).fetchone()
    return _seen_job(row) if row else None


def set_snippet_label(conn, job_id: str, is_match: bool | None) -> None:
    """Your judgment from the ~500-character snippet alone. None clears it."""
    with conn:
        conn.execute("UPDATE seen_jobs SET training_status_description = ? WHERE job_id = ?",
                     (_LABEL_TO_DB[is_match], job_id))


def set_full_label(conn, job_id: str, is_match: bool | None) -> None:
    """Your judgment after reading the full posting. None clears it."""
    with conn:
        conn.execute("UPDATE seen_jobs SET training_status_url = ? WHERE job_id = ?",
                     (_LABEL_TO_DB[is_match], job_id))


def labeled_jobs(conn, outcome: str | None = "evaluated") -> list[SeenJob]:
    """Postings with at least one hand label: the test set for re-scoring with a new prompt.

    By default only postings that went through Gemini; audited prefiltered postings
    are measured separately. Pass outcome=None for both.
    """
    query = _SEEN_SELECT + """
        WHERE (training_status_description IN ('Valid', 'Invalid')
               OR training_status_url IN ('Valid', 'Invalid'))"""
    params: list = []
    if outcome:
        query += " AND outcome = ?"
        params.append(outcome)
    return [_seen_job(r) for r in conn.execute(query + " ORDER BY rowid", params)]


def scored_job_ids(conn, model: str, prompt_version: str) -> set[str]:
    """job_ids that already have a score from this exact model + prompt version."""
    return {r[0] for r in conn.execute(
        "SELECT job_id FROM evaluations WHERE model = ? AND prompt_version = ?",
        (model, prompt_version))}


# ---------------------------------------------------------------------------
# Prefilter audit: a random sample of postings the regex prefilter rejected
# ---------------------------------------------------------------------------
def _audit_window(within_days: int | None) -> tuple[str, list]:
    if within_days is None:
        return "", []
    return " AND first_seen >= datetime('now', ?)", [f"-{int(within_days)} days"]


def unsampled_prefiltered_ids(conn, within_days: int | None = None) -> list[str]:
    """Prefiltered postings not yet drawn into the audit sample (optionally only recent ones)."""
    clause, params = _audit_window(within_days)
    return [r[0] for r in conn.execute(
        "SELECT job_id FROM seen_jobs WHERE outcome = 'prefiltered_out' "
        "AND audit_sampled_at IS NULL" + clause + " ORDER BY rowid", params)]


def mark_audit_sampled(conn, job_ids: Iterable[str]) -> int:
    """Add postings to the audit sample. The random choice is made by the caller."""
    with conn:
        cur = conn.executemany(
            "UPDATE seen_jobs SET audit_sampled_at = CURRENT_TIMESTAMP "
            "WHERE job_id = ? AND outcome = 'prefiltered_out' AND audit_sampled_at IS NULL",
            [(j,) for j in job_ids])
    return cur.rowcount


def audit_queue(conn) -> list[SeenJob]:
    """Sampled prefiltered postings still missing a label, in the order they were drawn."""
    rows = conn.execute(
        _SEEN_SELECT + """
        WHERE audit_sampled_at IS NOT NULL
          AND (COALESCE(training_status_description, 'Unlabeled') = 'Unlabeled'
               OR COALESCE(training_status_url, 'Unlabeled') = 'Unlabeled')
        ORDER BY audit_sampled_at, rowid""")
    return [_seen_job(r) for r in rows]


def audit_jobs(conn) -> list[SeenJob]:
    """Every posting in the audit sample, labeled or not."""
    rows = conn.execute(_SEEN_SELECT + " WHERE audit_sampled_at IS NOT NULL "
                                       "ORDER BY audit_sampled_at, rowid")
    return [_seen_job(r) for r in rows]


def prefiltered_count(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM seen_jobs WHERE outcome = 'prefiltered_out'").fetchone()[0]


def label_counts(conn, *, audit: bool = False) -> dict[str, int]:
    """How far labeling has got, for progress bars.

    audit=False: postings scored by Gemini. audit=True: the prefilter audit sample.
    Returns {'total', 'fully_labeled', 'snippet_only', 'unlabeled'}.
    """
    where = "audit_sampled_at IS NOT NULL" if audit else "outcome = 'evaluated'"
    row = conn.execute(f"""
        SELECT COUNT(*),
               SUM(training_status_description IN ('Valid', 'Invalid')
                   AND training_status_url IN ('Valid', 'Invalid')),
               SUM(training_status_description IN ('Valid', 'Invalid')
                   AND COALESCE(training_status_url, 'Unlabeled') = 'Unlabeled')
        FROM seen_jobs WHERE {where}""").fetchone()
    total, full, snippet_only = row[0], row[1] or 0, row[2] or 0
    return {"total": total, "fully_labeled": full, "snippet_only": snippet_only,
            "unlabeled": total - full - snippet_only}


def list_evaluations(conn, job_id: str) -> list[Evaluation]:
    """Every score a posting has received, newest first."""
    rows = conn.execute("SELECT * FROM evaluations WHERE job_id = ? "
                        "ORDER BY created_at DESC, id DESC", (job_id,))
    return [_evaluation(r) for r in rows]


def evaluation_versions(conn) -> list[tuple[str, str, int]]:
    """(model, prompt_version, number of scores) for each version, most recent first."""
    return [tuple(r) for r in conn.execute(
        """SELECT model, prompt_version, COUNT(*) FROM evaluations
           GROUP BY model, prompt_version ORDER BY MAX(created_at) DESC""")]


def labeled_evaluations(conn, *, model: str | None = None, prompt_version: str | None = None,
                        outcome: str | None = "evaluated") -> list[LabeledEvaluation]:
    """Model decisions joined with your labels, optionally for one model/prompt version.
    This is the input for precision/recall and confusion-matrix calculations.

    Only postings that passed the prefilter by default, so scoring audited prefiltered
    postings later can't skew the model's numbers. Pass outcome=None to include them.
    """
    query = """
        SELECT e.job_id, s.title, s.company, e.model, e.prompt_version, e.is_match,
               e.fit_score, s.training_status_description, s.training_status_url
        FROM evaluations e JOIN seen_jobs s ON s.job_id = e.job_id
        WHERE (s.training_status_description IN ('Valid', 'Invalid')
               OR s.training_status_url IN ('Valid', 'Invalid'))"""
    params: list = []
    if outcome:
        query += " AND s.outcome = ?"
        params.append(outcome)
    if model:
        query += " AND e.model = ?"
        params.append(model)
    if prompt_version:
        query += " AND e.prompt_version = ?"
        params.append(prompt_version)
    return [
        LabeledEvaluation(
            job_id=r["job_id"], title=r["title"] or "", company=r["company"] or "",
            model=r["model"], prompt_version=r["prompt_version"],
            predicted_match=bool(r["is_match"]), fit_score=r["fit_score"],
            snippet_label=_DB_TO_LABEL.get(r["training_status_description"]),
            full_label=_DB_TO_LABEL.get(r["training_status_url"]))
        for r in conn.execute(query, params)]
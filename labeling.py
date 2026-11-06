"""Labeling service: everything the UI needs for the evaluation cycle.

Three jobs:
  1. Hand labeling of postings Gemini scored (the "labeling" queue).
  2. The prefilter audit: label a random sample of postings the regex prefilter
     rejected, to measure how many good postings it throws away (the "audit" queue).
  3. Rescoring: run the current prompt over your labeled postings, so prompt
     versions can be compared on the same set.

Every posting is labeled in two steps, always in this order:
  snippet: judged from the same short description Gemini sees (no link to the posting)
  full:    judged after opening the full posting; this label is the ground truth

Nothing here prints or asks for input. Functions take a connection and return data;
problems the user should see are raised as LabelingError with a readable message.
SQLite connections can't be shared between threads, so open one with db.connect()
in whichever thread calls these (rescore_labeled opens its own if you don't pass one).

Typical UI flow:
    task = labeling.next_task(conn, labeling.LABELING, skip=skipped_this_session)
    # show task.title / task.snippet; show task.url only when task.step == FULL
    labeling.submit_label(conn, task.job_id, task.step, True)
"""
from __future__ import annotations

import random
import threading
import time
from dataclasses import asdict, dataclass
from typing import Callable, Iterable

import db

# Queues
LABELING = "labeling"  # postings scored by Gemini
AUDIT = "audit"        # random sample of postings the prefilter rejected
QUEUES = (LABELING, AUDIT)

# Steps
SNIPPET = "snippet"
FULL = "full"
STEPS = (SNIPPET, FULL)


class LabelingError(ValueError):
    """Something the user tried that isn't allowed; the message is safe to show in the UI."""


# ---------------------------------------------------------------------------
# Labeling tasks
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class LabelTask:
    """One label to give: which posting, which step, and what may be shown.

    `url` is None during the snippet step, so the UI can't reveal the full posting
    early even by accident. Gemini's decision is never included, so labels stay blind.
    """
    job_id: str
    num: int
    queue: str
    step: str
    title: str
    company: str
    location: str | None
    snippet: str
    url: str | None
    remaining: int  # postings left in this queue, including this one (skipped ones excluded)


@dataclass(slots=True)
class LabelProgress:
    queue: str
    total: int
    fully_labeled: int
    snippet_only: int
    unlabeled: int

    @property
    def fraction_done(self) -> float:
        return self.fully_labeled / self.total if self.total else 0.0


def _check_queue(queue: str) -> None:
    if queue not in QUEUES:
        raise LabelingError(f"Unknown queue {queue!r}; expected one of {QUEUES}")


def _queue_jobs(conn, queue: str) -> list[db.SeenJob]:
    _check_queue(queue)
    return db.labeling_queue(conn) if queue == LABELING else db.audit_queue(conn)


def _task(job: db.SeenJob, queue: str, remaining: int) -> LabelTask:
    step = SNIPPET if job.snippet_label is None else FULL
    return LabelTask(
        job_id=job.job_id, num=job.num, queue=queue, step=step,
        title=job.title, company=job.company, location=job.location,
        snippet=job.description or "(no snippet stored)",
        url=job.url if step == FULL else None,
        remaining=remaining,
    )


def next_task(conn, queue: str = LABELING, skip: Iterable[str] = ()) -> LabelTask | None:
    """The next label to give in a queue, or None when the queue is done.

    `skip` holds job_ids the user skipped this session; they come back next session.
    If a posting has its snippet label but not its full label, its full step is next.
    """
    skip = set(skip)
    jobs = [j for j in _queue_jobs(conn, queue) if j.job_id not in skip]
    return _task(jobs[0], queue, len(jobs)) if jobs else None


def get_task(conn, job_id: str) -> LabelTask | None:
    """The current step for one posting (e.g. to reopen it from a list), or None if
    it's fully labeled or isn't in either queue."""
    job = db.get_seen_job(conn, job_id)
    if job is None or job.full_label is not None:
        return None
    queue = _eligible_queue(job)
    return _task(job, queue, 1) if queue else None


def _eligible_queue(job: db.SeenJob) -> str | None:
    if job.outcome == "evaluated":
        return LABELING
    if job.audit_sampled_at is not None:
        return AUDIT
    return None


def _get_eligible(conn, job_id: str) -> db.SeenJob:
    job = db.get_seen_job(conn, job_id)
    if job is None:
        raise LabelingError(f"No posting with id {job_id}")
    if _eligible_queue(job) is None:
        raise LabelingError("This posting was prefiltered out and isn't in the audit sample, "
                            "so labeling it would skew the audit. Draw it into the sample first.")
    return job


def submit_label(conn, job_id: str, step: str, is_match: bool) -> None:
    """Record your answer for one step. is_match: True = it's a match for you.

    Rules (this is what keeps the snippet/full comparison honest):
      - the snippet label must come first
      - the snippet label can't change once the full label exists, because by then
        you've seen the full posting; use reset_labels to relabel both
    """
    if step not in STEPS:
        raise LabelingError(f"Unknown step {step!r}; expected one of {STEPS}")
    if not isinstance(is_match, bool):
        raise LabelingError("A label must be True (match) or False (not a match)")
    job = _get_eligible(conn, job_id)

    if step == SNIPPET:
        if job.full_label is not None:
            raise LabelingError("The snippet label can't be changed after the full posting was "
                                "labeled. Reset this posting to label both steps again.")
        db.set_snippet_label(conn, job_id, is_match)
    else:
        if job.snippet_label is None:
            raise LabelingError("Label the snippet first; the full-posting label comes second.")
        db.set_full_label(conn, job_id, is_match)


def undo_label(conn, job_id: str, step: str) -> None:
    """Clear one label, e.g. for an Undo button right after a mis-click.

    Clearing the full label is always allowed. Clearing the snippet label is only
    allowed while there's no full label (otherwise use reset_labels).
    """
    if step not in STEPS:
        raise LabelingError(f"Unknown step {step!r}; expected one of {STEPS}")
    job = _get_eligible(conn, job_id)
    if step == FULL:
        db.set_full_label(conn, job_id, None)
    elif job.full_label is not None:
        raise LabelingError("Clear the full-posting label first, or reset both.")
    else:
        db.set_snippet_label(conn, job_id, None)


def reset_labels(conn, job_id: str) -> None:
    """Clear both labels so the posting goes back into its queue."""
    _get_eligible(conn, job_id)
    db.set_full_label(conn, job_id, None)
    db.set_snippet_label(conn, job_id, None)


def progress(conn, queue: str = LABELING) -> LabelProgress:
    _check_queue(queue)
    return LabelProgress(queue=queue, **db.label_counts(conn, audit=(queue == AUDIT)))


# ---------------------------------------------------------------------------
# Prefilter audit sampling
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class AuditStatus:
    prefiltered_total: int  # every posting the prefilter has ever rejected
    sampled: int            # drawn into the audit sample so far
    available: int          # not sampled yet, so they can still be drawn
    fully_labeled: int
    remaining: int          # sampled but not fully labeled yet


def audit_status(conn, within_days: int | None = None) -> AuditStatus:
    """within_days limits `available` to recent postings, matching draw_audit_sample."""
    counts = db.label_counts(conn, audit=True)
    return AuditStatus(
        prefiltered_total=db.prefiltered_count(conn),
        sampled=counts["total"],
        available=len(db.unsampled_prefiltered_ids(conn, within_days)),
        fully_labeled=counts["fully_labeled"],
        remaining=counts["total"] - counts["fully_labeled"],
    )


def draw_audit_sample(conn, n: int, *, within_days: int | None = None,
                      rng: random.Random | None = None) -> int:
    """Add n randomly chosen prefiltered postings to the audit queue. Returns how many
    were added (fewer than n if not enough are left).

    The choice must be random: if you picked which rejected postings to look at,
    you'd tend to pick the interesting ones and the miss rate would be too high.

    within_days: only draw from postings first seen in the last N days. Job board
    links expire, so old postings may be impossible to open for the full label.
    rng: pass random.Random(seed) for a repeatable draw (used in tests).
    """
    if n < 1:
        raise LabelingError("Draw at least one posting")
    candidates = db.unsampled_prefiltered_ids(conn, within_days)
    if not candidates:
        return 0
    chosen = (rng or random.Random()).sample(candidates, min(n, len(candidates)))
    return db.mark_audit_sampled(conn, chosen)


# ---------------------------------------------------------------------------
# Rescoring labeled postings with the current prompt
# ---------------------------------------------------------------------------
@dataclass
class RescoreSummary:
    run_id: int | None = None
    status: str = "completed"  # "completed" | "stopped" | "failed"
    model: str = ""
    prompt_version: str = ""
    labeled: int = 0          # labeled postings in total
    already_scored: int = 0   # skipped because this model + prompt already scored them
    to_score: int = 0
    scored: int = 0
    errors: int = 0
    deferred: int = 0         # left for next time (Stop, quota or the per-run cap)

    def counts(self) -> dict:
        d = asdict(self)
        for key in ("run_id", "status", "model", "prompt_version"):
            del d[key]
        return d


def rescore_targets(conn, model: str, prompt_version: str,
                    only_missing: bool = True) -> list[db.SeenJob]:
    """Labeled postings (that went through Gemini) to rescore with this version."""
    jobs = db.labeled_jobs(conn)
    if only_missing:
        done = db.scored_job_ids(conn, model, prompt_version)
        jobs = [j for j in jobs if j.job_id not in done]
    return jobs


def rescore_labeled(conn=None, *, on_progress: Callable | None = None,
                    stop: threading.Event | None = None, only_missing: bool = True,
                    limit: int | None = None, evaluate: Callable | None = None) -> RescoreSummary:
    """Score your labeled postings with the current model and prompt (from config.json).

    Uses each posting's stored snippet, so it sees exactly what the pipeline saw.
    Scores are saved as a new version in `evaluations`; your tracker and saved
    matches are never changed. Runs under the same rate limit, retries, quota
    handling and Stop button as the pipeline, and is recorded in the runs table
    with kind='rescore'.

    only_missing: skip postings this exact model + prompt already scored, so a
                  stopped rescore picks up where it left off. False re-scores all.
    limit:        max postings per call (default: the pipeline's per-run cap).
    evaluate:     replacement for Gemini, used in tests.
    on_progress, stop: the same as Pipeline.run.

    Meant to run in a background thread from the UI, like the pipeline.
    """
    import Pipeline as P  # imported here: Pipeline reads config.json when imported

    if evaluate is None and not P.GEMINI_API_KEY:
        raise LabelingError("GEMINI_API_KEY is missing from .env")
    emit = on_progress or (lambda event: None)
    stop = stop or threading.Event()
    limit = P.MAX_EVALS_PER_RUN if limit is None else limit
    evaluate = evaluate or P.evaluate_with_retry

    own_conn = conn is None
    conn = conn or db.connect()
    summary = RescoreSummary(model=P.MODEL_NAME, prompt_version=P.PROMPT_VERSION)
    error = None
    try:
        summary.labeled = len(db.labeled_jobs(conn))
        targets = rescore_targets(conn, summary.model, summary.prompt_version, only_missing)
        summary.already_scored = summary.labeled - len(targets)
        summary.to_score = min(len(targets), limit)
        summary.deferred = len(targets) - summary.to_score
        summary.run_id = db.start_run(conn, model=summary.model,
                                      prompt_version=summary.prompt_version, kind="rescore")
        emit(P.ProgressEvent("stage", f" Rescoring {summary.to_score} labeled postings with "
                                      f"{summary.model}, prompt {summary.prompt_version}"))

        last_call = 0.0
        for i, job in enumerate(targets[:summary.to_score]):
            left = summary.to_score - i
            wait = P.SECONDS_BETWEEN_CALLS - (time.monotonic() - last_call)
            if stop.is_set() or (wait > 0 and stop.wait(wait)):
                summary.status = "stopped"
                summary.deferred += left
                break
            last_call = time.monotonic()

            posting = db.NewPosting(job.job_id, job.title, job.company, job.location or "",
                                    job.url, job.description or "")
            try:
                ev = evaluate(posting, emit, stop)
            except P.Stopped:
                summary.status = "stopped"
                summary.deferred += left
                break
            except P.QuotaExhausted:
                summary.deferred += left
                emit(P.ProgressEvent("warning", "Gemini daily quota exhausted; "
                                                "run the rescore again tomorrow to finish."))
                break
            except Exception as e:
                summary.errors += 1
                emit(P.ProgressEvent("warning", f"   Rescore error on {job.job_id} ({job.title}): {e}"))
                continue

            db.record_evaluation(conn, job.job_id, ev, model=summary.model,
                                 prompt_version=summary.prompt_version, run_id=summary.run_id)
            summary.scored += 1
            emit(P.ProgressEvent("scored", f"   {ev.fit_score}/100 "
                                           f"{'match' if ev.is_match else 'no match'}: {job.title}",
                                 summary.scored + summary.errors, summary.to_score))

        if summary.status == "stopped":
            emit(P.ProgressEvent("stage", " Rescore stopped; scores so far are saved."))
    except Exception as e:
        summary.status, error = "failed", f"{type(e).__name__}: {e}"
        raise
    finally:
        if summary.run_id is not None:
            db.finish_run(conn, summary.run_id, status=summary.status,
                          stats=summary.counts(), error=error)
        if own_conn:
            conn.close()
    return summary

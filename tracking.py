"""Tracking service: the application-tracking actions the UI needs.

The status rules themselves (the statuses, their order, which are closed, and
recording applied_date the first time a posting is marked Applied) live in db.py.
This module adds what the UI needs on top: the summary with response rate, the
review flow, and one-call actions that combine a status change with a note.

Nothing here prints or asks for input; problems are raised as TrackingError with
a message that's safe to show the user.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import db

STATUSES = tuple(db.STATUS_ORDER)  # for a status dropdown, in display order


class TrackingError(ValueError):
    """Something the user tried that isn't allowed; the message is safe to show."""


@dataclass(slots=True)
class TrackerSummary:
    counts: dict[str, int]       # postings per status, in STATUS_ORDER (non-zero only)
    total: int
    open: int                    # everything except Rejected / Not Interested
    unreviewed: int
    applied: int                 # applied or further along (Interviewing, Offer, Rejected)
    responses: int               # reached Interviewing or Offer
    response_rate: float | None  # responses / applied, None before applying anywhere


def summary(conn) -> TrackerSummary:
    counts = db.status_counts(conn)
    applied = sum(counts.get(s, 0) for s in db.APPLIED_OR_LATER)
    responses = counts.get(db.INTERVIEWING, 0) + counts.get(db.OFFER, 0)
    total = sum(counts.values())
    return TrackerSummary(
        counts=counts, total=total,
        open=total - sum(counts.get(s, 0) for s in db.CLOSED_STATUSES),
        unreviewed=counts.get(db.UNREVIEWED, 0),
        applied=applied, responses=responses,
        response_rate=responses / applied if applied else None,
    )


# ---------------------------------------------------------------------------
# Review flow: unreviewed postings one at a time, best fit first
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class ReviewItem:
    posting: db.Posting
    remaining: int  # unreviewed postings left, including this one (skipped ones excluded)
    snippet: str | None = None  # the stored description Gemini scored


def next_for_review(conn, skip: Iterable[str] = (), prefer: str | None = None) -> ReviewItem | None:
    """The next posting to review, or None when done.

    `skip` holds job_ids passed over this session ("decide later"); they stay
    Unreviewed and come back next session. `prefer` puts one posting first if it's
    still unreviewed (e.g. right after undoing a decision on it).
    """
    skip = set(skip)
    queue = [p for p in db.review_queue(conn) if p.job_id not in skip]
    if not queue:
        return None
    first = next((p for p in queue if p.job_id == prefer), queue[0])
    seen = db.get_seen_job(conn, first.job_id)
    return ReviewItem(first, len(queue), seen.description if seen else None)


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------
def _require(conn, job_id: str) -> db.Posting:
    posting = db.get_posting(conn, job_id)
    if posting is None:
        raise TrackingError(f"No saved posting with id {job_id}")
    return posting


def set_status(conn, job_ids: Iterable[str], status: str) -> int:
    """Change status for one or more postings (e.g. bulk actions in a table)."""
    if status not in db.STATUS_ORDER:
        raise TrackingError(f"Unknown status {status!r}")
    return db.set_status(conn, list(job_ids), status)


def mark_applied(conn, job_id: str, note: str | None = None) -> db.Posting:
    """Mark Applied (applied_date is recorded the first time) and optionally add a note."""
    _require(conn, job_id)
    body = _clean_note(note) if note and note.strip() else None  # validate before changing anything
    db.set_status(conn, [job_id], db.APPLIED)
    if body:
        db.add_note(conn, job_id, body)
    return db.get_posting(conn, job_id)


def mark_not_interested(conn, job_id: str) -> db.Posting:
    _require(conn, job_id)
    db.set_status(conn, [job_id], db.NOT_INTERESTED)
    return db.get_posting(conn, job_id)


MAX_NOTE_LENGTH = 5000


def _clean_note(body: str) -> str:
    body = (body or "").strip()
    if not body:
        raise TrackingError("Note is empty")
    if len(body) > MAX_NOTE_LENGTH:
        raise TrackingError(f"Notes can be at most {MAX_NOTE_LENGTH:,} characters "
                            f"(this one is {len(body):,}).")
    return body


def add_note(conn, job_id: str, body: str) -> db.Note:
    _require(conn, job_id)
    return db.add_note(conn, job_id, _clean_note(body))


def _require_note(conn, job_id: str, note_id: int) -> db.Note:
    note = next((n for n in db.list_notes(conn, job_id) if n.id == note_id), None)
    if note is None:
        raise TrackingError("That note no longer exists.")
    return note


def edit_note(conn, job_id: str, note_id: int, body: str) -> None:
    """Change a note's text. job_id makes sure the note belongs to that posting."""
    _require_note(conn, job_id, note_id)
    db.update_note(conn, note_id, _clean_note(body))


def delete_note(conn, job_id: str, note_id: int) -> db.Note:
    """Delete a note; returns it so the UI can offer to restore it."""
    note = _require_note(conn, job_id, note_id)
    db.delete_note(conn, note_id)
    return note


# ---------------------------------------------------------------------------
# Undo
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class StatusSnapshot:
    """A posting's status fields before a change, so the change can be undone."""
    job_id: str
    status: str
    applied_date: str | None
    status_updated: str | None


def snapshot(conn, job_id: str) -> StatusSnapshot:
    p = _require(conn, job_id)
    return StatusSnapshot(job_id, p.status, p.applied_date, p.status_updated)


def restore(conn, snap: StatusSnapshot) -> db.Posting:
    """Undo a status change, including any applied_date it recorded."""
    _require(conn, snap.job_id)
    db.restore_status(conn, snap.job_id, status=snap.status, applied_date=snap.applied_date,
                      status_updated=snap.status_updated)
    return db.get_posting(conn, snap.job_id)


# ---------------------------------------------------------------------------
# Detail page
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class PostingDetail:
    posting: db.Posting
    notes: list[db.Note]
    evaluations: list[db.Evaluation]  # every Gemini score for this posting, newest first


def posting_detail(conn, job_id: str) -> PostingDetail:
    posting = _require(conn, job_id)
    return PostingDetail(posting, db.list_notes(conn, job_id), db.list_evaluations(conn, job_id))

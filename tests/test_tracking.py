"""Tests for the tracking additions: note editing, validation, undo and review order."""
import pytest

import db
import tracking
from tests.demo_data import seed


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "t.db"
    seed(path)
    c = db.connect(path)
    yield c
    c.close()


def test_edit_and_delete_note(conn):
    note = tracking.add_note(conn, "d1", "first")
    tracking.edit_note(conn, "d1", note.id, "  changed  ")
    assert [n.body for n in db.list_notes(conn, "d1")] == ["changed"]
    deleted = tracking.delete_note(conn, "d1", note.id)
    assert deleted.body == "changed" and db.list_notes(conn, "d1") == []


@pytest.mark.parametrize("body", ["", "   ", "x" * (tracking.MAX_NOTE_LENGTH + 1)])
def test_invalid_notes_are_rejected(conn, body):
    with pytest.raises(tracking.TrackingError):
        tracking.add_note(conn, "d1", body)


def test_note_must_belong_to_the_posting(conn):
    note = tracking.add_note(conn, "d1", "mine")
    with pytest.raises(tracking.TrackingError):
        tracking.edit_note(conn, "d2", note.id, "hijack")
    with pytest.raises(tracking.TrackingError):
        tracking.delete_note(conn, "d2", note.id)
    assert db.list_notes(conn, "d1")[0].body == "mine"


def test_invalid_note_on_mark_applied_changes_nothing(conn):
    with pytest.raises(tracking.TrackingError):
        tracking.mark_applied(conn, "d1", "x" * (tracking.MAX_NOTE_LENGTH + 1))
    assert db.get_posting(conn, "d1").status == db.UNREVIEWED


def test_restore_undoes_applied_including_the_date(conn):
    before = tracking.snapshot(conn, "d1")
    tracking.mark_applied(conn, "d1")
    assert db.get_posting(conn, "d1").applied_date is not None
    p = tracking.restore(conn, before)
    assert (p.status, p.applied_date, p.status_updated) == (db.UNREVIEWED, None, None)


def test_review_order_skip_prefer_and_snippet(conn):
    first = tracking.next_for_review(conn)
    assert first.posting.job_id == "d1" and first.remaining == 3  # best fit first; d2 is Applied
    assert first.snippet.startswith("Join our analytics team")
    assert tracking.next_for_review(conn, skip={"d1"}).posting.job_id == "d5"
    assert tracking.next_for_review(conn, prefer="d3").posting.job_id == "d3"
    assert tracking.next_for_review(conn, prefer="d2").posting.job_id == "d1"  # not unreviewed

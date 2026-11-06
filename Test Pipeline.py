"""Tests for the pipeline steps. No network, API keys or Gemini quota needed."""
import threading
import time

import pytest

import db
import Pipeline as P


def job(i, title="Data Science Intern", description="python data"):
    """A fake Adzuna result."""
    return {"id": i, "title": title, "description": description,
            "company": {"display_name": "Acme"}, "location": {"display_name": "Detroit, MI"},
            "redirect_url": f"http://example.com/{i}"}


def evaluation(score=90, is_match=True):
    return P.JobEvaluation(is_match=is_match, fit_score=score, matched_major="Computer Science",
                           term="Summer 2027", key_strengths=["Python"], reasoning="test")


def fake_evaluator(scores):
    """Returns an evaluate() that hands out these results in order (an Exception is raised)."""
    results = iter(scores)
    def evaluate(posting, emit, stop):
        r = next(results)
        if isinstance(r, Exception):
            raise r
        return evaluation(r)
    return evaluate


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


@pytest.fixture(autouse=True)
def no_rate_limit(monkeypatch):
    monkeypatch.setattr(P, "SECONDS_BETWEEN_CALLS", 0)


def score(conn, postings, evaluate, stop=None):
    summary = P.RunSummary()
    events = []
    P.score_and_save(conn, postings, summary, events.append, stop or threading.Event(), evaluate)
    return summary, events


# ---- pure steps -------------------------------------------------------------
def test_to_posting_reads_adzuna_fields():
    p = P.to_posting(job(7))
    assert (p.job_id, p.company, p.location) == ("7", "Acme", "Detroit, MI")


def test_dedupe_drops_repeats_and_keeps_order():
    postings, dupes = P.dedupe([job(1), job(2), job(1)])
    assert [p.job_id for p in postings] == ["1", "2"]
    assert dupes == 1


def test_drop_seen():
    postings, skipped = P.drop_seen([P.to_posting(job(1)), P.to_posting(job(2))], {"1"})
    assert [p.job_id for p in postings] == ["2"] and skipped == 1


@pytest.mark.parametrize("title, description, kept", [
    ("Data Science Intern", "python", True),
    ("Barista", "coffee", False),                          # not technical
    ("Marketing Intern", "social media", False),           # intern, but not technical
    ("Senior Data Engineer", "mentor our interns", False),  # senior role
])
def test_split_prefilter(title, description, kept):
    keep, rejected = P.split_prefilter([P.to_posting(job(1, title, description))])
    assert bool(keep) == kept and bool(rejected) != kept


# ---- scoring loop -----------------------------------------------------------
def test_strong_match_saved_weak_score_only_recorded(conn):
    summary, events = score(conn, [P.to_posting(job(1)), P.to_posting(job(2))],
                            fake_evaluator([90, 40]))
    assert (summary.evaluated, summary.matches) == (2, 1)
    assert [p.job_id for p in db.list_postings(conn)] == ["1"]
    assert db.seen_job_ids(conn) == {"1", "2"}
    assert events[-1].done == 2 and events[-1].total == 2


def test_failed_evaluation_is_not_marked_seen(conn):
    summary, _ = score(conn, [P.to_posting(job(1))], fake_evaluator([ValueError("boom")]))
    assert summary.errors == 1
    assert "1" not in db.seen_job_ids(conn)


def test_per_run_cap_defers_the_rest(conn, monkeypatch):
    monkeypatch.setattr(P, "MAX_EVALS_PER_RUN", 2)
    summary, _ = score(conn, [P.to_posting(job(i)) for i in range(5)], fake_evaluator([90] * 5))
    assert (summary.evaluated, summary.deferred) == (2, 3)


def test_quota_exhausted_defers_the_rest(conn):
    summary, _ = score(conn, [P.to_posting(job(i)) for i in range(3)],
                       fake_evaluator([90, P.QuotaExhausted("429")]))
    assert (summary.evaluated, summary.deferred, summary.status) == (1, 2, "completed")


def test_stop_before_scoring_scores_nothing(conn):
    stop = threading.Event()
    stop.set()
    summary, _ = score(conn, [P.to_posting(job(1))], fake_evaluator([90]), stop)
    assert (summary.evaluated, summary.deferred, summary.status) == (0, 1, "stopped")
    assert db.seen_job_ids(conn) == set()


def test_stop_interrupts_the_rate_limit_wait(conn, monkeypatch):
    monkeypatch.setattr(P, "SECONDS_BETWEEN_CALLS", 30)
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    start = time.monotonic()
    summary, _ = score(conn, [P.to_posting(job(i)) for i in range(3)], fake_evaluator([90] * 3), stop)
    assert time.monotonic() - start < 2          # didn't sit through a 30 s wait
    assert (summary.evaluated, summary.deferred, summary.status) == (1, 2, "stopped")


def test_stop_interrupts_retry_backoff(conn, monkeypatch):
    class FakeAPIError(P.errors.APIError):
        def __init__(self):
            Exception.__init__(self, "unavailable")
            self.code = 503
    def always_503(*args):
        raise FakeAPIError()
    monkeypatch.setattr(P, "evaluate_job", always_503)
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    start = time.monotonic()
    summary, events = score(conn, [P.to_posting(job(1))], P.evaluate_with_retry, stop)
    assert time.monotonic() - start < 2          # didn't sit through the 10 s backoff
    assert summary.status == "stopped" and summary.deferred == 1
    assert any(e.kind == "retry" for e in events)
"""Tests for labeling.py, metrics.py, tracking.py and db migration 4.

No network, API keys or Gemini quota needed: Gemini is replaced by fakes.
"""
import random
import sqlite3
import threading
import time
import types

import pytest

import db
import labeling as L
import metrics as M
import tracking as T


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


def posting(i, title="Data Science Intern"):
    return db.NewPosting(str(i), title, f"Co{i}", "Detroit, MI", f"http://example.com/{i}",
                         f"snippet {i}")


def ev(is_match=True, score=90):
    return types.SimpleNamespace(is_match=is_match, fit_score=score, term="Summer 2027",
                                 matched_major="CS", key_strengths=["Python"], reasoning="r")


def scored(conn, i, is_match=True, score=90, model="m", version="v1", save=None):
    db.record_scored(conn, posting(i), ev(is_match, score), model=model, prompt_version=version,
                     save_as_match=is_match and score >= 75 if save is None else save)


def label(conn, i, snippet, full=None):
    L.submit_label(conn, str(i), L.SNIPPET, snippet)
    if full is not None:
        L.submit_label(conn, str(i), L.FULL, full)


def prefiltered(conn, ids):
    for i in ids:
        db.record_prefiltered(conn, posting(i, "Barista"))


# ---------------------------------------------------------------------------
# db migration 4
# ---------------------------------------------------------------------------
def test_migration_4_upgrades_a_version_3_database(tmp_path):
    path = tmp_path / "old.db"
    c = sqlite3.connect(path)
    for step in db._MIGRATIONS[:3]:  # build a database exactly as it was before this change
        step(c)
    c.execute("PRAGMA user_version = 3")
    c.execute("INSERT INTO seen_jobs (job_id, title, outcome) VALUES ('x', 't', 'prefiltered_out')")
    c.execute("INSERT INTO runs (model) VALUES ('m')")
    c.commit()
    c.close()

    conn = db.connect(path)
    assert db.schema_version(conn) == 4
    assert db.get_seen_job(conn, "x").audit_sampled_at is None
    assert db.list_runs(conn)[0].kind == "pipeline"  # existing runs default to pipeline
    conn.close()


# ---------------------------------------------------------------------------
# labeling: tasks and the two-step rule
# ---------------------------------------------------------------------------
def test_snippet_step_hides_url_and_full_step_shows_it(conn):
    scored(conn, 1)
    task = L.next_task(conn)
    assert (task.step, task.url, task.snippet) == (L.SNIPPET, None, "snippet 1")
    L.submit_label(conn, "1", L.SNIPPET, True)
    task = L.next_task(conn)
    assert (task.step, task.url) == (L.FULL, "http://example.com/1")
    L.submit_label(conn, "1", L.FULL, False)
    assert L.next_task(conn) is None


def test_full_label_requires_snippet_first(conn):
    scored(conn, 1)
    with pytest.raises(L.LabelingError, match="snippet first"):
        L.submit_label(conn, "1", L.FULL, True)


def test_snippet_locked_after_full_until_reset(conn):
    scored(conn, 1)
    label(conn, 1, True, False)
    with pytest.raises(L.LabelingError, match="can't be changed"):
        L.submit_label(conn, "1", L.SNIPPET, False)
    L.reset_labels(conn, "1")
    assert L.next_task(conn).step == L.SNIPPET


def test_snippet_can_be_corrected_before_full(conn):
    scored(conn, 1)
    L.submit_label(conn, "1", L.SNIPPET, True)
    L.submit_label(conn, "1", L.SNIPPET, False)
    assert db.get_seen_job(conn, "1").snippet_label is False


def test_undo(conn):
    scored(conn, 1)
    label(conn, 1, True, True)
    with pytest.raises(L.LabelingError):
        L.undo_label(conn, "1", L.SNIPPET)  # full exists
    L.undo_label(conn, "1", L.FULL)
    assert L.next_task(conn).step == L.FULL
    L.undo_label(conn, "1", L.SNIPPET)
    assert L.next_task(conn).step == L.SNIPPET


def test_skip_and_remaining(conn):
    for i in (1, 2, 3):
        scored(conn, i)
    first = L.next_task(conn)
    assert first.remaining == 3
    second = L.next_task(conn, skip={first.job_id})
    assert second.job_id != first.job_id and second.remaining == 2


def test_invalid_input(conn):
    scored(conn, 1)
    with pytest.raises(L.LabelingError):
        L.submit_label(conn, "1", "middle", True)
    with pytest.raises(L.LabelingError):
        L.submit_label(conn, "1", L.SNIPPET, "Valid")
    with pytest.raises(L.LabelingError):
        L.submit_label(conn, "nope", L.SNIPPET, True)
    with pytest.raises(L.LabelingError):
        L.next_task(conn, "other")


def test_progress(conn):
    for i in (1, 2, 3):
        scored(conn, i)
    label(conn, 1, True, True)
    label(conn, 2, False)
    p = L.progress(conn)
    assert (p.total, p.fully_labeled, p.snippet_only, p.unlabeled) == (3, 1, 1, 1)
    assert p.fraction_done == pytest.approx(1 / 3)


def test_get_task(conn):
    scored(conn, 1)
    assert L.get_task(conn, "1").step == L.SNIPPET
    label(conn, 1, True, True)
    assert L.get_task(conn, "1") is None


# ---------------------------------------------------------------------------
# labeling: prefilter audit
# ---------------------------------------------------------------------------
def test_audit_queue_is_separate_and_needs_sampling(conn):
    scored(conn, 1)
    prefiltered(conn, range(10, 20))
    assert L.next_task(conn, L.AUDIT) is None
    assert L.next_task(conn).job_id == "1"  # labeling queue never shows prefiltered postings
    with pytest.raises(L.LabelingError, match="audit sample"):
        L.submit_label(conn, "10", L.SNIPPET, True)  # not sampled: labeling it would bias


def test_draw_audit_sample_is_random_without_repeats(conn):
    prefiltered(conn, range(100, 150))
    assert L.draw_audit_sample(conn, 10, rng=random.Random(1)) == 10
    first = {j.job_id for j in db.audit_jobs(conn)}
    assert L.draw_audit_sample(conn, 10, rng=random.Random(1)) == 10
    all_sampled = {j.job_id for j in db.audit_jobs(conn)}
    assert len(all_sampled) == 20 and first < all_sampled  # nothing drawn twice
    assert sorted(first) != sorted(str(i) for i in range(100, 110))  # not just the first ten


def test_draw_more_than_available(conn):
    prefiltered(conn, range(3))
    assert L.draw_audit_sample(conn, 10) == 3
    assert L.draw_audit_sample(conn, 10) == 0
    with pytest.raises(L.LabelingError):
        L.draw_audit_sample(conn, 0)


def test_draw_within_days(conn):
    prefiltered(conn, range(4))
    conn.execute("UPDATE seen_jobs SET first_seen = datetime('now', '-60 days') "
                 "WHERE job_id IN ('0', '1')")
    conn.commit()
    assert L.audit_status(conn, within_days=30).available == 2
    assert L.draw_audit_sample(conn, 10, within_days=30) == 2
    assert {j.job_id for j in db.audit_jobs(conn)} == {"2", "3"}


def test_audit_labeling_flow_and_status(conn):
    prefiltered(conn, range(5))
    L.draw_audit_sample(conn, 2, rng=random.Random(0))
    task = L.next_task(conn, L.AUDIT)
    assert task.queue == L.AUDIT and task.url is None
    label(conn, int(task.job_id), False, True)
    s = L.audit_status(conn)
    assert (s.prefiltered_total, s.sampled, s.available, s.fully_labeled, s.remaining) == (5, 2, 3, 1, 1)
    assert L.progress(conn, L.AUDIT).fully_labeled == 1


# ---------------------------------------------------------------------------
# labeling: rescore
# ---------------------------------------------------------------------------
@pytest.fixture
def pipeline(monkeypatch):
    import Pipeline as P
    monkeypatch.setattr(P, "SECONDS_BETWEEN_CALLS", 0)
    monkeypatch.setattr(P, "MODEL_NAME", "m")
    monkeypatch.setattr(P, "PROMPT_VERSION", "v2")
    return P


def fake_evaluate(P, results):
    it = iter(results)
    def evaluate(posting, emit, stop):
        r = next(it)
        if isinstance(r, BaseException):
            raise r
        return P.JobEvaluation(is_match=r >= 75, fit_score=r, matched_major="CS",
                               term="Summer 2027", key_strengths=["Python"], reasoning="new")
    return evaluate


def test_rescore_scores_labeled_postings_as_a_new_version(conn, pipeline):
    for i in (1, 2, 3):
        scored(conn, i)          # scored by v1
    label(conn, 1, True, True)
    label(conn, 2, False)
    events = []
    s = L.rescore_labeled(conn, evaluate=fake_evaluate(pipeline, [80, 40]), on_progress=events.append)
    assert (s.status, s.labeled, s.to_score, s.scored) == ("completed", 2, 2, 2)
    assert db.scored_job_ids(conn, "m", "v2") == {"1", "2"}   # unlabeled #3 not rescored
    assert len(db.list_evaluations(conn, "1")) == 2           # v1 score kept alongside v2
    run = db.list_runs(conn, kind="rescore")[0]
    assert run.status == "completed" and run.stats["scored"] == 2
    assert events[-1].done == 2 and events[-1].total == 2


def test_rescore_never_touches_the_tracker(conn, pipeline):
    scored(conn, 1, is_match=False, score=40)  # not saved as a match
    label(conn, 1, True, True)
    L.rescore_labeled(conn, evaluate=fake_evaluate(pipeline, [95]))
    assert db.list_postings(conn, include_closed=True) == []


def test_rescore_only_missing_resumes(conn, pipeline):
    for i in (1, 2, 3):
        scored(conn, i)
        label(conn, i, True)
    s1 = L.rescore_labeled(conn, limit=2, evaluate=fake_evaluate(pipeline, [80, 80]))
    assert (s1.scored, s1.deferred) == (2, 1)
    s2 = L.rescore_labeled(conn, evaluate=fake_evaluate(pipeline, [80]))
    assert (s2.already_scored, s2.scored) == (2, 1)
    s3 = L.rescore_labeled(conn, only_missing=False, evaluate=fake_evaluate(pipeline, [70] * 3))
    assert s3.scored == 3  # forced full rescore replaces the v2 scores
    assert {e.fit_score for e in db.labeled_evaluations(conn, prompt_version="v2")} == {70}


def test_rescore_errors_and_quota(conn, pipeline):
    for i in (1, 2, 3):
        scored(conn, i)
        label(conn, i, True)
    s = L.rescore_labeled(conn, evaluate=fake_evaluate(
        pipeline, [ValueError("bad"), 80, pipeline.QuotaExhausted("429")]))
    assert (s.errors, s.scored, s.deferred, s.status) == (1, 1, 1, "completed")


def test_rescore_stop_interrupts_wait(conn, pipeline, monkeypatch):
    monkeypatch.setattr(pipeline, "SECONDS_BETWEEN_CALLS", 30)
    for i in (1, 2, 3):
        scored(conn, i)
        label(conn, i, True)
    stop = threading.Event()
    threading.Timer(0.3, stop.set).start()
    start = time.monotonic()
    s = L.rescore_labeled(conn, stop=stop, evaluate=fake_evaluate(pipeline, [80] * 3))
    assert time.monotonic() - start < 2
    assert (s.status, s.scored, s.deferred) == ("stopped", 1, 2)
    assert db.list_runs(conn, kind="rescore")[0].status == "stopped"


def test_rescore_needs_api_key(conn, pipeline, monkeypatch):
    monkeypatch.setattr(pipeline, "GEMINI_API_KEY", None)
    with pytest.raises(L.LabelingError, match="GEMINI_API_KEY"):
        L.rescore_labeled(conn)


# ---------------------------------------------------------------------------
# metrics: building blocks
# ---------------------------------------------------------------------------
def test_confusion_counts_and_rates():
    c = M.confusion([(True, True)] * 6 + [(True, False)] * 2 + [(False, True)] * 3 + [(False, False)] * 9)
    assert (c.tp, c.fp, c.fn, c.tn, c.n) == (6, 2, 3, 9, 20)
    assert c.precision.value == 6 / 8 and c.recall.value == 6 / 9
    assert c.accuracy.value == 15 / 20 and c.specificity.value == 9 / 11
    assert c.f1 == pytest.approx(2 * (6 / 8) * (6 / 9) / (6 / 8 + 6 / 9))
    assert str(c.precision) == "75% (6/8)"


def test_empty_rates_are_none_not_errors():
    c = M.confusion([(False, False)])
    assert c.precision.value is None and c.precision.interval is None
    assert c.f1 is None and str(c.precision) == "n/a (0)"
    assert M.confusion([(False, True)]).f1 is None    # precision undefined
    assert M.confusion([(True, False), (False, True)]).f1 == 0.0


def test_wilson_interval():
    low, high = M.wilson(25, 30)
    assert low < 25 / 30 < high and 0.6 < low and high < 0.95
    assert M.wilson(0, 10)[0] == 0.0 and M.wilson(10, 10)[1] == 1.0
    small, large = M.wilson(5, 10), M.wilson(50, 100)
    assert small[1] - small[0] > large[1] - large[0]  # fewer labels = wider range


def le(job, pred, score, snippet, full, model="m", version="v1"):
    return db.LabeledEvaluation(str(job), "t", "c", model, version, pred, score, snippet, full)


def test_version_metrics_comparisons_and_error_breakdown():
    evals = [
        le(1, True, 90, True, True),     # right
        le(2, True, 80, True, False),    # wrong; snippet said yes too -> snippet-limited
        le(3, False, 30, True, True),    # wrong; snippet said yes -> model error
        le(4, True, 60, None, False),    # wrong; no snippet label -> unknown
        le(5, False, 20, False, None),   # no full label: only in vs_snippet
    ]
    vm = M.version_metrics(evals, model="m", prompt_version="v1", threshold=75)
    assert (vm.vs_full.tp, vm.vs_full.fp, vm.vs_full.fn, vm.vs_full.tn) == (1, 2, 1, 0)
    assert vm.vs_snippet.n == 4
    assert vm.errors == M.ErrorBreakdown(total=3, snippet_limited=1, model_errors=1, unknown=1)
    assert {i.job_id for i in vm.false_positives} == {"2", "4"}
    assert [i.job_id for i in vm.false_negatives] == ["3"]
    # save rule: #4 is_match but scored 60 < 75, so not saved -> correct negative
    assert (vm.saved_vs_full.tp, vm.saved_vs_full.fp, vm.saved_vs_full.tn) == (1, 1, 1)


def test_threshold_sweep_trades_recall_for_precision():
    evals = [le(i, True, s, None, f) for i, (s, f) in
             enumerate([(95, True), (85, True), (78, False), (65, True), (55, False), (40, False)])]
    vm = M.version_metrics(evals, model="m", prompt_version="v1")
    by_t = {p.threshold: p.confusion for p in vm.sweep}
    assert by_t[0].recall.value == 1.0 and by_t[0].precision.value == 0.5
    assert by_t[80].precision.value == 1.0 and by_t[80].recall.value == pytest.approx(2 / 3)
    assert by_t[100].tp == 0
    recalls = [p.confusion.recall.value for p in vm.sweep]
    assert recalls == sorted(recalls, reverse=True)  # raising the bar never adds matches


def test_audit_metrics_estimate():
    jobs = [db.SeenJob(i, str(i), "t", "c", None, None, None, "prefiltered_out", None,
                       snippet, full, "2026-11-01")
            for i, (snippet, full) in enumerate([(False, True), (False, False), (False, False),
                                                 (True, False), (None, None)])]
    a = M.audit_metrics(jobs, prefiltered_total=200)
    assert (a.sampled, a.labeled, a.miss_rate.k, a.miss_rate.n) == (5, 4, 1, 4)
    low, best, high = a.estimated_missed
    assert best == 50 and low < best < high
    assert [m.job_id for m in a.missed] == ["0"]
    assert M.audit_metrics([], 10).estimated_missed is None


# ---------------------------------------------------------------------------
# metrics: reports from the database
# ---------------------------------------------------------------------------
def test_build_report(conn):
    scored(conn, 1, True, 90)
    scored(conn, 2, True, 80)
    scored(conn, 3, False, 30)
    label(conn, 1, True, True)
    label(conn, 2, True, False)
    label(conn, 3, False, True)
    prefiltered(conn, range(10, 14))
    L.draw_audit_sample(conn, 2, rng=random.Random(0))
    for j in db.audit_jobs(conn):
        label(conn, int(j.job_id), False, False)

    r = M.build_report(conn, threshold=75)
    assert len(r.versions) == 1 and r.versions[0].version == ("m", "v1")
    assert r.versions[0].vs_full.accuracy.k == 1
    assert (r.snippet_vs_full.tp, r.snippet_vs_full.fp, r.snippet_vs_full.fn) == (1, 1, 1)
    assert (r.audit.prefiltered_total, r.audit.labeled, r.audit.miss_rate.k) == (4, 2, 0)
    assert r.label_progress["fully_labeled"] == 3


def test_scored_audit_postings_dont_leak_into_model_metrics(conn):
    """When option C later scores audited postings, they must stay out of model metrics."""
    scored(conn, 1)
    label(conn, 1, True, True)
    prefiltered(conn, [10])
    L.draw_audit_sample(conn, 1)
    label(conn, 10, True, True)
    db.record_evaluation(conn, "10", ev(False, 10), model="m", prompt_version="v1")
    r = M.build_report(conn)
    assert r.versions[0].scored_and_labeled == 1


def test_compare_versions_uses_only_shared_postings(conn):
    for i, v1 in [(1, True), (2, True), (3, False), (4, True)]:
        scored(conn, i, v1, 90 if v1 else 30)
        label(conn, i, True, i != 2)   # #2 is really not a match
    for i, v2 in [(1, True), (2, False), (3, True)]:  # v2 hasn't scored #4
        db.record_evaluation(conn, str(i), ev(v2, 90 if v2 else 30), model="m", prompt_version="v2")

    c = M.compare_versions(conn, ("m", "v1"), ("m", "v2"))
    assert (c.shared, c.only_a, c.only_b) == (3, 1, 0)
    assert c.a.vs_full.n == c.b.vs_full.n == 3
    assert c.a.vs_full.accuracy.k == 1 and c.b.vs_full.accuracy.k == 3
    assert {(d.item.job_id, d.right) for d in c.disagreements} == {("2", "b"), ("3", "b")}


# ---------------------------------------------------------------------------
# tracking
# ---------------------------------------------------------------------------
def test_summary_and_response_rate(conn):
    assert T.summary(conn).response_rate is None
    for i in range(1, 6):
        scored(conn, i)
    T.set_status(conn, ["1", "2"], db.APPLIED)
    T.set_status(conn, ["3"], db.INTERVIEWING)
    T.set_status(conn, ["4"], db.NOT_INTERESTED)
    s = T.summary(conn)
    assert (s.total, s.open, s.unreviewed, s.applied, s.responses) == (5, 4, 1, 3, 1)
    assert s.response_rate == pytest.approx(1 / 3)


def test_review_flow(conn):
    scored(conn, 1, score=80)
    scored(conn, 2, score=95)
    item = T.next_for_review(conn)
    assert item.posting.job_id == "2" and item.remaining == 2   # best fit first
    assert T.next_for_review(conn, skip={"2"}).posting.job_id == "1"
    p = T.mark_applied(conn, "2", note="Applied on site")
    assert p.status == db.APPLIED and p.applied_date
    T.mark_not_interested(conn, "1")
    assert T.next_for_review(conn) is None
    assert [n.body for n in T.posting_detail(conn, "2").notes] == ["Applied on site"]


def test_posting_detail_has_score_history(conn):
    scored(conn, 1)
    db.record_evaluation(conn, "1", ev(True, 70), model="m", prompt_version="v2")
    d = T.posting_detail(conn, "1")
    assert {e.prompt_version for e in d.evaluations} == {"v1", "v2"}


def test_tracking_errors(conn):
    with pytest.raises(T.TrackingError):
        T.mark_applied(conn, "nope")
    with pytest.raises(T.TrackingError):
        T.set_status(conn, ["1"], "applied")
    scored(conn, 1)
    with pytest.raises(T.TrackingError):
        T.add_note(conn, "1", "   ")

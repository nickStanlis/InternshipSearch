"""How well Gemini's decisions agree with your labels.

Ground truth is your FULL-posting label. Three comparisons:

  model vs full      End-to-end accuracy: what actually matters.
  model vs snippet   Error given the same information you had (the snippet).
  snippet vs full    Your own snippet call vs your full call: how much the
                     ~500-character snippet loses. No model working from the
                     snippet can reliably beat this.

The model's "yes" is measured two ways:
  is_match           Gemini's own decision.
  saved              is_match AND fit_score >= threshold, i.e. what the pipeline
                     actually saves to your tracker. The threshold sweep shows how
                     precision and recall change as minimum_fit_score_to_save moves.

Every rate carries its counts (k of n) and a 95% Wilson interval, because with a
few dozen labels a percentage alone looks more certain than it is.

Everything is computed from data already stored (scores and labels), so nothing
here writes to the database. Pure functions take plain lists so they're easy to
test; build_report / compare_versions read the database for the UI.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable

import db

DEFAULT_THRESHOLD = 75
SWEEP_THRESHOLDS = tuple(range(0, 101, 5))

Version = tuple[str, str]  # (model, prompt_version)


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------
def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% Wilson score interval for k successes in n trials (None when n == 0).
    Behaves well for small n and for rates near 0% or 100%, unlike k/n ± error."""
    if n == 0:
        return None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


@dataclass(frozen=True, slots=True)
class Rate:
    """k out of n, e.g. precision = 25 of 30 predicted matches were real."""
    k: int
    n: int

    @property
    def value(self) -> float | None:
        return self.k / self.n if self.n else None

    @property
    def interval(self) -> tuple[float, float] | None:
        return wilson(self.k, self.n)

    def __str__(self) -> str:
        return "n/a (0)" if not self.n else f"{self.value:.0%} ({self.k}/{self.n})"


@dataclass(frozen=True, slots=True)
class Confusion:
    """Counts for predicted-vs-actual, with 'match' as the positive class."""
    tp: int = 0  # predicted match, really a match
    fp: int = 0  # predicted match, not a match      (wasted review time)
    fn: int = 0  # predicted no match, really a match (missed internship)
    tn: int = 0  # predicted no match, not a match

    @property
    def n(self) -> int:
        return self.tp + self.fp + self.fn + self.tn

    @property
    def precision(self) -> Rate:
        """Of postings predicted to match, how many really did."""
        return Rate(self.tp, self.tp + self.fp)

    @property
    def recall(self) -> Rate:
        """Of postings that really match, how many were caught."""
        return Rate(self.tp, self.tp + self.fn)

    @property
    def specificity(self) -> Rate:
        """Of postings that don't match, how many were correctly turned down."""
        return Rate(self.tn, self.tn + self.fp)

    @property
    def accuracy(self) -> Rate:
        return Rate(self.tp + self.tn, self.n)

    @property
    def f1(self) -> float | None:
        p, r = self.precision.value, self.recall.value
        if not p or not r:
            return None if p is None or r is None else 0.0
        return 2 * p * r / (p + r)


def confusion(pairs: Iterable[tuple[bool, bool]]) -> Confusion:
    """Build a confusion matrix from (predicted, actual) pairs."""
    tp = fp = fn = tn = 0
    for predicted, actual in pairs:
        if predicted and actual:
            tp += 1
        elif predicted:
            fp += 1
        elif actual:
            fn += 1
        else:
            tn += 1
    return Confusion(tp, fp, fn, tn)


def is_saved(e: db.LabeledEvaluation, threshold: int) -> bool:
    """The pipeline's save rule: Gemini says match AND the score clears the threshold."""
    return e.predicted_match and (e.fit_score or 0) >= threshold


# ---------------------------------------------------------------------------
# Per-version metrics
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ThresholdPoint:
    threshold: int
    confusion: Confusion


@dataclass(frozen=True, slots=True)
class ErrorBreakdown:
    """Model mistakes (vs your full label), split by cause.

    snippet_limited: from the snippet you made the same wrong call, so the
                     information wasn't there. A better prompt can't fix these;
                     a longer description could.
    model_errors:    from the same snippet you got it right, so the model erred.
                     This is what prompt changes can fix.
    unknown:         no snippet label to compare with.
    """
    total: int
    snippet_limited: int
    model_errors: int
    unknown: int


@dataclass(frozen=True, slots=True)
class LabeledItem:
    """One posting in an error list, for the UI to show and link to."""
    job_id: str
    title: str
    company: str
    predicted_match: bool
    fit_score: int | None
    snippet_label: bool | None
    full_label: bool | None


@dataclass(frozen=True, slots=True)
class VersionMetrics:
    model: str
    prompt_version: str
    threshold: int
    scored_and_labeled: int           # postings with a score from this version and any label
    vs_full: Confusion                 # is_match vs your full label (headline)
    vs_snippet: Confusion              # is_match vs your snippet label
    saved_vs_full: Confusion           # save rule at `threshold` vs your full label
    sweep: tuple[ThresholdPoint, ...]  # save rule at every threshold in SWEEP_THRESHOLDS
    errors: ErrorBreakdown             # breakdown of vs_full mistakes
    false_positives: tuple[LabeledItem, ...]  # predicted match, full label says no
    false_negatives: tuple[LabeledItem, ...]  # predicted no match, full label says yes

    @property
    def version(self) -> Version:
        return self.model, self.prompt_version


def _item(e: db.LabeledEvaluation) -> LabeledItem:
    return LabeledItem(e.job_id, e.title, e.company, e.predicted_match, e.fit_score,
                       e.snippet_label, e.full_label)


def version_metrics(evals: list[db.LabeledEvaluation], *, model: str, prompt_version: str,
                    threshold: int = DEFAULT_THRESHOLD,
                    sweep_thresholds: Iterable[int] = SWEEP_THRESHOLDS) -> VersionMetrics:
    """Metrics for one model + prompt version, from its labeled evaluations."""
    full = [e for e in evals if e.full_label is not None]
    snippet = [e for e in evals if e.snippet_label is not None]
    wrong = [e for e in full if e.predicted_match != e.full_label]

    limited = sum(1 for e in wrong if e.snippet_label is not None
                  and e.snippet_label == e.predicted_match)
    unknown = sum(1 for e in wrong if e.snippet_label is None)

    return VersionMetrics(
        model=model, prompt_version=prompt_version, threshold=threshold,
        scored_and_labeled=len(evals),
        vs_full=confusion((e.predicted_match, e.full_label) for e in full),
        vs_snippet=confusion((e.predicted_match, e.snippet_label) for e in snippet),
        saved_vs_full=confusion((is_saved(e, threshold), e.full_label) for e in full),
        sweep=tuple(ThresholdPoint(t, confusion((is_saved(e, t), e.full_label) for e in full))
                    for t in sweep_thresholds),
        errors=ErrorBreakdown(total=len(wrong), snippet_limited=limited,
                              model_errors=len(wrong) - limited - unknown, unknown=unknown),
        false_positives=tuple(_item(e) for e in wrong if e.predicted_match),
        false_negatives=tuple(_item(e) for e in wrong if not e.predicted_match),
    )


def snippet_vs_full(jobs: Iterable[db.SeenJob]) -> Confusion:
    """Your snippet label treated as a prediction of your full label."""
    return confusion((j.snippet_label, j.full_label) for j in jobs
                     if j.snippet_label is not None and j.full_label is not None)


# ---------------------------------------------------------------------------
# Prefilter audit
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class AuditMetrics:
    """How many good postings the regex prefilter throws away, from the random sample."""
    prefiltered_total: int
    sampled: int
    labeled: int                    # sampled postings with a full label
    miss_rate: Rate                 # of labeled sampled postings, how many were good
    estimated_missed: tuple[float, float, float] | None  # (low, best, high) good postings
                                                          # lost across ALL prefiltered ones
    snippet_vs_full: Confusion      # same snippet-vs-full check, on the audit sample
    missed: tuple[LabeledItem, ...] # the good ones, to see what the regex got wrong


def audit_metrics(jobs: list[db.SeenJob], prefiltered_total: int) -> AuditMetrics:
    labeled = [j for j in jobs if j.full_label is not None]
    rate = Rate(sum(1 for j in labeled if j.full_label), len(labeled))
    estimate = None
    if rate.n:
        low, high = rate.interval
        estimate = (low * prefiltered_total, rate.value * prefiltered_total,
                    high * prefiltered_total)
    return AuditMetrics(
        prefiltered_total=prefiltered_total, sampled=len(jobs), labeled=len(labeled),
        miss_rate=rate, estimated_missed=estimate, snippet_vs_full=snippet_vs_full(jobs),
        missed=tuple(LabeledItem(j.job_id, j.title, j.company, False, None,
                                 j.snippet_label, j.full_label)
                     for j in labeled if j.full_label),
    )


# ---------------------------------------------------------------------------
# Reports for the UI
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class MetricsReport:
    threshold: int
    versions: tuple[VersionMetrics, ...]  # most recently used version first
    snippet_vs_full: Confusion            # over all labeled postings Gemini scored
    audit: AuditMetrics
    label_progress: dict = field(default_factory=dict)  # db.label_counts for the labeling queue


def _grouped(evals: list[db.LabeledEvaluation]) -> dict[Version, list[db.LabeledEvaluation]]:
    groups: dict[Version, list[db.LabeledEvaluation]] = {}
    for e in evals:
        groups.setdefault((e.model, e.prompt_version), []).append(e)
    return groups


def build_report(conn, threshold: int = DEFAULT_THRESHOLD) -> MetricsReport:
    """Everything for the metrics page in one call.

    threshold: the save cutoff to report (pass your minimum_fit_score_to_save).
    """
    groups = _grouped(db.labeled_evaluations(conn))
    order = [(m, v) for m, v, _ in db.evaluation_versions(conn)]
    versions = tuple(version_metrics(groups[key], model=key[0], prompt_version=key[1],
                                     threshold=threshold)
                     for key in order if key in groups)
    return MetricsReport(
        threshold=threshold,
        versions=versions,
        snippet_vs_full=snippet_vs_full(db.labeled_jobs(conn)),
        audit=audit_metrics(db.audit_jobs(conn), db.prefiltered_count(conn)),
        label_progress=db.label_counts(conn),
    )


@dataclass(frozen=True, slots=True)
class Disagreement:
    """A posting the two versions decided differently, and who was right."""
    item: LabeledItem   # labels, with predicted_match/fit_score from version a
    a_match: bool
    b_match: bool
    b_fit_score: int | None

    @property
    def right(self) -> str | None:
        """'a', 'b', or None if there's no full label to judge by."""
        if self.item.full_label is None:
            return None
        return "a" if self.a_match == self.item.full_label else "b"


@dataclass(frozen=True, slots=True)
class VersionComparison:
    """Two versions measured on exactly the same postings, so the comparison is fair."""
    shared: int
    only_a: int            # labeled postings only version a has scored
    only_b: int            # (rescore the other version to include them)
    a: VersionMetrics
    b: VersionMetrics
    disagreements: tuple[Disagreement, ...]


def compare_versions(conn, a: Version, b: Version,
                     threshold: int = DEFAULT_THRESHOLD) -> VersionComparison:
    """Compare two model + prompt versions on the labeled postings both have scored."""
    ea = {e.job_id: e for e in db.labeled_evaluations(conn, model=a[0], prompt_version=a[1])}
    eb = {e.job_id: e for e in db.labeled_evaluations(conn, model=b[0], prompt_version=b[1])}
    shared = sorted(ea.keys() & eb.keys())
    sa, sb = [ea[j] for j in shared], [eb[j] for j in shared]
    return VersionComparison(
        shared=len(shared), only_a=len(ea.keys() - eb.keys()), only_b=len(eb.keys() - ea.keys()),
        a=version_metrics(sa, model=a[0], prompt_version=a[1], threshold=threshold),
        b=version_metrics(sb, model=b[0], prompt_version=b[1], threshold=threshold),
        disagreements=tuple(
            Disagreement(_item(x), x.predicted_match, y.predicted_match, y.fit_score)
            for x, y in zip(sa, sb) if x.predicted_match != y.predicted_match),
    )

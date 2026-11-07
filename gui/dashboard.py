"""Home page: what needs your attention, where applications stand, and the last run."""
from __future__ import annotations

from urllib.parse import quote

from nicegui import PageArguments, ui

import db
import labeling
import tracking
from gui.components import local_time, plural, word
from gui.data import conn
from gui.security import redact

# The application stages, in order. Closed outcomes are shown after the sequence.
STAGES = [db.UNREVIEWED, db.APPLIED, db.INTERVIEWING, db.OFFER]


def page(args: PageArguments) -> None:
    ui.label("Home").classes("t-title")
    ui.label("What's waiting for you, and how your applications are going.").classes("t-soft q-mb-lg")
    content()


@ui.refreshable
def content() -> None:
    c = conn()
    tracker = tracking.summary(c)
    label_progress = labeling.progress(c, labeling.LABELING)
    audit = labeling.audit_status(c)
    runs = db.list_runs(c, limit=1)

    # --- What needs you -----------------------------------------------------
    ui.label("Waiting for you").classes("t-h2 q-mb-sm")
    to_label = label_progress.total - label_progress.fully_labeled
    rows = [
        (to_label, word(to_label, "scored posting") + " to label", "Open labeling", "/label"),
        (audit.remaining, word(audit.remaining, "audit posting") + " to label",
         "Open audit", "/label?tab=audit"),
        (tracker.unreviewed, word(tracker.unreviewed, "new match", "new matches") + " to review",
         "Open review", "/review"),
    ]
    with ui.element("div").classes("panel w-full q-mb-xl"):
        waiting = [r for r in rows if r[0]]
        if not waiting:
            ui.label("Nothing is waiting. New postings arrive after the next pipeline run.") \
                .classes("t-soft")
        for count, text, button, target in waiting:
            with ui.row().classes("todo-row w-full items-center no-wrap"):
                ui.label(str(count)).classes("todo-count")
                ui.label(text).classes("col")
                ui.button(button, on_click=lambda t=target: ui.navigate.to(t)) \
                    .props("unelevated no-caps")

    # --- Applications ---------------------------------------------------------
    ui.label("Applications").classes("t-h2 q-mb-sm")
    with ui.element("div").classes("panel w-full q-mb-xl"):
        if tracker.total == 0:
            ui.label("No saved matches yet. Gemini saves postings that score above your threshold.") \
                .classes("t-soft")
        else:
            with ui.row().classes("w-full no-wrap gap-0 stages"):
                for status in STAGES:
                    with ui.link(target=f"/tracker?status={quote(status)}").classes("stage"):
                        ui.label(str(tracker.counts.get(status, 0))).classes("stage-count")
                        ui.label(status).classes("t-soft")
            closed = ", ".join(f"{tracker.counts[s]} {s.lower()}"
                               for s in db.CLOSED_STATUSES if tracker.counts.get(s))
            if closed:
                ui.label(f"Closed: {closed}.").classes("t-small q-mt-sm")
            if tracker.response_rate is None:
                ui.label("No applications sent yet.").classes("q-mt-md")
            else:
                ui.label(f"{tracker.responses} of {plural(tracker.applied, 'application')} "
                         f"got a response ({tracker.response_rate:.0%}).").classes("q-mt-md")

    # --- Last run -------------------------------------------------------------
    ui.label("Last run").classes("t-h2 q-mb-sm")
    with ui.element("div").classes("panel w-full"):
        if not runs:
            ui.label("The pipeline hasn't run yet. For now, run it from the terminal with "
                     "python Pipeline.py; the Runs page will replace that.").classes("t-soft measure")
        else:
            run = runs[0]
            kind = "Pipeline run" if run.kind == "pipeline" else "Rescore"
            when = local_time(run.finished_at or run.started_at)
            ui.label(f"{kind} #{run.id}: {run.status}, {when}.")
            s = run.stats
            if run.kind == "pipeline" and s:
                ui.label(f"Fetched {s.get('fetched', 0)}, prefiltered out {s.get('prefiltered_out', 0)}, "
                         f"scored {s.get('evaluated', 0)}, saved {plural(s.get('matches', 0), 'match', 'matches')}."
                         ).classes("t-soft")
            elif s:
                ui.label(f"Scored {s.get('scored', 0)} of {s.get('to_score', 0)}.").classes("t-soft")
            if run.error:
                ui.label(f"Error: {redact(run.error)[:300]}").classes("t-small measure")

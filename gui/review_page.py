"""Review page: new matches one at a time, best fit first.

Unlike labeling, review isn't blind: Gemini's score and reasoning are shown to
help you decide. Decisions go straight into the tracker.

Keys: A applied, N not interested, S decide later, U undo, O open the posting.
"""
from __future__ import annotations

from dataclasses import dataclass

from nicegui import PageArguments, ui

import tracking
from gui.components import (external_link, keycap, open_in_new_tab, plural, posting_card,
                            run_action, shortcuts)
from gui.data import conn


def page(args: PageArguments) -> None:
    ui.label("Review").classes("t-title")
    ui.label("New matches Gemini saved, best fit first. Decide whether each one is worth "
             "applying to; your decision goes to the tracker.").classes("t-soft measure q-mb-lg")
    panel = ReviewPanel()
    shortcuts({"a": panel.applied, "n": panel.not_interested, "s": panel.skip,
               "u": panel.undo, "o": panel.open_posting})


@dataclass
class Decision:
    """What Undo needs to put a posting back exactly as it was."""
    before: tracking.StatusSnapshot
    title: str
    note_id: int | None = None


class ReviewPanel:
    def __init__(self):
        self.skipped: set[str] = set()   # this visit only
        self.history: list[Decision] = []
        self.message = ""
        self.item: tracking.ReviewItem | None = None
        self.note_box: ui.textarea | None = None
        self.view = ui.column().classes("w-full gap-0")
        self.reload()

    # --- actions --------------------------------------------------------------
    def reload(self, prefer: str | None = None) -> None:
        _, self.item = run_action(tracking.next_for_review, conn(), self.skipped, prefer)
        self.render()

    def applied(self) -> None:
        item = self.item
        if item is None:
            return
        p = item.posting
        ok, before = run_action(tracking.snapshot, conn(), p.job_id)
        if not ok:
            return
        note_text = (self.note_box.value if self.note_box else "") or ""
        note_id = None
        if note_text.strip():  # add the note first, so an invalid note changes nothing
            ok, note = run_action(tracking.add_note, conn(), p.job_id, note_text)
            if not ok:
                return
            note_id = note.id
        ok, _ = run_action(tracking.mark_applied, conn(), p.job_id)
        if not ok:
            if note_id is not None:
                run_action(tracking.delete_note, conn(), p.job_id, note_id)
            return
        self.history.append(Decision(before, p.title, note_id))
        self.message = f"Marked {p.title} as Applied."
        self.reload()

    def not_interested(self) -> None:
        item = self.item
        if item is None:
            return
        p = item.posting
        ok, before = run_action(tracking.snapshot, conn(), p.job_id)
        if ok and run_action(tracking.mark_not_interested, conn(), p.job_id)[0]:
            self.history.append(Decision(before, p.title))
            self.message = f"Marked {p.title} as Not Interested."
        self.reload()

    def skip(self) -> None:
        if self.item is None:
            return
        self.skipped.add(self.item.posting.job_id)
        self.message = f"Left {self.item.posting.title} for later. It comes back next time."
        self.reload()

    def undo(self) -> None:
        if not self.history:
            ui.notify("Nothing to undo yet.")
            return
        d = self.history[-1]
        ok, _ = run_action(tracking.restore, conn(), d.before)
        if not ok:
            return
        if d.note_id is not None:
            run_action(tracking.delete_note, conn(), d.before.job_id, d.note_id)
        self.history.pop()
        self.skipped.discard(d.before.job_id)
        self.message = f"Undid the decision on {d.title}. It's back to {d.before.status}."
        self.reload(prefer=d.before.job_id)

    def unskip(self) -> None:
        self.skipped.clear()
        self.message = ""
        self.reload()

    def open_posting(self) -> None:
        if self.item is not None:
            open_in_new_tab(self.item.posting.url)

    # --- view -----------------------------------------------------------------
    def render(self) -> None:
        self.view.clear()
        self.note_box = None
        with self.view:
            if self.item is None:
                self._empty()
            else:
                self._item(self.item)
            if self.message:
                ui.label(self.message).classes("t-small q-mt-md").props('role="status" aria-live="polite"')

    def _empty(self) -> None:
        with ui.element("div").classes("panel w-full measure"):
            if self.skipped:
                ui.label(f"You've left the remaining {plural(len(self.skipped), 'match', 'matches')} "
                         f"for later.")
                ui.button("Show them again", on_click=self.unskip) \
                    .props("unelevated no-caps").classes("q-mt-md")
            else:
                ui.label("No new matches to review. New ones arrive after the next pipeline run.")
        if self.history:
            with ui.row().classes("q-mt-md"):
                keycap("U", "Undo last decision", self.undo)

    def _item(self, item: tracking.ReviewItem) -> None:
        p = item.posting
        ui.label(f"{plural(item.remaining, 'match', 'matches')} left to review").classes("t-small q-mb-sm")
        posting_card(p.title, p.company, p.location, item.snippet or "(no snippet stored)")

        with ui.element("div").classes("gemini-note w-full measure q-mt-md"):
            score = f"{p.fit_score} of 100" if p.fit_score is not None else "no score"
            ui.label(f"Gemini's fit score: {score}").classes("t-h2")
            facts = [f"Term: {p.term}" if p.term else "",
                     f"Matched major: {p.matched_major}" if p.matched_major else "",
                     f"Strengths: {', '.join(p.key_strengths)}" if p.key_strengths else ""]
            for fact in filter(None, facts):
                ui.label(fact).classes("t-soft")
            if p.reasoning:
                ui.label(p.reasoning).classes("detail-text q-mt-sm")

        with ui.row().classes("items-center gap-3 q-mt-md"):
            keycap("O", "Open the posting", self.open_posting)
            external_link("or use this link", p.url)

        self.note_box = ui.textarea("Note (optional, saved when you mark Applied)") \
            .props(f"outlined autogrow maxlength={tracking.MAX_NOTE_LENGTH}").classes("w-full measure q-mt-lg")

        with ui.row().classes("gap-4 q-mt-md w-full"):
            keycap("A", "Applied", self.applied, tone="match")
            keycap("N", "Not interested", self.not_interested, tone="no-match")
        with ui.row().classes("gap-3 q-mt-md items-center"):
            keycap("S", "Decide later", self.skip)
            undo = keycap("U", "Undo", self.undo)
            if not self.history:
                undo.disable()

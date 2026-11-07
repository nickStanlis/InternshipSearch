"""Labeling page: hand labels for Gemini-scored postings and for the prefilter audit.

The rules that keep the labels honest are enforced by labeling.py; this page
only shows what it's given:
  - Blind: LabelTask never carries Gemini's decision, so it can't be shown.
  - Snippet first: task.url is None during the snippet step, so there's no link.
  - The snippet label is locked once the full label exists (labeling.py raises).
  - The audit tab only labels postings drawn at random (labeling.py raises otherwise).

Keys: Y match, N not a match, S skip, U undo, O open the full posting (full step only).
"""
from __future__ import annotations

from nicegui import PageArguments, ui
from nicegui.events import KeyEventArguments

import labeling
from labeling import AUDIT, FULL, LABELING, SNIPPET
from gui.components import external_link, keycap, open_in_new_tab, plural, posting_card, run_action
from gui.data import conn

MAX_DRAW = 200
WINDOWS = {14: "Last 14 days", 30: "Last 30 days", 0: "Any age"}  # 0 = no limit

INSTRUCTIONS = {
    SNIPPET: "Judge from the snippet alone, the same text Gemini sees: is this a posting you'd want?",
    FULL: "Now open the full posting and decide again. This answer is the ground truth.",
}


def page(args: PageArguments) -> None:
    ui.label("Labeling").classes("t-title")
    ui.label("Label each posting twice, first from the snippet and then from the full posting. "
             "Gemini's decision stays hidden so your labels aren't influenced by it.") \
        .classes("t-soft measure q-mb-md")

    start = AUDIT if args.query_parameters.get("tab") == "audit" else LABELING
    with ui.tabs(value=start).props("no-caps align=left inline-label").classes("q-mb-md") as tabs:
        ui.tab(LABELING, "Scored by Gemini")
        ui.tab(AUDIT, "Prefilter audit")

    panels: dict[str, LabelingPanel] = {}
    with ui.tab_panels(tabs, value=start).classes("w-full bg-transparent").props("animated=false"):
        with ui.tab_panel(LABELING).classes("q-pa-none"):
            panels[LABELING] = LabelingPanel(LABELING)
        with ui.tab_panel(AUDIT).classes("q-pa-none"):
            AuditControls(on_draw=lambda: panels[AUDIT].reload())
            panels[AUDIT] = LabelingPanel(AUDIT)

    def on_key(e: KeyEventArguments) -> None:
        current = tabs.value if isinstance(tabs.value, str) else getattr(tabs.value, "_props", {}).get("name")
        panels[current or LABELING].handle_key(e)

    # 'button' is left out of ignore so shortcuts keep working after a mouse click.
    ui.keyboard(on_key=on_key, ignore=["input", "select", "textarea"])


class LabelingPanel:
    """One queue: the current posting, the decision keys, undo and skip."""

    def __init__(self, queue: str):
        self.queue = queue
        self.skipped: set[str] = set()          # this visit only; they come back next time
        self.history: list[tuple[str, str, str]] = []  # (job_id, step, title) for Undo
        self.message = ""
        self.task: labeling.LabelTask | None = None
        self.view = ui.column().classes("w-full gap-0")
        self.reload()

    # --- actions --------------------------------------------------------------
    def reload(self) -> None:
        _, self.task = run_action(labeling.next_task, conn(), self.queue, self.skipped)
        self.render()

    def submit(self, is_match: bool) -> None:
        task = self.task
        if task is None:
            return
        ok, _ = run_action(labeling.submit_label, conn(), task.job_id, task.step, is_match)
        if ok:
            self.history.append((task.job_id, task.step, task.title))
            step = "snippet" if task.step == SNIPPET else "full posting"
            self.message = f"Saved {step} label for {task.title}: {'match' if is_match else 'not a match'}."
        self.reload()

    def skip(self) -> None:
        if self.task is None:
            return
        self.skipped.add(self.task.job_id)
        self.message = f"Skipped {self.task.title}. It comes back next time you open this page."
        self.reload()

    def undo(self) -> None:
        if not self.history:
            ui.notify("Nothing to undo yet.")
            return
        job_id, step, title = self.history[-1]
        ok, _ = run_action(labeling.undo_label, conn(), job_id, step)
        if not ok:
            return
        self.history.pop()
        self.skipped.discard(job_id)
        _, self.task = run_action(labeling.get_task, conn(), job_id)
        self.message = f"Undid the {'snippet' if step == SNIPPET else 'full posting'} label for {title}."
        if self.task is None:
            self.reload()
        else:
            self.render()

    def unskip(self) -> None:
        self.skipped.clear()
        self.message = ""
        self.reload()

    def open_posting(self) -> None:
        if self.task is not None and self.task.step == FULL:
            open_in_new_tab(self.task.url)

    def handle_key(self, e: KeyEventArguments) -> None:
        if not e.action.keydown or e.action.repeat:
            return
        if e.modifiers.ctrl or e.modifiers.meta or e.modifiers.alt:
            return  # leave browser shortcuts like Ctrl+U alone
        actions = {"y": lambda: self.submit(True), "n": lambda: self.submit(False),
                   "s": self.skip, "u": self.undo, "o": self.open_posting}
        action = actions.get(str(e.key.name).lower())
        if action:
            action()

    # --- view -----------------------------------------------------------------
    def render(self) -> None:
        self.view.clear()
        with self.view:
            self._progress()
            if self.task is None:
                self._empty()
            else:
                self._task(self.task)
            if self.message:
                ui.label(self.message).classes("t-small q-mt-md").props('role="status" aria-live="polite"')

    def _progress(self) -> None:
        _, p = run_action(labeling.progress, conn(), self.queue)
        if p is None or p.total == 0:
            return
        line = f"{p.fully_labeled} of {plural(p.total, 'posting')} fully labeled."
        if p.snippet_only:
            line += f" {p.snippet_only} still {'needs' if p.snippet_only == 1 else 'need'} the full-posting label."
        ui.label(line).classes("t-soft")
        ui.linear_progress(value=p.fraction_done, show_value=False, size="6px") \
            .props('rounded color=primary track-color=grey-4 aria-label="Labeling progress"') \
            .classes("q-mt-xs q-mb-lg measure")

    def _empty(self) -> None:
        with ui.element("div").classes("panel w-full measure"):
            if self.skipped:
                ui.label(f"You've skipped the remaining {plural(len(self.skipped), 'posting')}.")
                ui.button("Show skipped postings again", on_click=self.unskip) \
                    .props("unelevated no-caps").classes("q-mt-md")
            elif self.queue == LABELING:
                ui.label("Every scored posting is labeled. New ones arrive after the next pipeline run.")
            else:
                ui.label("No audit postings are waiting. Draw a sample above to start, "
                         "or draw more once these are done.")
        if self.history:
            with ui.row().classes("q-mt-md"):
                keycap("U", "Undo last label", self.undo)

    def _task(self, task: labeling.LabelTask) -> None:
        with ui.row().classes("items-center gap-2 q-mb-sm").props('aria-label="Steps"'):
            first, second = ("current", "") if task.step == SNIPPET else ("done", "current")
            ui.label("1. Snippet").classes(f"step {first}")
            ui.label("2. Full posting").classes(f"step {second}")
            ui.label(f"{task.remaining} left in this queue").classes("t-small q-ml-md")
        ui.label(INSTRUCTIONS[task.step]).classes("measure q-mb-md")

        posting_card(task.title, task.company, task.location, task.snippet)

        if task.step == FULL:  # task.url is None during the snippet step
            with ui.row().classes("items-center gap-3 q-mt-md"):
                keycap("O", "Open the full posting", self.open_posting)
                external_link("or use this link", task.url)

        with ui.row().classes("gap-4 q-mt-lg w-full"):
            keycap("Y", "Match", lambda: self.submit(True), tone="match")
            keycap("N", "Not a match", lambda: self.submit(False), tone="no-match")
        with ui.row().classes("gap-3 q-mt-md items-center"):
            keycap("S", "Skip for now", self.skip)
            undo = keycap("U", "Undo", self.undo)
            if not self.history:
                undo.disable()


class AuditControls:
    """Status of the prefilter audit and the "draw a random sample" form."""

    def __init__(self, on_draw):
        self.on_draw = on_draw
        with ui.element("div").classes("panel w-full q-mb-lg"):
            self.status_view = ui.column().classes("gap-1")
            with ui.row().classes("items-end gap-3 q-mt-md"):
                self.count = ui.number("How many", value=10, min=1, max=MAX_DRAW, step=1,
                                       precision=0).props("outlined dense").classes("w-28")
                self.window = ui.select(WINDOWS, value=14, label="Posted",
                                        on_change=lambda: self.show_status()) \
                    .props("outlined dense").classes("w-40")
                ui.button("Draw sample", on_click=self.draw).props("unelevated no-caps")
        self.show_status()

    def within_days(self) -> int | None:
        value = self.window.value
        return value if value in WINDOWS and value else None

    def show_status(self) -> None:
        self.status_view.clear()
        ok, s = run_action(labeling.audit_status, conn(), self.within_days())
        with self.status_view:
            if not ok:
                return
            ui.label(f"The prefilter has rejected {plural(s.prefiltered_total, 'posting')} so far. "
                     f"{s.sampled} are in your audit sample and {s.available} more can be drawn.") \
                .classes("measure")
            ui.label("Postings are drawn at random, so the miss rate isn't skewed toward the "
                     "interesting ones. Recent postings work best because old links expire.") \
                .classes("t-small measure")

    def draw(self) -> None:
        # Validate on the server: the browser's min/max are only a convenience.
        try:
            n = int(self.count.value)
        except (TypeError, ValueError):
            n = 0
        if not 1 <= n <= MAX_DRAW:
            ui.notify(f"Choose a number from 1 to {MAX_DRAW}.", type="warning")
            return
        ok, added = run_action(labeling.draw_audit_sample, conn(), n, within_days=self.within_days())
        if not ok:
            return
        if added:
            ui.notify(f"Added {plural(added, 'posting')} to the audit sample.", type="positive")
        else:
            ui.notify("No postings are left to draw for that time range.", type="warning")
        self.show_status()
        self.on_draw()

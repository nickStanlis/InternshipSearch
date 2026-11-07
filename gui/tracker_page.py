"""Tracker page: every saved match in a table, with a side panel for one posting.

The side panel holds the status, Gemini's evaluations and your notes. Text from
outside (titles, companies, reasoning, notes) is only shown as escaped text: in
the table through Quasar's default cell rendering and {{ }} interpolation, and
in the panel through ui.label.
"""
from __future__ import annotations

from nicegui import PageArguments, ui

import db
import tracking
from gui.components import external_link, local_date, plural, run_action
from gui.data import conn

OPEN, ALL = "Open", "All"
FILTERS = [OPEN, ALL, *tracking.STATUSES]

COLUMNS = [
    {"name": "fit", "label": "Fit", "field": "fit", "sortable": True, "align": "right"},
    {"name": "title", "label": "Posting", "field": "title", "sortable": True, "align": "left"},
    {"name": "company", "label": "Company", "field": "company", "sortable": True, "align": "left"},
    {"name": "location", "label": "Location", "field": "location", "align": "left"},
    {"name": "status", "label": "Status", "field": "status", "sortable": True, "align": "left"},
    {"name": "applied", "label": "Applied", "field": "applied", "sortable": True, "align": "left"},
]

# The title cell is a real button so rows can be opened from the keyboard.
# :label passes the title as plain text, so Vue escapes it.
TITLE_SLOT = r"""
<q-td :props="props">
  <q-btn flat dense no-caps align="left" class="tracker-open" :label="props.value"
         @click.stop="() => $parent.$emit('open', props.row.job_id)" />
</q-td>
"""


def page(args: PageArguments) -> None:
    ui.label("Tracker").classes("t-title")
    ui.label("Every posting Gemini saved for you. Select a posting to see its details, "
             "change its status or add notes.").classes("t-soft measure q-mb-lg")
    requested = args.query_parameters.get("status")
    Tracker(initial_filter=requested if requested in FILTERS else OPEN)


def rows_for(filter_value: str) -> list[dict]:
    c = conn()
    if filter_value == ALL:
        postings = db.list_postings(c, include_closed=True)
    elif filter_value == OPEN:
        postings = db.list_postings(c)
    else:
        postings = db.list_postings(c, status=filter_value)
    return [{"job_id": p.job_id, "fit": p.fit_score, "title": p.title or "(untitled)",
             "company": p.company, "location": p.location, "status": p.status,
             "applied": p.applied_date or ""} for p in postings]


class Tracker:
    def __init__(self, initial_filter: str):
        with ui.row().classes("items-end gap-3 w-full q-mb-md"):
            self.filter = ui.select(FILTERS, value=initial_filter, label="Show",
                                    on_change=lambda: self.reload()) \
                .props("outlined dense").classes("w-44")
            self.search = ui.input("Search", placeholder="Title, company, location") \
                .props("outlined dense clearable").classes("w-72")

        with ui.row().classes("items-center gap-3 q-mb-sm bulk-bar") as self.bulk_bar:
            self.bulk_label = ui.label()
            self.bulk_status = ui.select(list(tracking.STATUSES), label="Set status to") \
                .props("outlined dense").classes("w-48")
            ui.button("Apply to selected", on_click=self.bulk_apply).props("unelevated no-caps")
        self.bulk_bar.set_visibility(False)

        self.table = ui.table(columns=COLUMNS, rows=[], row_key="job_id", selection="multiple",
                              pagination=25, on_select=self.on_select) \
            .props('flat bordered wrap-cells no-data-label="No postings match this filter."') \
            .classes("w-full tracker-table")
        self.table.add_slot("body-cell-title", TITLE_SLOT)
        self.table.on("open", lambda e: self.open(e.args))
        self.table.on("rowClick", lambda e: self.open(e.args[1]["job_id"]))
        self.search.bind_value_to(self.table, "filter")

        self.detail = Detail(on_change=self.reload)
        self.reload()

    def reload(self) -> None:
        ok, rows = run_action(rows_for, self.filter.value)
        if ok:
            self.table.rows = rows
            self.table.selected = [r for r in self.table.selected
                                   if any(r["job_id"] == x["job_id"] for x in rows)]
            self.table.update()
        self.on_select()

    def on_select(self) -> None:
        n = len(self.table.selected)
        self.bulk_bar.set_visibility(n > 0)
        self.bulk_label.text = f"{plural(n, 'posting')} selected."

    def bulk_apply(self) -> None:
        status = self.bulk_status.value
        ids = [r["job_id"] for r in self.table.selected]
        if status not in tracking.STATUSES:
            ui.notify("Choose a status first.", type="warning")
            return
        ok, n = run_action(tracking.set_status, conn(), ids, status)
        if ok:
            ui.notify(f"Set {plural(n, 'posting')} to {status}.", type="positive")
            self.table.selected = []
            self.reload()

    def open(self, job_id) -> None:
        if isinstance(job_id, str):
            self.detail.show(job_id)


class Detail:
    """The side panel for one posting."""

    def __init__(self, on_change):
        self.on_change = on_change
        self.job_id: str | None = None
        self.editing: int | None = None          # note being edited
        self.confirm_delete: int | None = None   # note waiting for delete confirmation
        with ui.dialog().props("position=right full-height") as self.dialog, \
                ui.card().classes("detail-panel q-pa-none"):
            with ui.row().classes("w-full justify-end q-pa-sm"):
                ui.button(icon="close", on_click=self.dialog.close) \
                    .props('flat round dense aria-label="Close details"')
            self.body = ui.column().classes("w-full gap-0 q-px-lg q-pb-lg")

    def show(self, job_id: str) -> None:
        self.job_id, self.editing, self.confirm_delete = job_id, None, None
        if self.render():
            self.dialog.open()

    def changed(self) -> None:
        self.render()
        self.on_change()

    # --- actions --------------------------------------------------------------
    def set_status(self, status: str) -> None:
        ok, _ = run_action(tracking.set_status, conn(), [self.job_id], status)
        if ok:
            ui.notify(f"Status changed to {status}.", type="positive")
            self.changed()

    def add_note(self, box: ui.textarea) -> None:
        ok, _ = run_action(tracking.add_note, conn(), self.job_id, box.value)
        if ok:
            self.changed()

    def save_note(self, note_id: int, box: ui.textarea) -> None:
        ok, _ = run_action(tracking.edit_note, conn(), self.job_id, note_id, box.value)
        if ok:
            self.editing = None
            self.changed()

    def delete_note(self, note_id: int) -> None:
        ok, _ = run_action(tracking.delete_note, conn(), self.job_id, note_id)
        self.confirm_delete = None
        if ok:
            ui.notify("Note deleted.")
            self.changed()

    def edit(self, note_id: int | None) -> None:
        self.editing, self.confirm_delete = note_id, None
        self.render()

    def ask_delete(self, note_id: int | None) -> None:
        self.confirm_delete, self.editing = note_id, None
        self.render()

    # --- view -----------------------------------------------------------------
    def render(self) -> bool:
        ok, d = run_action(tracking.posting_detail, conn(), self.job_id)
        self.body.clear()
        if not ok:
            return False
        p = d.posting
        with self.body:
            ui.label(p.title or "(untitled posting)").classes("t-h2")
            ui.label(", ".join(x for x in (p.company, p.location) if x)).classes("t-soft")
            if p.term:
                ui.label(f"Term: {p.term}").classes("t-soft")
            with ui.row().classes("q-mt-sm"):
                external_link("Open the posting", p.url)

            # Status
            ui.label("Status").classes("detail-heading")
            ui.select(list(tracking.STATUSES), value=p.status,
                      on_change=lambda e: self.set_status(e.value) if e.value != p.status else None) \
                .props('outlined dense aria-label="Status"').classes("w-56")
            dates = []
            if p.applied_date:
                dates.append(f"Applied {local_date(p.applied_date)}.")
            if p.status_updated:
                dates.append(f"Status last changed {local_date(p.status_updated)}.")
            if p.date_logged:
                dates.append(f"Saved {local_date(p.date_logged)}.")
            ui.label(" ".join(dates)).classes("t-small q-mt-xs")

            # Gemini
            ui.label("Gemini's evaluation").classes("detail-heading")
            if not d.evaluations:
                ui.label("No evaluation stored for this posting.").classes("t-soft")
            else:
                latest = d.evaluations[0]
                verdict = "match" if latest.is_match else "not a match"
                score = f"{latest.fit_score} of 100" if latest.fit_score is not None else "no score"
                ui.label(f"Fit score {score}, {verdict}.")
                if latest.matched_major:
                    ui.label(f"Matched major: {latest.matched_major}").classes("t-soft")
                if latest.key_strengths:
                    ui.label(f"Strengths: {', '.join(latest.key_strengths)}").classes("t-soft")
                if latest.reasoning:
                    ui.label(latest.reasoning).classes("detail-text q-mt-sm")
                if len(d.evaluations) > 1:
                    ui.label("Earlier scores").classes("t-small q-mt-md")
                    for e in d.evaluations[1:]:
                        ui.label(f"{e.fit_score} ({'match' if e.is_match else 'not a match'}), "
                                 f"{e.model}, prompt {e.prompt_version}, {local_date(e.created_at)}") \
                            .classes("t-small")

            # Notes
            ui.label("Notes").classes("detail-heading")
            if not d.notes:
                ui.label("No notes yet.").classes("t-soft")
            for note in d.notes:
                self._note(note)
            box = ui.textarea(placeholder="Add a note: who you talked to, deadlines, follow-ups") \
                .props(f'outlined autogrow maxlength={tracking.MAX_NOTE_LENGTH} counter') \
                .classes("w-full q-mt-md")
            ui.button("Add note", on_click=lambda: self.add_note(box)).props("unelevated no-caps")
        return True

    def _note(self, note: db.Note) -> None:
        with ui.element("div").classes("note w-full"):
            ui.label(local_date(note.created_at)).classes("t-small")
            if self.editing == note.id:
                box = ui.textarea(value=note.body) \
                    .props(f"outlined autogrow maxlength={tracking.MAX_NOTE_LENGTH} counter").classes("w-full")
                with ui.row().classes("gap-2"):
                    ui.button("Save", on_click=lambda: self.save_note(note.id, box)) \
                        .props("unelevated no-caps dense")
                    ui.button("Cancel", on_click=lambda: self.edit(None)).props("flat no-caps dense")
                return
            ui.label(note.body).classes("detail-text")
            with ui.row().classes("gap-1"):
                if self.confirm_delete == note.id:
                    ui.label("Delete this note?").classes("q-mr-sm self-center")
                    ui.button("Delete", on_click=lambda: self.delete_note(note.id), color="negative") \
                        .props("unelevated no-caps dense")
                    ui.button("Keep", on_click=lambda: self.ask_delete(None)).props("flat no-caps dense")
                else:
                    ui.button("Edit", on_click=lambda: self.edit(note.id)).props("flat no-caps dense")
                    ui.button("Delete", on_click=lambda: self.ask_delete(note.id)) \
                        .props("flat no-caps dense")

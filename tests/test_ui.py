"""UI tests: NiceGUI's simulated user, no browser, a temporary demo database.

Each test runs the real app shell (layout.root) and checks what you'd see and
what ends up in the database.
"""
import asyncio

import pytest
from nicegui import ui
from nicegui.testing.user_simulation import user_simulation

import db
from tests.demo_data import HOSTILE_TITLE, seed
from gui import data, layout


@pytest.fixture
def demo_db(tmp_path):
    path = tmp_path / "demo.db"
    seed(path)
    data.use_database(path)
    yield path
    data.use_database(None)


def run(scenario):
    async def go():
        async with user_simulation(root=layout.root) as user:
            await scenario(user)
    asyncio.run(go())


def press(user, key, *, ctrl=False):
    user.find(ui.keyboard).trigger("key", {
        "action": "keydown", "repeat": False, "altKey": False, "ctrlKey": ctrl,
        "metaKey": False, "shiftKey": False, "key": key, "code": f"Key{key.upper()}", "location": 0})


def seen(path, job_id):
    c = db.connect(path)
    try:
        return db.get_seen_job(c, job_id)
    finally:
        c.close()


def links(user):
    return [el._props.get("href", "") for el in user.find(ui.link).elements]


# ---------------------------------------------------------------------------
# Home
# ---------------------------------------------------------------------------
def test_home_shows_waiting_work_and_last_run(demo_db):
    async def scenario(user):
        await user.open("/")
        await user.should_see("scored postings to label")
        await user.should_see("new matches to review")
        await user.should_see("Pipeline run #1: completed")
        await user.should_see("0 of 1 application got a response (0%).")
    run(scenario)


# ---------------------------------------------------------------------------
# Labeling: blindness and the snippet-first rule
# ---------------------------------------------------------------------------
def test_snippet_step_hides_link_and_gemini_decision(demo_db):
    async def scenario(user):
        await user.open("/label")
        await user.should_see("1. Snippet")
        assert not any(h.startswith(("http", "javascript")) for h in links(user))
        await user.should_not_see("Fits the analytics focus.")  # Gemini's reasoning
        await user.should_not_see("Open the full posting")
    run(scenario)


def test_keys_label_snippet_then_full(demo_db):
    async def scenario(user):
        await user.open("/label")
        press(user, "y")  # snippet: newest posting first (d5)
        await user.should_see("2. Full posting")
        press(user, "n")  # full
        await user.should_see("Saved full posting label")
    run(scenario)
    job = seen(demo_db, "d5")
    assert (job.snippet_label, job.full_label) == (True, False)


def test_undo_clears_the_last_label(demo_db):
    async def scenario(user):
        await user.open("/label")
        press(user, "y")
        await user.should_see("2. Full posting")
        press(user, "u")
        await user.should_see("Undid the snippet label")
        await user.should_see("1. Snippet")
    run(scenario)
    assert seen(demo_db, "d5").snippet_label is None


def test_browser_shortcuts_with_modifiers_are_ignored(demo_db):
    async def scenario(user):
        await user.open("/label")
        press(user, "y", ctrl=True)
        await user.should_see("1. Snippet")
    run(scenario)
    assert seen(demo_db, "d5").snippet_label is None


def test_skip_moves_on_without_labeling(demo_db):
    async def scenario(user):
        await user.open("/label")
        press(user, "s")
        await user.should_see("It comes back next time")
    run(scenario)
    assert seen(demo_db, "d5").snippet_label is None


def test_hostile_posting_is_shown_as_text_and_never_linked(demo_db):
    async def scenario(user):
        await user.open("/label")
        await user.should_see(HOSTILE_TITLE)  # the raw text, so it wasn't parsed as HTML
        press(user, "y")
        await user.should_see("This posting has no usable link.")
        assert not any(h.lower().startswith("javascript") for h in links(user))
    run(scenario)


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------
def test_audit_draw_adds_random_sample(demo_db):
    async def scenario(user):
        await user.open("/label?tab=audit")
        await user.should_see("The prefilter has rejected 6 postings so far.")
        user.find("Draw sample").click()
        await user.should_see("Added 6 postings to the audit sample.")
    run(scenario)
    c = db.connect(demo_db)
    assert db.label_counts(c, audit=True)["total"] == 6
    c.close()


def test_audit_draw_rejects_out_of_range_count(demo_db):
    async def scenario(user):
        await user.open("/label?tab=audit")
        user.find(ui.number).elements.pop().value = 0
        user.find("Draw sample").click()
        await user.should_see("Choose a number from 1 to 200.")
    run(scenario)
    c = db.connect(demo_db)
    assert db.label_counts(c, audit=True)["total"] == 0
    c.close()


def posting(path, job_id):
    c = db.connect(path)
    try:
        return db.get_posting(c, job_id)
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------
def test_review_shows_gemini_reasoning(demo_db):
    async def scenario(user):
        await user.open("/review")
        await user.should_see("Gemini's fit score: 88 of 100")
        await user.should_see("Fits the analytics focus.")
    run(scenario)


def test_review_applied_with_note_then_undo(demo_db):
    async def scenario(user):
        await user.open("/review")
        user.find(ui.textarea).type("Applied through the portal")
        press(user, "a")
        await user.should_see("Marked Data Analytics Intern, Summer 2027 as Applied.")
        p = posting(demo_db, "d1")
        assert p.status == db.APPLIED and p.applied_date
        c = db.connect(demo_db)
        assert [n.body for n in db.list_notes(c, "d1")] == ["Applied through the portal"]
        c.close()
        press(user, "u")
        await user.should_see("Undid the decision")
    run(scenario)
    p = posting(demo_db, "d1")
    assert (p.status, p.applied_date) == (db.UNREVIEWED, None)
    c = db.connect(demo_db)
    assert db.list_notes(c, "d1") == []
    c.close()


def test_review_not_interested_and_skip(demo_db):
    async def scenario(user):
        await user.open("/review")
        press(user, "n")
        await user.should_see("as Not Interested.")
        press(user, "s")
        await user.should_see("Left ")
    run(scenario)
    assert posting(demo_db, "d1").status == db.NOT_INTERESTED
    assert posting(demo_db, "d5").status == db.UNREVIEWED  # skipped, not changed


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------
def test_tracker_lists_open_postings_and_filters_by_status(demo_db):
    async def scenario(user):
        await user.open("/tracker")
        table = user.find(ui.table).elements.pop()
        assert {r["job_id"] for r in table.rows} == {"d1", "d2", "d3", "d5"}
        await user.open("/tracker?status=Applied")
        table = user.find(ui.table).elements.pop()
        assert [r["job_id"] for r in table.rows] == ["d2"]
    run(scenario)


def test_tracker_ignores_unknown_status_filter(demo_db):
    async def scenario(user):
        await user.open("/tracker?status=<script>")
        table = user.find(ui.table).elements.pop()
        assert len(table.rows) == 4  # fell back to Open
    run(scenario)


def test_tracker_detail_change_status_and_notes(demo_db):
    async def scenario(user):
        await user.open("/tracker")
        user.find(ui.table).trigger("open", "d3")
        await user.should_see("Harbor Point Capital, Chicago, IL")
        user.find(ui.textarea).type("Recruiter call Friday")
        user.find("Add note").click()
        await user.should_see("Recruiter call Friday")
        status = [s for s in user.find(ui.select).elements if s.value == db.UNREVIEWED][0]
        status.value = db.INTERVIEWING
        await user.should_see("Status changed to Interviewing.")
    run(scenario)
    assert posting(demo_db, "d3").status == db.INTERVIEWING


def test_tracker_bulk_status(demo_db):
    async def scenario(user):
        await user.open("/tracker")
        table = user.find(ui.table).elements.pop()
        rows = [r for r in table.rows if r["job_id"] in ("d1", "d3")]
        user.find(ui.table).trigger("selection", {"added": True, "rows": rows, "keys": ["d1", "d3"]})
        await user.should_see("2 postings selected.")
        bulk = [s for s in user.find(ui.select).elements if s.props.get("label") == "Set status to"][0]
        bulk.value = db.NOT_INTERESTED
        user.find("Apply to selected").click()
        await user.should_see("Set 2 postings to Not Interested.")
    run(scenario)
    assert posting(demo_db, "d1").status == posting(demo_db, "d3").status == db.NOT_INTERESTED

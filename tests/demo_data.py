"""Fictional demo data, for UI tests and screenshots.

    python -m tests.demo_data demo.db     # then: python app.py --db demo.db

Includes one deliberately hostile posting (HTML in the title, a javascript: link)
so the tests can prove the UI shows it as plain text and never links it.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import db
import tracking

HOSTILE_TITLE = '<img src=x onerror="document.title=\'pwned\'">Data Intern'
HOSTILE_URL = "javascript:document.title='pwned'"

SCORED = [  # (id, title, company, location, snippet, is_match, score)
    ("d1", "Data Analytics Intern, Summer 2027", "Northlake Logistics", "Detroit, MI",
     "Join our analytics team to build dashboards in Python and SQL, clean shipment data, "
     "and present findings to operations leads. Pursuing a degree in statistics, data science, "
     "computer science or a related field.", True, 88),
    ("d2", "Software Engineering Intern", "Brightwater Health", "Ann Arbor, MI",
     "Work on internal tools in Python and TypeScript. You'll ship features with a mentor, "
     "write tests and join code reviews. Open to rising juniors and seniors.", True, 81),
    ("d3", "Quantitative Research Intern", "Harbor Point Capital", "Chicago, IL",
     "Research systematic strategies with a team of quants. Strong probability and statistics "
     "background required; Python or R experience preferred.", True, 77),
    ("d4", "Marketing Coordinator Intern", "Fable & Fern Co.", "Remote",
     "Plan social campaigns, write newsletter copy and track engagement in spreadsheets.",
     False, 31),
    ("d5", HOSTILE_TITLE, "Test <b>Corp</b>", None,
     "<script>document.title='pwned'</script>Ordinary snippet text after a script tag.",
     True, 79),
]
PREFILTERED = [(f"p{i}", t, "Various", "Detroit, MI", "Rejected by the keyword prefilter.")
               for i, t in enumerate(["Barista", "Warehouse Associate", "Data Entry Clerk",
                                      "Lifeguard", "Junior Python Developer", "Cashier"], 1)]


def seed(path: str | Path) -> None:
    c = db.connect(path)
    for jid, title, company, loc, snippet, is_match, score in SCORED:
        url = HOSTILE_URL if jid == "d5" else f"https://example.com/jobs/{jid}"
        ev = types.SimpleNamespace(is_match=is_match, fit_score=score, term="Summer 2027",
                                   matched_major="Statistics", key_strengths=["Python", "SQL"],
                                   reasoning="Fits the analytics focus.")
        db.record_scored(c, db.NewPosting(jid, title, company, loc, url, snippet), ev,
                         model="demo-model", prompt_version="demo", save_as_match=score >= 75)
    for jid, title, company, loc, snippet in PREFILTERED:
        db.record_prefiltered(c, db.NewPosting(jid, title, company, loc,
                                               f"https://example.com/jobs/{jid}", snippet))
    tracking.set_status(c, ["d2"], db.APPLIED)
    run_id = db.start_run(c, model="demo-model", prompt_version="demo")
    db.finish_run(c, run_id, status="completed",
                  stats={"fetched": 240, "duplicates": 12, "already_seen": 150, "prefiltered_out": 6,
                         "evaluated": 5, "matches": 4, "errors": 0, "deferred": 0})
    c.close()


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "demo.db")
    if target.exists():
        sys.exit(f"{target} already exists; pick a new file name.")
    seed(target)
    print(f"Wrote demo data to {target}. Start the UI with: python app.py --db {target}")

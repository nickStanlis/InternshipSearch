"""One-off check for labels damaged by the old Evaluate.py bug.

Between commits 155f093 (Nov 4) and a88a95f (Nov 6), Evaluate.py saved the
full-posting answer with set_snippet_label instead of set_full_label. A posting
labeled in that window ended up with:
    snippet label = your FULL-posting judgment, full label = empty.

There are no label timestamps, so this lists every posting in that state. Some
may be legitimate (you answered the snippet step, then quit at the full step).

    python check_labels.py           # list suspects (read-only)
    python check_labels.py --reset   # clear their snippet labels so you relabel them

Delete this file once you've run it.
"""
import sys

import db


def suspects(conn) -> list[db.SeenJob]:
    return [j for j in db.labeled_jobs(conn, outcome=None)
            if j.snippet_label is not None and j.full_label is None]


def main() -> None:
    conn = db.connect()
    found = suspects(conn)
    if not found:
        print("No suspect labels found. Nothing to fix.")
        return

    print(f"{len(found)} posting(s) have a snippet label but no full label:\n")
    for j in found:
        label = "Valid" if j.snippet_label else "Invalid"
        print(f"  #{j.num:<5} {label:<8} {j.outcome:<12} {j.title} @ {j.company}")

    if "--reset" in sys.argv:
        for j in found:
            db.set_snippet_label(conn, j.job_id, None)
        print(f"\nCleared {len(found)} snippet label(s). They're back in the labeling queue.")
    else:
        print("\nRun with --reset to clear these snippet labels and relabel them.")


if __name__ == "__main__":
    main()

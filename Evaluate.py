import sys
import argparse
import textwrap
import webbrowser

from Tracker import connect

LABELS = {"y": "Valid", "n": "Invalid"}

def set_unevaluated(conn, args) -> None :
    cols = {row[1] for row in conn.execute("PRAGMA table_info(seen_jobs)")}
    if "training_status_description" not in cols:
        conn.execute("ALTER TABLE seen_jobs ADD COLUMN training_status_description TEXT DEFAULT 'Unlabeled'")
    if "training_status_url" not in cols:
        conn.execute("ALTER TABLE seen_jobs ADD COLUMN training_status_url TEXT DEFAULT 'Unlabeled'")
    conn.commit()



def cmd_set_training_status_description(conn, refs, training_status_description):
    for ref in refs:
        rid = resolve_training(conn, ref)
        if rid is None:
            print(f"  ? No posting #{ref}")
            continue

        conn.execute(
            "UPDATE seen_jobs SET training_status_description = ?  WHERE rowid = ?",
            (training_status_description, rid))

        title, company = conn.execute(
            "SELECT title, company FROM seen_jobs WHERE rowid = ?", (rid,)).fetchone()
        print(f"   #{rid} {title} @ {company} new {training_status_description}")
    conn.commit()


def cmd_set_training_status_url(conn, refs, training_status_url):
    for ref in refs:
        rid = resolve_training(conn, ref)
        if rid is None:
            print(f"  ? No posting #{ref}")
            continue

        conn.execute(
            "UPDATE seen_jobs SET training_status_url = ?  WHERE rowid = ?",
            (training_status_url, rid))

        title, company = conn.execute(
            "SELECT title, company FROM seen_jobs WHERE rowid = ?", (rid,)).fetchone()
        print(f"   #{rid} {title} @ {company} new {training_status_url}")
    conn.commit()


def resolve_training(conn, ref: str):

    row = None
    if ref.isdigit():
        row = conn.execute("SELECT rowid FROM seen_jobs WHERE rowid = ?", (int(ref),)).fetchone()
    if row is None:
        row = conn.execute("SELECT rowid FROM seen_jobs WHERE job_id = ?", (ref,)).fetchone()
    return row[0] if row else None


def ask_label(prompt, url=None, allow_open=False):
    """Ask for one label. Returns 'y', 'n', 'skip' or 'quit'.

    'o' only works when allow_open is True, so the snippet step can't
    peek at the full posting.
    """
    options = "[y]es  [n]o  [Enter] skip  [q]uit"
    if allow_open:
        options = "[o]pen  " + options
    print(f"  {prompt}   {options}")

    while True:
        choice = input("> ").strip().lower()
        if choice in LABELS:
            return choice
        if choice == "":
            return "skip"
        if choice == "q":
            return "quit"
        if choice == "o" and allow_open:
            if url:
                webbrowser.open(url)
            else:
                print("  No URL for this posting.")
            continue  # stay on this question after opening
        print(f"  Choose one of: {options}")


def cmd_evaluate(conn):
    """Label each evaluated job twice: from the snippet alone, then from the full posting."""
    rows = conn.execute(
        """SELECT rowid, title, company, url, description,
                  training_status_description, training_status_url
           FROM seen_jobs
           WHERE outcome = 'evaluated'
             AND (training_status_description = 'Unlabeled'
                  OR training_status_url = 'Unlabeled')
           ORDER BY rowid DESC""").fetchall()
    if not rows:
        print("Nothing left to label.")
        return

    print(f"{len(rows)} postings to label.  "
          "Step 1: snippet only.  Step 2: full posting.\n")

    for i, (rid, title, company, url, description, snippet_label, full_label) in enumerate(rows, 1):
        print("=" * 90)
        print(f"({i}/{len(rows)})  #{rid}  {title} @ {company}")
        print(textwrap.fill(description or "(no snippet stored)", width=88,
                            initial_indent="  ", subsequent_indent="  "))

        # Step 1: snippet only (no URL shown, no [o]pen)
        if snippet_label == "Unlabeled":
            answer = ask_label("SNIPPET ONLY - is this a match?")
            if answer == "quit":
                return
            if answer == "skip":
                continue  # skip the whole job; full label must come after the snippet label
            cmd_set_training_status_description(conn, [str(rid)], LABELS[answer])

        # Step 2: full posting (runs for both yes and no snippet labels)
        if full_label == "Unlabeled":
            print(f"\n  Now check the full posting: {url or '(no URL)'}")
            answer = ask_label("FULL POSTING - is this a match?", url=url, allow_open=True)
            if answer == "quit":
                return
            if answer == "skip":
                continue  # snippet label is kept; this job comes back next session
            cmd_set_training_status_url(conn, [str(rid)], LABELS[answer])

def main():
    parser = argparse.ArgumentParser(description="Track internship applications.")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("evaluate", help="Go through unelabeled postings one by one")

    # No command (e.g. launched with an IDE's Run button) -> show the open list.
    args = parser.parse_args(sys.argv[1:] or ["evaluate"])
    conn = connect()

    try:
        set_unevaluated(conn, args)
        if args.cmd == "evaluate":
            cmd_evaluate(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
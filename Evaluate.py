import sys
import argparse
import textwrap
import webbrowser

import db
from Tracker import connect

ANSWERS = {"y": True, "n": False}


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
        if choice in ANSWERS:
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
    queue = db.labeling_queue(conn)
    if not queue:
        print("Nothing left to label.")
        return

    print(f"{len(queue)} postings to label.  "
          "Step 1: snippet only.  Step 2: full posting.\n")

    for i, job in enumerate(queue, 1):
        print("=" * 90)
        print(f"({i}/{len(queue)})  #{job.num}  {job.title} @ {job.company}")
        print(textwrap.fill(job.description or "(no snippet stored)", width=88,
                            initial_indent="  ", subsequent_indent="  "))

        # Step 1: snippet only (no URL shown, no [o]pen)
        if job.snippet_label is None:
            answer = ask_label("SNIPPET ONLY - is this a match?")
            if answer == "quit":
                return
            if answer == "skip":
                continue
            db.set_snippet_label(conn, job.job_id, ANSWERS[answer])

        # Step 2: full posting (runs for both yes and no snippet labels)
        if job.full_label is None:
            print(f"\n  Now check the full posting: {job.url or '(no URL)'}")
            answer = ask_label("FULL POSTING - is this a match?", url=job.url, allow_open=True)
            if answer == "quit":
                return
            if answer == "skip":
                continue  # snippet label is kept; this job comes back next session
            db.set_snippet_label(conn, job.job_id, ANSWERS[answer])


def main():
    parser = argparse.ArgumentParser(description="Evaluate AI agent's effectiveness")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("evaluate", help="Go through unelabeled postings one by one")

    # No command (e.g. launched with an IDE's Run button) -> show the open list.
    args = parser.parse_args(sys.argv[1:] or ["evaluate"])
    conn = connect()

    try:
        if args.cmd == "evaluate":
            cmd_evaluate(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
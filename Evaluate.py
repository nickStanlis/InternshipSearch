import sys
import sqlite3
import argparse
import textwrap
import webbrowser
from datetime import date

from Tracker import STATUSES, cmd_set_status, add_note, connect



def set_unevaluated(conn, args) -> None :
    cols = {row[1] for row in conn.execute("PRAGMA table_info(seen_jobs)")}
    if "training_status" not in cols:
        conn.execute("ALTER TABLE seen_jobs ADD COLUMN training_status TEXT DEFAULT 'Unevaluated'")
    conn.commit()



def cmd_set_training_status(conn, refs, training_status):
    today = date.today().isoformat()
    for ref in refs:
        rid = resolve_training(conn, ref)
        if rid is None:
            print(f"  ? No posting #{ref}")
            continue

        conn.execute(
            "UPDATE seen_jobs SET training_status = ?  WHERE rowid = ?",
            (training_status, rid))

        title, company = conn.execute(
            "SELECT title, company FROM seen_jobs WHERE rowid = ?", (rid,)).fetchone()
        print(f"   #{rid} {title} @ {company} new {training_status}")
    conn.commit()

def resolve_training(conn, ref: str):
    """Accept either the short # shown by `list` or the full Adzuna job_id."""
    row = None
    if ref.isdigit():
        row = conn.execute("SELECT rowid FROM seen_jobs WHERE rowid = ?", (int(ref),)).fetchone()
    if row is None:
        row = conn.execute("SELECT rowid FROM seen_jobs WHERE job_id = ?", (ref,)).fetchone()
    return row[0] if row else None



def cmd_evaluate(conn, args):
    """Walk through unevaluated postings one at a time, to cross validate ."""
    rows = conn.execute(
        """SELECT s.rowid, s.title, s.company,  s.url
           FROM seen_jobs s
           WHERE s.training_status = 'Unevaluated'
           """).fetchall()
    if not rows:
        print("Nothing left to evaluate. ")
        return

    print(f"{len(rows)} postings to evaluate.  "
          "[o]pen  [y]es  [n]o  [Enter] next  [q]uit\n")
    for i, (rid, title, company,  url) in enumerate(rows, 1):
        print("=" * 90)
        print(f"({i}/{len(rows)})  #{rid}  {title} @ {company}")
        print(f"  {url or '(no URL)'}")

        while True:
            choice = input("> ").strip().lower()
            if choice == "o":
                if url:
                    webbrowser.open(url)
                else:
                    print("  No URL for this posting.")
            elif choice == "y":
                cmd_set_training_status(conn, [str(rid)], "Valid")
                break
            elif choice == "n":
                cmd_set_training_status(conn, [str(rid)], "Invalid")
                break
            elif choice == "":
                break               # leave as Unevaluated, come back later
            elif choice == "q":
                return
            else:
                print("  o / y / n / Enter / q")


def main():
    parser = argparse.ArgumentParser(description="Track internship applications.")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("evaluate", help="Go through unevaluated postings one by one")

    # No command (e.g. launched with an IDE's Run button) -> show the open list.
    args = parser.parse_args(sys.argv[1:] or ["evaluate"])
    conn = connect()

    try:
        set_unevaluated(conn, args)
        if args.cmd == "evaluate":
            cmd_evaluate(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
import sys
import sqlite3
import argparse
import textwrap
import webbrowser
from datetime import date

from Tracker import STATUSES, cmd_set_status, add_note, connect, resolve, cut, cmd_list, cmd_show, cmd_note, cmd_summary
from Tracker import CLOSED
from Tracker import STATUS_ORDER
DB_PATH = "matches.db"
training_status = {
    "unevaluated" : "Unevaluated",
    "valid" : "Valid",
    "invalid" : "Invalid",
}
training_status_ORDER = [
    "valid",
    "invalid",
    "unevaluated",
]


def set_unevaluated(conn, args) -> None :
    cols = {row[1] for row in conn.execute("PRAGMA table_info(seen_jobs)")}
    if "training_status" not in cols:
        conn.execute("ALTER TABLE seen_jobs ADD COLUMN training_status TEXT DEFAULT 'Unevaluated'")
    conn.commit()


def cmd_set_training_status(conn, refs, training_status):
    today = date.today().isoformat()
    for ref in refs:
        rid = resolve(conn, ref)
        if rid is None:
            print(f"  ? No posting #{ref}")
            continue
        if training_status == "Unevaluated":
            conn.execute(
                "UPDATE seen_jobs SET training_status = ?,  WHERE rowid = ?",
                (training_status, rid))
        else:
            conn.execute("UPDATE seen_jobs SET training_status = ?, WHERE rowid = ?",
                         (training_status,  rid))
        title, company = conn.execute(
            "SELECT title, company FROM seen_jobs WHERE rowid = ?", (rid,)).fetchone()
        print(f"   #{rid} {title} @ {company} new {training_status}")
    conn.commit()

def cmd_evaluate(conn, args):
    """Walk through unevaluated postings one at a time, to cross validate ."""
    rows = conn.execute(
        """SELECT p.rowid, p.title, p.company, p.location, p.term, p.url
           FROM job_postings p LEFT JOIN seen_jobs s ON s.job_id = p.job_id
           WHERE p.training_status = 'Unevaluated'
           ORDER BY p.date_logged DESC""").fetchall()
    if not rows:
        print("Nothing left to evaluate. ")
        return

    print(f"{len(rows)} postings to evaluate.  "
          "[o]pen  [y]es  [n]o  [Enter] next  [q]uit\n")
    for i, (rid, title, company, loc, term, url) in enumerate(rows, 1):
        print("=" * 90)
        print(f"({i}/{len(rows)})  #{rid}  {title} @ {company}")
        print(f"  {loc}  |  {term}")
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
                note = input("  Note (Enter to skip): ").strip()
                if note:
                    add_note(conn, rid, note)
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
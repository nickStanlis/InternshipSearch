"""Track applications for jobs saved by job_matcher.py.

Usage:
  python tracker.py review               # walk through new postings one at a time
  python tracker.py list                # open postings (hides Rejected / Not Interested)
  python tracker.py list --all           # everything
  python tracker.py list --status applied
  python tracker.py show 4               # full details + URL for posting #4
  python tracker.py applied 4 7 12       # mark one or more postings
  python tracker.py interviewing 4
  python tracker.py rejected 7
  python tracker.py offer 4
  python tracker.py skip 12              # not interested
  python tracker.py reset 12             # back to Unreviewed
  python tracker.py note 4 "Recruiter: Jane, follow up Nov 10"
  python tracker.py summary
"""
import sys
import sqlite3
import argparse
import textwrap
import webbrowser
from datetime import date

DB_PATH = "matches.db"

STATUSES = {
    "applied": "Applied",
    "interviewing": "Interviewing",
    "rejected": "Rejected",
    "offer": "Offer",
    "skip": "Not Interested",
    "reset": "Unreviewed",

}
CLOSED = ("Rejected", "Not Interested")
STATUS_ORDER = ["Offer", "Interviewing", "Applied", "Unreviewed", "Rejected", "Not Interested"]


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(job_postings)")}
    if not cols:
        sys.exit("No job_postings table found. Run job_matcher.py first.")
    # Add tracking columns the first time this runs; existing data is untouched.
    for col in ("applied_date", "status_updated", "notes"):
        if col not in cols:
            conn.execute(f"ALTER TABLE job_postings ADD COLUMN {col} TEXT")
    conn.commit()
    return conn


def resolve(conn, ref: str):
    """Accept either the short # shown by `list` or the full Adzuna job_id."""
    row = None
    if ref.isdigit():
        row = conn.execute("SELECT rowid FROM job_postings WHERE rowid = ?", (int(ref),)).fetchone()
    if row is None:
        row = conn.execute("SELECT rowid FROM job_postings WHERE job_id = ?", (ref,)).fetchone()
    return row[0] if row else None


def cut(text, width):
    text = text or ""
    return text if len(text) <= width else text[: width - 1] + "…"


def cmd_list(conn, args):
    query = "SELECT rowid, status, fit_score, title, company, location, term, applied_date FROM job_postings"
    params = []
    if args.status:
        status = STATUSES.get(args.status.lower(), args.status)
        query += " WHERE status = ?"
        params.append(status)
    elif not args.all:
        query += f" WHERE status NOT IN ({','.join('?' * len(CLOSED))})"
        params.extend(CLOSED)
    rows = conn.execute(query, params).fetchall()
    if not rows:
        print("No postings to show.")
        return

    rank = {s: i for i, s in enumerate(STATUS_ORDER)}
    rows.sort(key=lambda r: (rank.get(r[1], 99), -(r[2] or 0)))

    print(f"{'#':>4}  {'Status':<14} {'Fit':>3}  {'Title':<36} {'Company':<22} {'Location':<20} {'Term':<14} Applied")
    print("-" * 132)
    for rid, status, score, title, company, loc, term, applied in rows:
        print(f"{rid:>4}  {cut(status, 14):<14} {score or '':>3}  {cut(title, 36):<36} "
              f"{cut(company, 22):<22} {cut(loc, 20):<20} {cut(term, 14):<14} {applied or ''}")


def cmd_show(conn, args):
    rid = resolve(conn, args.ref)
    if rid is None:
        sys.exit(f"No posting #{args.ref}")
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT rowid AS num, * FROM job_postings WHERE rowid = ?", (rid,)).fetchone()
    for key in row.keys():
        print(f"{key:>15}: {row[key] if row[key] is not None else ''}")
    reasoning = conn.execute("SELECT reasoning FROM seen_jobs WHERE job_id = ?", (row["job_id"],)).fetchone()
    if reasoning and reasoning[0]:
        print(f"\n      reasoning:\n{textwrap.fill(reasoning[0], width=90, initial_indent='  ', subsequent_indent='  ')}")


def cmd_set_status(conn, refs, status):
    today = date.today().isoformat()
    for ref in refs:
        rid = resolve(conn, ref)
        if rid is None:
            print(f"  ? No posting #{ref}")
            continue
        if status == "Applied":
            conn.execute(
                "UPDATE job_postings SET status = ?, status_updated = ?, "
                "applied_date = COALESCE(applied_date, ?) WHERE rowid = ?",
                (status, today, today, rid))
        else:
            conn.execute("UPDATE job_postings SET status = ?, status_updated = ? WHERE rowid = ?",
                         (status, today, rid))
        title, company = conn.execute(
            "SELECT title, company FROM job_postings WHERE rowid = ?", (rid,)).fetchone()
        print(f"  ✓ #{rid} {title} @ {company} → {status}")
    conn.commit()


def cmd_note(conn, args):
    rid = resolve(conn, args.ref)
    if rid is None:
        sys.exit(f"No posting #{args.ref}")
    add_note(conn, rid, args.text)
    print(f"  ✓ Note added to #{rid}")


def cmd_review(conn, args):
    """Walk through unreviewed postings one at a time, best fit first."""
    rows = conn.execute(
        """SELECT p.rowid, p.fit_score, p.title, p.company, p.location, p.term, p.url, s.reasoning
           FROM job_postings p LEFT JOIN seen_jobs s ON s.job_id = p.job_id
           WHERE p.status = 'Unreviewed'
           ORDER BY p.fit_score DESC, p.date_logged DESC""").fetchall()
    if not rows:
        print("Nothing left to review. ")
        return

    print(f"{len(rows)} postings to review.  "
          "[o]pen  [a]pplied  [s]kip  [n]ote  [Enter] next  [q]uit\n")
    for i, (rid, score, title, company, loc, term, url, reasoning) in enumerate(rows, 1):
        print("=" * 90)
        print(f"({i}/{len(rows)})  #{rid}  {title} @ {company}   fit {score}")
        print(f"  {loc}  |  {term}")
        if reasoning:
            print(textwrap.fill(reasoning, width=88, initial_indent="  ", subsequent_indent="  "))
        print(f"  {url or '(no URL)'}")

        while True:
            choice = input("> ").strip().lower()
            if choice == "o":
                if url:
                    webbrowser.open(url)
                else:
                    print("  No URL for this posting.")
            elif choice == "a":
                cmd_set_status(conn, [str(rid)], "Applied")
                note = input("  Note (Enter to skip): ").strip()
                if note:
                    add_note(conn, rid, note)
                break
            elif choice == "s":
                cmd_set_status(conn, [str(rid)], "Not Interested")
                break
            elif choice == "n":
                note = input("  Note: ").strip()
                if note:
                    add_note(conn, rid, note)
                    print("   Note added")
            elif choice == "":
                break               # leave as Unreviewed, come back later
            elif choice == "q":
                return
            else:
                print("  o / a / s / n / Enter / q")


def add_note(conn, rid, text):
    existing = conn.execute("SELECT notes FROM job_postings WHERE rowid = ?", (rid,)).fetchone()[0]
    entry = f"[{date.today().isoformat()}] {text}"
    conn.execute("UPDATE job_postings SET notes = ? WHERE rowid = ?",
                 (f"{existing}\n{entry}" if existing else entry, rid))
    conn.commit()


def cmd_summary(conn, args):
    counts = dict(conn.execute("SELECT status, COUNT(*) FROM job_postings GROUP BY status").fetchall())
    total = sum(counts.values())
    for status in STATUS_ORDER + sorted(set(counts) - set(STATUS_ORDER)):
        if counts.get(status):
            print(f"  {status:<15} {counts[status]}")
    print(f"  {'Total':<15} {total}")
    applied = sum(counts.get(s, 0) for s in ("Applied", "Interviewing", "Rejected", "Offer"))
    if applied:
        responses = counts.get("Interviewing", 0) + counts.get("Offer", 0)
        print(f"\n  Applied to {applied}; {responses} reached interview or offer "
              f"({responses / applied:.0%}).")


def main():
    parser = argparse.ArgumentParser(description="Track internship applications.")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("list", help="List postings")
    p.add_argument("--all", action="store_true", help="Include rejected / not interested")
    p.add_argument("--status", help="Only this status, e.g. applied")

    p = sub.add_parser("show", help="Show full details for one posting")
    p.add_argument("ref")

    for name, status in STATUSES.items():
        p = sub.add_parser(name, help=f"Mark postings as {status}")
        p.add_argument("refs", nargs="+")

    p = sub.add_parser("note", help="Add a dated note to a posting")
    p.add_argument("ref")
    p.add_argument("text")

    sub.add_parser("summary", help="Counts by status")
    sub.add_parser("review", help="Go through unreviewed postings one by one")

    # No command (e.g. launched with an IDE's Run button) -> show the open list.
    args = parser.parse_args(sys.argv[1:] or ["list"])
    conn = connect()
    try:
        if args.cmd == "list":
            cmd_list(conn, args)
        elif args.cmd == "show":
            cmd_show(conn, args)
        elif args.cmd in STATUSES:
            cmd_set_status(conn, args.refs, STATUSES[args.cmd])
        elif args.cmd == "note":
            cmd_note(conn, args)
        elif args.cmd == "summary":
            cmd_summary(conn, args)
        elif args.cmd == "review":
            cmd_review(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
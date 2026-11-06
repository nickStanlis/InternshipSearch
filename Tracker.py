"""Track applications for jobs saved by Pipeline.py.

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

import argparse
import textwrap
import webbrowser
from datetime import date
import db

STATUSES = {
    "applied": db.APPLIED,
    "interviewing": db.INTERVIEWING,
    "rejected": db.REJECTED,
    "offer": db.OFFER,
    "skip": db.NOT_INTERESTED,
    "reset": db.UNREVIEWED,
}

def cut(text, width):
    text = text or ""
    return text if len(text) <= width else text[: width - 1] + "…"


def wrap(text, width=88):
    return textwrap.fill(text, width=width, initial_indent="  ", subsequent_indent="  ")


def resolve_or_exit(conn, ref: str) -> str:
    """Accept either the short # shown by `list` or the full Adzuna job_id."""
    job_id = db.resolve_posting(conn, ref)
    if job_id is None:
        sys.exit(f"No posting #{ref}")
    return job_id


def cmd_list(conn, args):
    status = STATUSES.get(args.status.lower(), args.status) if args.status else None
    postings = db.list_postings(conn, status=status, include_closed=args.all)
    if not postings:
        print("No postings to show.")
        return

    print(f"{'#':>4}  {'Status':<14} {'Fit':>3}  {'Title':<36} {'Company':<22} {'Location':<20} {'Term':<14} Applied")
    print("-" * 132)
    for p in postings:
        print(f"{p.num:>4}  {cut(p.status, 14):<14} {p.fit_score or '':>3}  {cut(p.title, 36):<36} "
              f"{cut(p.company, 22):<22} {cut(p.location, 20):<20} {cut(p.term, 14):<14} {p.applied_date or ''}")

def cmd_show(conn, args):
    p = db.get_posting(conn, resolve_or_exit(conn, args.ref))
    fields = {
        "#": p.num,
        "job_id": p.job_id,
        "title": p.title,
        "company": p.company,
        "location": p.location,
        "term": p.term,
        "status": p.status,
        "fit_score": p.fit_score,
        "matched_major": p.matched_major,
        "key_strengths": ", ".join(p.key_strengths),
        "date_logged": p.date_logged,
        "applied_date": p.applied_date,
        "status_updated": p.status_updated,
        "url": p.url,
    }
    for key, value in fields.items():
        print(f"{key:>15}: {value if value is not None else ''}")

    if p.reasoning:
        print("\n      reasoning:")
        print(wrap(p.reasoning, 90))

    notes = db.list_notes(conn, p.job_id)
    if notes:
        print("\n          notes:")
        for n in notes:
            body = n.body.replace("\n", "\n" + " " * 15)  # line up continuation lines
            print(f"  [{n.created_at or '?'}] {body}")

def cmd_set_status(conn, refs, status):
    today = date.today().isoformat()
    for ref in refs:
        job_id = db.resolve_posting(conn, ref)
        if job_id is None:
            print(f"  ? No posting #{ref}")
            continue
        db.set_status(conn, [job_id], status)
        p = db.get_posting(conn, job_id)
        print(f"  #{p.num} {p.title} @ {p.company} -> {status}")


def cmd_note(conn, args):
    job_id = resolve_or_exit(conn, args.ref)
    db.add_note(conn, job_id, args.text)
    print(f" Note added to #{args.ref}")


def cmd_review(conn, args):
    queue = db.review_queue(conn)
    if not queue:
        print("Nothing left to review.")
        return

    print(f"{len(queue)} postings to review.  "
          "[o]pen  [a]pplied  [s]kip  [n]ote  [Enter] next  [q]uit\n")
    for i, p in enumerate(queue, 1):
        print("=" * 90)
        print(f"({i}/{len(queue)})  #{p.num}  {p.title} @ {p.company}   fit {p.fit_score}")
        print(f"  {p.location}  |  {p.term}")
        if p.reasoning:
            print(wrap(p.reasoning))
        print(f"  {p.url or '(no URL)'}")

        while True:
            choice = input("> ").strip().lower()
            if choice == "o":
                if p.url:
                    webbrowser.open(p.url)
                else:
                    print("  No URL for this posting.")
            elif choice == "a":
                db.set_status(conn, [p.job_id], db.APPLIED)
                print(f"  ✓ #{p.num} → {db.APPLIED}")
                note = input("  Note (Enter to skip): ").strip()
                if note:
                    db.add_note(conn, p.job_id, note)
                break
            elif choice == "s":
                db.set_status(conn, [p.job_id], db.NOT_INTERESTED)
                print(f"  ✓ #{p.num} → {db.NOT_INTERESTED}")
                break
            elif choice == "n":
                note = input("  Note: ").strip()
                if note:
                    db.add_note(conn, p.job_id, note)
                    print("  ✓ Note added")
            elif choice == "":
                break  # leave as Unreviewed, come back later
            elif choice == "q":
                return
            else:
                print("  o / a / s / n / Enter / q")




def cmd_summary(conn, args):
    counts = db.status_counts(conn)
    for status, count in counts.items():
        print(f"  {status:<15} {count}")
    print(f"  {'Total':<15} {sum(counts.values())}")

    applied = sum(counts.get(s, 0) for s in db.APPLIED_OR_LATER)
    if applied:
        responses = counts.get(db.INTERVIEWING, 0) + counts.get(db.OFFER, 0)
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
    conn = db.connect()
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
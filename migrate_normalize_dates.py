"""One-off migration: normalize email_candidates.datum / end_date to ISO.

Rewrites any German (DD.MM.YYYY / DD.MM.YY), range-shorthand ("13.-15.11.26")
or time-suffixed ISO value to plain YYYY-MM-DD, so the stored format matches the
"intern ISO" convention. Uses the SAME parser as the dashboard calendar
(_parse_calendar_date), so anything the calendar can show, this can normalize.

DRY-RUN by default — nothing is written unless you pass --commit.

Run on the server inside the app container:
    docker compose exec app python migrate_normalize_dates.py            # preview
    docker compose exec app python migrate_normalize_dates.py --commit   # apply

Take a DB backup first (see the runbook printed by --help / the PR notes).
"""
import sys

from database import get_session
from models import EmailCandidate
from email_client import _parse_calendar_date

FIELDS = ('datum', 'end_date')


def _to_iso(value):
    d = _parse_calendar_date(value)
    return d.strftime('%Y-%m-%d') if d else None


def main(commit: bool) -> None:
    to_change = []   # (id, field, old, new)
    skipped = []     # (id, field, value) -- unparseable, needs manual fix

    with get_session() as s:
        for row in s.query(EmailCandidate).order_by(EmailCandidate.id).all():
            for field in FIELDS:
                cur = getattr(row, field)
                if not cur:
                    continue
                new = _to_iso(cur)
                if new is None:
                    skipped.append((row.id, field, cur))
                elif new != cur:
                    to_change.append((row.id, field, cur, new))
                    if commit:
                        setattr(row, field, new)
        # On dry-run we never mutated anything, so the auto-commit is a no-op.

    mode = 'APPLIED' if commit else 'DRY-RUN (no changes written)'
    print(f"=== {mode} ===\n")

    if to_change:
        print(f"{len(to_change)} value(s) normalized to ISO:")
        for rid, field, old, new in to_change:
            print(f"  #{rid:<4} {field:<9} {old!r:>16}  ->  {new}")
    else:
        print("Nothing to normalize — all values already ISO.")

    if skipped:
        print(f"\n{len(skipped)} value(s) could NOT be parsed "
              f"(left untouched — fix manually in the UI):")
        for rid, field, val in skipped:
            print(f"  #{rid:<4} {field:<9} {val!r}")

    if not commit and to_change:
        print("\nRe-run with --commit to apply.")


if __name__ == '__main__':
    main('--commit' in sys.argv)

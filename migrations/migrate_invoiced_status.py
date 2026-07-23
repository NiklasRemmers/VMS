"""One-off migration: mark already-invoiced candidates as 'invoiced'.

Before this fix, sending a Rechnung/Umbuchung set status back to 'done', which
made the loan reappear in the Rückgaben list (past date + 'done'). New sends
now set status='invoiced'; this backfills the rows invoiced under the old code.

A candidate is considered already invoiced if status='done' AND a
laufende_nummer was assigned (only the invoice send flow sets both).

DRY-RUN by default — nothing is written unless you pass --commit.

Run on the server inside the app container:
    docker compose exec app python migrate_invoiced_status.py            # preview
    docker compose exec app python migrate_invoiced_status.py --commit   # apply
"""
import sys

from vms.domain.database import get_session
from vms.domain.models import EmailCandidate


def main(commit: bool) -> None:
    changed = []  # (id, name, laufende_nummer, nummer_typ)

    with get_session() as s:
        rows = (s.query(EmailCandidate)
                 .filter(EmailCandidate.status == 'done',
                         EmailCandidate.laufende_nummer.isnot(None))
                 .order_by(EmailCandidate.id)
                 .all())
        for row in rows:
            changed.append((row.id, row.vorname_nachname,
                            row.laufende_nummer, row.nummer_typ))
            if commit:
                row.status = 'invoiced'
        # On dry-run we never mutated anything, so the auto-commit is a no-op.

    mode = 'APPLIED' if commit else 'DRY-RUN (no changes written)'
    print(f"=== {mode} ===\n")

    if changed:
        print(f"{len(changed)} candidate(s) set to status='invoiced':")
        for rid, name, nummer, typ in changed:
            print(f"  #{rid:<4} {name or '?':<30} {typ or '?'} Nr. {nummer}")
    else:
        print("Nothing to migrate — no 'done' candidates with laufende_nummer.")

    if not commit and changed:
        print("\nRe-run with --commit to apply.")


if __name__ == '__main__':
    main('--commit' in sys.argv)

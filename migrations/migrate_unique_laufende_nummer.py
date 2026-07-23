"""One-off migration: enforce unique invoice numbers per Nummernkreis.

Bis zur gesperrten Nummernvergabe (models.claim_sequential_number) war
`/api/sequential-number/<typ>` ein reines Peek ohne Reservierung: zwei Clients,
die vor dem Absenden peekten, bekamen dieselbe Nummer, und beide Sends wurden
akzeptiert. Der Applikations-Lock schließt das für neue Vorgänge; dieser
partielle Unique-Index ist die Absicherung darunter, falls ein künftiger
Schreibpfad die Vergabe umgeht.

Partiell (WHERE laufende_nummer IS NOT NULL), weil die große Mehrheit der
Kandidaten nie fakturiert wird und NULL-Zeilen nicht kollidieren sollen.

Prüft ZUERST auf bestehende Duplikate und bricht ab, statt blind zu migrieren:
Altbestände können durch den alten Fehler echte Doppelnummern enthalten, die
fachlich entschieden werden müssen (welche Rechnung behält die Nummer?).

DRY-RUN by default — nothing is written unless you pass --commit.

Run on the server inside the app container:
    docker compose exec app python migrate_unique_laufende_nummer.py            # preview
    docker compose exec app python migrate_unique_laufende_nummer.py --commit   # apply
"""
import sys

from sqlalchemy import text

from vms.domain.database import get_session

INDEX_NAME = 'uq_candidates_nummer_typ_laufende_nummer'

CREATE_INDEX = text(f"""
    CREATE UNIQUE INDEX IF NOT EXISTS {INDEX_NAME}
    ON email_candidates (nummer_typ, laufende_nummer)
    WHERE laufende_nummer IS NOT NULL
""")

FIND_DUPLICATES = text("""
    SELECT nummer_typ, laufende_nummer, COUNT(*) AS anzahl,
           array_agg(id ORDER BY id) AS ids
    FROM email_candidates
    WHERE laufende_nummer IS NOT NULL
    GROUP BY nummer_typ, laufende_nummer
    HAVING COUNT(*) > 1
    ORDER BY nummer_typ, laufende_nummer
""")


def main(commit: bool) -> None:
    with get_session() as s:
        duplikate = s.execute(FIND_DUPLICATES).all()

        if duplikate:
            print("=== ABBRUCH: bestehende Doppelnummern ===\n")
            print(f"{len(duplikate)} Nummer(n) sind mehrfach vergeben. Der "
                  f"Unique-Index kann erst angelegt werden, wenn diese "
                  f"fachlich aufgelöst sind:\n")
            for typ, nummer, anzahl, ids in duplikate:
                print(f"  {typ or '?':<10} Nr. {nummer:<10} {anzahl}x  "
                      f"Kandidaten {', '.join(str(i) for i in ids)}")
            print("\nJe Zeile entscheiden, welcher Vorgang die Nummer behält, "
                  "und den anderen umnummerieren. Danach erneut ausführen.")
            sys.exit(1)

        print("Keine Doppelnummern gefunden.\n")

        if commit:
            s.execute(CREATE_INDEX)
            print(f"=== APPLIED ===\n\nUnique-Index {INDEX_NAME} angelegt.")
        else:
            print("=== DRY-RUN (no changes written) ===\n")
            print(f"Würde anlegen:\n{CREATE_INDEX.text.strip()}")
            print("\nRe-run with --commit to apply.")


if __name__ == '__main__':
    main('--commit' in sys.argv)

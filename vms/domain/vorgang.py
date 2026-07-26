"""Domänenregeln eines Vorgangs: wohin er gehört und wann er ein Datum braucht.

Wahrheitsquelle: docs/specs/vorgangslisten-und-datumspflicht.md.

Vorher stand die Einsortierung an sechs Stellen mit je eigener Bedingung -- die
Listen filterten nach Start-, End- bzw. rohem Datumsstring und widersprachen sich
dadurch. Hier liegt sie einmal, rein und ohne I/O.

`heute` wird immer übergeben, nie hier ermittelt: nur so sind die Tagesgrenzen
(Endtag zählt noch zum Verleih) deterministisch testbar.
"""
from enum import Enum

from vms.domain.models import (
    TERMINAL_STATUSES,
    CandidateStatus,
    parse_flexible_date,
)


class Zielliste(str, Enum):
    """Die fünf Listen der Oberfläche. Ein Vorgang gehört in genau eine."""
    OFFEN = 'offen'              # Anfragen -> Offene Anfragen
    ERLEDIGT = 'erledigt'        # Anfragen -> Erledigte Anfragen
    RUECKGABEN = 'rueckgaben'    # Anfragen -> Rückgaben
    RECHNUNGEN = 'rechnungen'    # Rechnungen
    ARCHIV = 'archiv'            # Anfragen -> Vergangene


# Alle Stati außer PENDING. Ab diesen ist der Vorgang ein Verleih und braucht
# ein Datum -- die Ergänzung zu ACTIVE_STATUSES/TERMINAL_STATUSES in models.py.
VERLEIH_STATUSES = tuple(s for s in CandidateStatus if s is not CandidateStatus.PENDING)


class DatumErforderlich(ValueError):
    """Ein Verleih sollte ohne verwertbares Datum angelegt oder geändert werden."""


def massgebliches_enddatum(parsed_date, parsed_end):
    """Der Tag, an dem ein Verleih endet -- maßgeblich für "vorbei?".

    Gibt es ein Enddatum, zählt das. Gibt es keines, zählt das Startdatum.
    Bewusst nicht pauschal eines von beiden: nach dem Startdatum zu filtern
    zeigte mehrtägige Verleihe schon ab Tag zwei als vorbei an, während sie
    noch liefen.

    Ein Enddatum vor dem Startdatum ist Datenmüll und wird ignoriert, damit ein
    Zahlendreher einen Vorgang nicht vorzeitig verschwinden lässt.
    """
    if parsed_end is not None and (parsed_date is None or parsed_end >= parsed_date):
        return parsed_end
    return parsed_date


def zielliste(status, datum, end_date, heute):
    """Die eine Liste, in die ein Vorgang heute gehört.

    Total: jede Paarung aus Status und Datumslage ergibt genau eine Zielliste.
    Ein unbekannter Status wirft `ValueError`, statt still aus allen Listen zu
    fallen.
    """
    status = CandidateStatus(status)

    # Abgeschlossen ist abgeschlossen -- datumsunabhängig, auch bei vorzeitiger
    # Rückgabe.
    if status in TERMINAL_STATUSES:
        return Zielliste.ARCHIV

    # Eine offene Rechnung wird ausschließlich im Rechnungstab bearbeitet.
    if status is CandidateStatus.INVOICE_PENDING:
        return Zielliste.RECHNUNGEN

    ende = massgebliches_enddatum(parse_flexible_date(datum),
                                  parse_flexible_date(end_date))
    # Der Endtag gehört noch zum Verleih: erst ab dem Folgetag ist er vorbei.
    # Ohne verwertbares Datum ist ein Vorgang nie "vorbei" -- das hält eine
    # datumslose Anfrage offen und den datumslosen Altbestand sichtbar.
    ist_vorbei = ende is not None and ende < heute

    if status is CandidateStatus.PENDING:
        return Zielliste.ARCHIV if ist_vorbei else Zielliste.OFFEN

    return Zielliste.RUECKGABEN if ist_vorbei else Zielliste.ERLEDIGT


def require_datum(status, datum):
    """Stelle sicher, dass ein Verleih ein verwertbares Datum hat.

    Nur PENDING darf datumslos sein. Geprüft wird auf *parsbar*, nicht auf
    *nicht leer*: `to_iso_date` speichert Freitext ("nächste Woche") unverändert,
    und der verhält sich in jedem Zeitfilter wie ein fehlendes Datum.

    Wirft `DatumErforderlich`, sonst gibt sie nichts zurück.
    """
    if CandidateStatus(status) is CandidateStatus.PENDING:
        return
    if parse_flexible_date(datum) is None:
        raise DatumErforderlich('Ein Verleih braucht ein gültiges Datum.')

"""Rücknahme einer Rechnungsabsicht: „doch keine Rechnung erstellen".

Wahrheitsquelle: docs/specs/rechnung-doch-nicht-erstellen.md.

Wer beim Einzählen „& Rechnung erstellen" wählte, saß bisher fest: der Vorgang
erscheint nur noch im Rechnungen-Tab, und der kannte genau eine Aktion. Hier
liegt der Rückweg -- in den Zustand, den „nur eingezählt" von Anfang an erzeugt
hätte.

Die Rückgabe selbst wird dabei *nicht* wiederholt: `returned_at` und die
Lagerplätze gehören zum Einzählen, das längst passiert ist. Zurückgenommen wird
nur die Rechnungsabsicht -- deshalb tauchen beide hier gar nicht erst auf.
"""
from dataclasses import dataclass

from vms.domain.models import CandidateStatus


class RechnungsabbruchUnzulaessig(ValueError):
    """Aus diesem Status gibt es keine Rechnungsabsicht zurückzunehmen."""


# Nur diese beiden tragen eine Rechnungsabsicht. `returned` ist bereits im
# Zielzustand, `pending`/`processed`/`done` sind noch gar nicht zurück.
ABBRECHBARE_STATUSES = (CandidateStatus.INVOICE_PENDING, CandidateStatus.INVOICED)


@dataclass(frozen=True)
class Rechnungsabbruch:
    """Was nach dem Abbruch am Vorgang stehen soll.

    `freizugebende_nummer` ist der einzige Seiteneffekt, den die Regel anordnet,
    aber nicht selbst ausführen kann -- der Aufrufer reicht sie an
    `release_sequential_number` weiter.
    """
    status: str
    return_note: str | None
    nummer_typ: str | None
    laufende_nummer: str | None
    freizugebende_nummer: tuple[str, int] | None


def plan_rechnungsabbruch(status, nummer_typ, laufende_nummer,
                          bestehende_notiz, notiz):
    """Was beim Verwerfen der Rechnung am Vorgang stehen soll.

    Der Zielzustand ist immer `returned`; es gibt keinen zweiten Ausgang.

    Die laufende Nummer geht **nur** zurück in den Nummernkreis, wenn nie ein
    Dokument entstanden ist (`invoice_pending`) -- dort vergibt der
    Rückgabe-Endpunkt sie optional gleich mit, ohne dass je ein PDF folgte.
    Ist der Vorgang dagegen schon fakturiert, trägt das ausgelieferte PDF die
    Nummer: sie bleibt am Vorgang und darf nie ein zweites Mal vergeben werden.

    Wirft `RechnungsabbruchUnzulaessig`, wenn es nichts zurückzunehmen gibt.
    """
    status = CandidateStatus(status)
    if status not in ABBRECHBARE_STATUSES:
        raise RechnungsabbruchUnzulaessig(
            f'Vorgang im Status "{status.label}" hat keine Rechnung, '
            f'die zurückgenommen werden könnte')

    dokument_existiert = status is CandidateStatus.INVOICED

    return Rechnungsabbruch(
        status=CandidateStatus.RETURNED.value,
        return_note=_gueltige_notiz(notiz) or bestehende_notiz,
        nummer_typ=nummer_typ if dokument_existiert else None,
        laufende_nummer=laufende_nummer if dokument_existiert else None,
        freizugebende_nummer=(None if dokument_existiert
                              else _freizugebende_nummer(nummer_typ, laufende_nummer)),
    )


def _gueltige_notiz(notiz):
    """Die Notiz, sofern der Nutzer wirklich eine geschrieben hat, sonst None.

    Abbrechen ohne Angabe darf die beim Einzählen erfasste Notiz nicht löschen.
    """
    notiz = (notiz or '').strip()
    return notiz or None


def _freizugebende_nummer(nummer_typ, laufende_nummer):
    """Der Nummernkreis-Schritt, der zurückgedreht werden kann -- oder None.

    `laufende_nummer` ist eine Freitextspalte: steht dort etwas, das keine Zahl
    ist, lässt sich der Zähler nicht sinnvoll zurückdrehen. Das dann trotzdem zu
    versuchen wäre still folgenlos, weil `release_sequential_number` einen String
    gegen einen Integer vergliche. Hier wird es stattdessen benannt.
    """
    if not nummer_typ or not laufende_nummer:
        return None
    try:
        return (nummer_typ, int(laufende_nummer))
    except (TypeError, ValueError):
        return None

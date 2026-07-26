"""Tests für vms/domain/rechnungsabbruch.py -- „doch keine Rechnung erstellen".

Wahrheitsquelle: docs/specs/rechnung-doch-nicht-erstellen.md.

Reine Domänenregeln: die Funktion bekommt den Ist-Zustand als einfache Werte und
gibt zurück, was geschrieben werden soll. Kein App-Kontext, keine DB, kein I/O --
deshalb durchweg @pytest.mark.unit. Die Verdrahtung (Session, Kanboard, HTTP)
wird in tests/test_invoice_routes.py geprüft.
"""
import pytest

from vms.domain.rechnungsabbruch import (
    RechnungsabbruchUnzulaessig,
    plan_rechnungsabbruch,
)


# --------------------------------------------------------------------------
# Zielzustand: der Abbruch führt immer nach `returned` -- genau der Zustand,
# den „nur eingezählt" von Anfang an erzeugt hätte.
# --------------------------------------------------------------------------

@pytest.mark.unit
def test_abbruch_aus_invoice_pending_fuehrt_zu_returned():
    """Kriterium 1."""
    plan = plan_rechnungsabbruch(
        status="invoice_pending", nummer_typ=None, laufende_nummer=None,
        bestehende_notiz=None, notiz=None,
    )

    assert plan.status == "returned"


@pytest.mark.unit
def test_abbruch_aus_invoiced_fuehrt_zu_returned():
    """Kriterium 2: auch eine bereits erzeugte Rechnung lässt sich zurücknehmen."""
    plan = plan_rechnungsabbruch(
        status="invoiced", nummer_typ="rechnung", laufende_nummer="7",
        bestehende_notiz=None, notiz=None,
    )

    assert plan.status == "returned"


# --------------------------------------------------------------------------
# Nummernkreis: freigeben nur dort, wo nie ein Dokument entstanden ist.
# --------------------------------------------------------------------------

@pytest.mark.unit
def test_abbruch_aus_invoice_pending_gibt_nummer_frei():
    """Kriterium 3: kein PDF entstanden -> Nummer löschen und zurückgeben,
    damit der Nummernkreis keine Lücke bekommt."""
    plan = plan_rechnungsabbruch(
        status="invoice_pending", nummer_typ="rechnung", laufende_nummer="12",
        bestehende_notiz=None, notiz=None,
    )

    assert plan.laufende_nummer is None
    assert plan.nummer_typ is None
    assert plan.freizugebende_nummer == ("rechnung", 12)


@pytest.mark.unit
def test_abbruch_aus_invoiced_behaelt_nummer():
    """Kriterium 4: das PDF existiert und trägt die Nummer. Sie bleibt dem
    Vorgang zugeordnet und darf nie ein zweites Mal vergeben werden."""
    plan = plan_rechnungsabbruch(
        status="invoiced", nummer_typ="umbuchung", laufende_nummer="4",
        bestehende_notiz=None, notiz=None,
    )

    assert plan.laufende_nummer == "4"
    assert plan.nummer_typ == "umbuchung"
    assert plan.freizugebende_nummer is None


@pytest.mark.unit
def test_abbruch_ohne_nummer_gibt_nichts_frei():
    """Kriterium 5: der Regelfall -- beim Einzählen wurde keine Nummer vergeben."""
    plan = plan_rechnungsabbruch(
        status="invoice_pending", nummer_typ=None, laufende_nummer=None,
        bestehende_notiz=None, notiz=None,
    )

    assert plan.freizugebende_nummer is None
    assert plan.laufende_nummer is None
    assert plan.nummer_typ is None


@pytest.mark.unit
@pytest.mark.parametrize("unlesbar", ["", "R-2024/1", "keine"])
def test_abbruch_mit_unlesbarer_nummer_gibt_nichts_frei(unlesbar):
    """Kriterium 6: `laufende_nummer` ist eine Freitextspalte (String(30)).
    Ein nicht-numerischer Altwert lässt sich im Zähler nicht zurückdrehen --
    release_sequential_number verglich einen String mit einem Integer und täte
    schweigend nichts. Der Vorgang wird trotzdem sauber geleert."""
    plan = plan_rechnungsabbruch(
        status="invoice_pending", nummer_typ="rechnung", laufende_nummer=unlesbar,
        bestehende_notiz=None, notiz=None,
    )

    assert plan.freizugebende_nummer is None
    assert plan.laufende_nummer is None
    assert plan.nummer_typ is None


# --------------------------------------------------------------------------
# Notiz: optional, ersetzt -- Abbrechen ohne Angabe darf nichts löschen.
# --------------------------------------------------------------------------

@pytest.mark.unit
def test_abbruch_mit_notiz_ersetzt_return_note():
    """Kriterium 7."""
    plan = plan_rechnungsabbruch(
        status="invoice_pending", nummer_typ=None, laufende_nummer=None,
        bestehende_notiz="Alles vollständig zurück", notiz="Doch nichts abzurechnen",
    )

    assert plan.return_note == "Doch nichts abzurechnen"


@pytest.mark.unit
@pytest.mark.parametrize("leer", [None, "", "   "])
def test_abbruch_ohne_notiz_behaelt_bestehende_return_note(leer):
    """Kriterium 8: die beim Einzählen erfasste Notiz bleibt stehen."""
    plan = plan_rechnungsabbruch(
        status="invoice_pending", nummer_typ=None, laufende_nummer=None,
        bestehende_notiz="Alles vollständig zurück", notiz=leer,
    )

    assert plan.return_note == "Alles vollständig zurück"


# --------------------------------------------------------------------------
# Verbotene Ausgangszustände.
# --------------------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("status", ["pending", "processed", "done", "returned"])
def test_abbruch_aus_nicht_abbrechbarem_status_wirft(status):
    """Kriterium 9: nur invoice_pending und invoiced haben eine Rechnungsabsicht
    zurückzunehmen. `returned` ist schon im Zielzustand -- ein zweiter Abbruch
    wird abgelehnt statt still bestätigt."""
    with pytest.raises(RechnungsabbruchUnzulaessig):
        plan_rechnungsabbruch(
            status=status, nummer_typ=None, laufende_nummer=None,
            bestehende_notiz=None, notiz=None,
        )

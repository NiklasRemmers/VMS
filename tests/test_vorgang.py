"""Tests für vms/domain/vorgang.py -- die Einsortierungs- und Datumsregeln.

Wahrheitsquelle: docs/specs/vorgangslisten-und-datumspflicht.md.

Alle Tests hier sind rein: `heute` wird der Funktion übergeben statt in ihr
ermittelt, damit die Tagesgrenzen ohne freeze_time deterministisch sind. Genau
dafür ist der Parameter da -- wer ihn weglässt, kann den Endtag nicht testen.
"""
import pytest

from datetime import date

from vms.domain.models import CandidateStatus
from vms.domain.vorgang import (
    DatumErforderlich,
    Zielliste,
    massgebliches_enddatum,
    require_datum,
    zielliste,
)

HEUTE = date(2026, 7, 26)
GESTERN = "2026-07-25"
MORGEN = "2026-07-27"
HEUTE_ISO = "2026-07-26"
LETZTE_WOCHE = "2026-07-19"

VERLEIH_LAEUFT = (CandidateStatus.PROCESSED, CandidateStatus.DONE)


# --- Offene Anfragen -------------------------------------------------------

@pytest.mark.unit
def test_pending_ohne_datum_bleibt_offen():
    """Eine Anfrage darf datumslos sein -- sie ist noch nicht terminiert."""
    assert zielliste(CandidateStatus.PENDING, None, None, HEUTE) == Zielliste.OFFEN


@pytest.mark.unit
def test_pending_mit_zukunftsdatum_ist_offen():
    assert zielliste(CandidateStatus.PENDING, MORGEN, None, HEUTE) == Zielliste.OFFEN


@pytest.mark.unit
def test_pending_mit_verstrichenem_termin_geht_ins_archiv():
    """Eine nie bearbeitete Anfrage verfällt, statt in 'Offen' zu verharren."""
    assert zielliste(CandidateStatus.PENDING, GESTERN, None, HEUTE) == Zielliste.ARCHIV


# --- Erledigte Anfragen vs. Rückgaben: die Tagesgrenze ---------------------

@pytest.mark.unit
@pytest.mark.parametrize("status", VERLEIH_LAEUFT)
def test_verleih_am_endtag_bleibt_erledigt(status):
    """Läuft ein Verleih bis heute, ist er heute noch nicht zurückzugeben."""
    assert zielliste(status, LETZTE_WOCHE, HEUTE_ISO, HEUTE) == Zielliste.ERLEDIGT


@pytest.mark.unit
@pytest.mark.parametrize("status", VERLEIH_LAEUFT)
def test_verleih_ab_tag_nach_ende_ist_rueckgabe(status):
    assert zielliste(status, LETZTE_WOCHE, GESTERN, HEUTE) == Zielliste.RUECKGABEN


@pytest.mark.unit
@pytest.mark.parametrize("status", VERLEIH_LAEUFT)
def test_laufender_mehrtagesverleih_bleibt_erledigt(status):
    """Der Kernfehler der alten Logik: gefiltert wurde nach dem Startdatum,
    wodurch ein laufender Verleih ab Tag zwei aus der Arbeitsliste fiel."""
    assert zielliste(status, GESTERN, MORGEN, HEUTE) == Zielliste.ERLEDIGT


@pytest.mark.unit
@pytest.mark.parametrize("status", VERLEIH_LAEUFT)
def test_eintagesverleih_am_verleihtag_ist_nicht_doppelt(status):
    """Stand vorher gleichzeitig in 'Erledigte Anfragen' und in 'Rückgaben'."""
    assert zielliste(status, HEUTE_ISO, None, HEUTE) == Zielliste.ERLEDIGT


@pytest.mark.unit
@pytest.mark.parametrize("status", VERLEIH_LAEUFT)
def test_verleih_ohne_datum_faellt_auf_erledigt_zurueck(status):
    """Kann nur Altbestand sein (die Datumspflicht verhindert Neuzugänge).
    Er bleibt sichtbar, statt aus allen Listen zu fallen."""
    assert zielliste(status, None, None, HEUTE) == Zielliste.ERLEDIGT


# --- Rechnungen und Archiv -------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("datum", [GESTERN, MORGEN, None])
def test_invoice_pending_bleibt_im_rechnungstab(datum):
    """Datumsunabhängig -- vorher zog der Datums-Zweig des Archivs ihn zusätzlich
    unter 'Vergangene'."""
    assert zielliste(CandidateStatus.INVOICE_PENDING, datum, None, HEUTE) == \
        Zielliste.RECHNUNGEN


@pytest.mark.unit
@pytest.mark.parametrize("status", [CandidateStatus.RETURNED, CandidateStatus.INVOICED])
@pytest.mark.parametrize("datum", [GESTERN, MORGEN, None])
def test_abgeschlossener_vorgang_ist_immer_archiv(status, datum):
    """Auch bei vorzeitiger Rückgabe: abgeschlossen ist abgeschlossen."""
    assert zielliste(status, datum, None, HEUTE) == Zielliste.ARCHIV


# --- Randfälle -------------------------------------------------------------

@pytest.mark.unit
def test_enddatum_vor_startdatum_wird_ignoriert():
    """Ein Zahlendreher ist Datenmüll und darf einen Verleih nicht vorzeitig
    in die Rückgaben schieben -- maßgeblich bleibt dann das Startdatum."""
    assert massgebliches_enddatum(date(2026, 8, 1), date(2026, 7, 1)) == date(2026, 8, 1)

    assert zielliste(CandidateStatus.PROCESSED, MORGEN, LETZTE_WOCHE, HEUTE) == \
        Zielliste.ERLEDIGT


@pytest.mark.unit
def test_unparsbares_datum_verhaelt_sich_wie_kein_datum():
    """to_iso_date reicht Unparsbares unverändert durch; die Einsortierung darf
    darauf nicht hereinfallen."""
    assert zielliste(CandidateStatus.PENDING, "nächste Woche", None, HEUTE) == \
        Zielliste.OFFEN


@pytest.mark.unit
def test_unbekannter_status_wirft():
    """Ein Status außerhalb des Enums darf nicht still aus allen Listen fallen."""
    with pytest.raises(ValueError):
        zielliste("voellig_unbekannt", MORGEN, None, HEUTE)


@pytest.mark.unit
@pytest.mark.parametrize("status", list(CandidateStatus))
@pytest.mark.parametrize("datum,end_date", [
    (None, None),
    (GESTERN, None),
    (HEUTE_ISO, None),
    (MORGEN, None),
    (GESTERN, MORGEN),
    (GESTERN, GESTERN),
    (MORGEN, LETZTE_WOCHE),
    ("nächste Woche", None),
])
def test_jede_kombination_hat_genau_eine_zielliste(status, datum, end_date):
    """Totalität: es gibt keine Paarung aus Status und Datumslage ohne Zielliste.
    Das ist die Eigenschaft, die 'nirgends angezeigt' strukturell ausschließt."""
    assert zielliste(status, datum, end_date, HEUTE) in set(Zielliste)


# --- Datumspflicht ---------------------------------------------------------

@pytest.mark.unit
def test_pending_darf_ohne_datum_sein():
    require_datum(CandidateStatus.PENDING, None)  # wirft nicht


@pytest.mark.unit
@pytest.mark.parametrize("status", [
    CandidateStatus.PROCESSED, CandidateStatus.DONE, CandidateStatus.INVOICE_PENDING,
    CandidateStatus.RETURNED, CandidateStatus.INVOICED,
])
@pytest.mark.parametrize("datum", [None, "", "   "])
def test_verleih_ohne_datum_wird_abgelehnt(status, datum):
    with pytest.raises(DatumErforderlich):
        require_datum(status, datum)


@pytest.mark.unit
def test_unparsbares_datum_zaehlt_als_kein_datum():
    """'Nicht leer' genügt nicht: to_iso_date speichert Freitext unverändert,
    und in jedem Zeitfilter verhält der sich wie ein fehlendes Datum."""
    with pytest.raises(DatumErforderlich):
        require_datum(CandidateStatus.PROCESSED, "nächste Woche")


@pytest.mark.unit
@pytest.mark.parametrize("datum", ["2026-08-01", "01.08.2026", "1.8.26"])
def test_verleih_mit_datum_wird_akzeptiert(datum):
    require_datum(CandidateStatus.PROCESSED, datum)  # wirft nicht

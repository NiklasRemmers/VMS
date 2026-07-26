"""Tests für email_routes.py -- die 14 E-Mail-/Kandidaten-Routen.

Aus tests/test_app_routes.py hierher gezogen, nachdem die Routen in Block B von
app.py nach email_routes.py gewandert sind.
"""
import pytest


# --------------------------------------------------------------------------
# Diese beiden Routen reichen Argumente positionsabhängig an email_client
# weiter. Beim Entfernen des toten user_id-Parameters (Block E) verschoben sich
# dort die Positionen -- ein Fehler hätte hier still falsche Daten geliefert,
# statt zu werfen. Deshalb explizit abgedeckt.
# --------------------------------------------------------------------------

def _make_candidate(db_session, user_id, **overrides):
    from vms.domain.models import EmailCandidate
    defaults = dict(user_id=user_id, vorname_nachname="Erika Mustermann",
                    veranstaltungsname="Sommerfest", datum="2024-05-01",
                    status="processed", tags=[])
    defaults.update(overrides)
    c = EmailCandidate(**defaults)
    db_session.add(c)
    db_session.flush()
    return c.id


@pytest.mark.route
def test_calendar_events_returns_candidate_in_window(auth_client, user, db_session):
    _make_candidate(db_session, user["id"], veranstaltungsname="Sommerfest",
                    datum="2024-05-01", email_id="cal-1")
    db_session.commit()

    resp = auth_client.get("/api/calendar/events?start=2024-04-01&end=2024-06-01")

    assert resp.status_code == 200
    titles = [e["title"] for e in resp.get_json()]
    assert "Sommerfest" in titles


@pytest.mark.route
def test_calendar_events_respects_the_window_boundaries(auth_client, user, db_session):
    """Belegt, dass start/end wirklich als Fenster ankommen -- und nicht durch
    eine Positionsverschiebung vertauscht oder ignoriert werden."""
    _make_candidate(db_session, user["id"], veranstaltungsname="Weit weg",
                    datum="2023-01-15", email_id="cal-2")
    db_session.commit()

    resp = auth_client.get("/api/calendar/events?start=2024-04-01&end=2024-06-01")

    assert resp.status_code == 200
    assert [e["title"] for e in resp.get_json()] == []


@pytest.mark.route
def test_archive_paginates_and_honours_limit(auth_client, user, db_session):
    for i in range(3):
        _make_candidate(db_session, user["id"], veranstaltungsname=f"Alt {i}",
                        datum="2020-01-0%d" % (i + 1), status="returned",
                        email_id=f"arch-{i}")
    db_session.commit()

    resp = auth_client.get("/api/emails/archive?page=1&limit=2")

    assert resp.status_code == 200
    body = resp.get_json()
    assert len(body["items"]) == 2
    assert body["total"] == 3


@pytest.mark.route
def test_archive_excludes_active_loan_awaiting_return(auth_client, user, db_session):
    """Ein laufender Verleih (processed/done), dessen Datum vorbei ist, aber der
    noch nicht als Rückgabe markiert wurde, gehört in die Rückgaben -- nicht ins
    Archiv. Vorher zog das Archiv jeden vergangenen Vorgang unabhängig vom Status,
    sodass ein Verleih ohne Vertrag direkt als "archiviert" erschien, ohne je
    zurückgegeben worden zu sein."""
    from datetime import date, timedelta
    gestern = (date.today() - timedelta(days=2)).strftime("%d.%m.%Y")

    _make_candidate(db_session, user["id"], veranstaltungsname="Noch offen",
                    datum=gestern, status="processed", contract_created=False,
                    email_id="arch-active-1")
    _make_candidate(db_session, user["id"], veranstaltungsname="Zurückgegeben",
                    datum=gestern, status="returned", email_id="arch-active-2")
    db_session.commit()

    titles = [i["veranstaltungsname"]
              for i in auth_client.get("/api/emails/archive").get_json()["items"]]

    # Der zurückgegebene (terminale) Vorgang bleibt archiviert ...
    assert "Zurückgegeben" in titles
    # ... der noch offene laufende Verleih nicht.
    assert "Noch offen" not in titles


@pytest.mark.route
def test_archive_search_query_filters(auth_client, user, db_session):
    _make_candidate(db_session, user["id"], veranstaltungsname="Sommerfest",
                    datum="2020-01-01", status="returned", email_id="arch-a")
    _make_candidate(db_session, user["id"], veranstaltungsname="Winterball",
                    datum="2020-01-02", status="returned", email_id="arch-b")
    db_session.commit()

    resp = auth_client.get("/api/emails/archive?q=Winterball")

    assert resp.status_code == 200
    titles = [i["veranstaltungsname"] for i in resp.get_json()["items"]]
    assert titles == ["Winterball"]


# --------------------------------------------------------------------------
# Datums-Konsolidierung (Block C): fünf Endpunkte bauten dieselbe Parse-Logik
# von Hand nach. Diese Tests decken die Zeitraum-Entscheidungen ab, die dabei
# umgeschrieben wurden -- sie liefen vorher ungetestet.
# --------------------------------------------------------------------------

@pytest.mark.route
def test_returns_lists_only_past_active_loans(auth_client, user, db_session):
    from datetime import date, timedelta
    gestern = (date.today() - timedelta(days=2)).strftime("%d.%m.%Y")
    morgen = (date.today() + timedelta(days=5)).strftime("%d.%m.%Y")

    _make_candidate(db_session, user["id"], veranstaltungsname="Vorbei",
                    datum=gestern, status="done", email_id="ret-1")
    _make_candidate(db_session, user["id"], veranstaltungsname="Kommt noch",
                    datum=morgen, status="done", email_id="ret-2")
    _make_candidate(db_session, user["id"], veranstaltungsname="Schon zurück",
                    datum=gestern, status="returned", email_id="ret-3")
    db_session.commit()

    resp = auth_client.get("/api/emails/returns")

    assert resp.status_code == 200
    namen = [c["veranstaltungsname"] for c in resp.get_json()["items"]]
    assert namen == ["Vorbei"]


# --------------------------------------------------------------------------
# Wann ist ein Verleih "vorbei"? Regel: gibt es ein Enddatum, zählt das --
# sonst das Startdatum. Vorher wertete get_returns "Start ODER Ende vergangen"
# aus und zeigte laufende mehrtägige Verleihe schon in den Rückgaben an,
# während /api/emails/candidates/for-contract dieselbe Frage anders beantwortete.
# --------------------------------------------------------------------------

@pytest.mark.route
def test_returns_waits_for_the_end_date_of_a_running_loan(auth_client, user, db_session):
    """Gestern begonnen, endet übermorgen: läuft noch, gehört nicht in Rückgaben."""
    from datetime import date, timedelta
    start = (date.today() - timedelta(days=1)).strftime("%d.%m.%Y")
    ende = (date.today() + timedelta(days=3)).strftime("%Y-%m-%d")

    _make_candidate(db_session, user["id"], veranstaltungsname="Läuft noch",
                    datum=start, end_date=ende, status="done", email_id="ret-span")
    db_session.commit()

    resp = auth_client.get("/api/emails/returns")

    assert [c["veranstaltungsname"] for c in resp.get_json()["items"]] == []


@pytest.mark.route
def test_returns_shows_a_loan_once_its_end_date_has_passed(auth_client, user, db_session):
    from datetime import date, timedelta
    start = (date.today() - timedelta(days=10)).strftime("%d.%m.%Y")
    ende = (date.today() - timedelta(days=2)).strftime("%Y-%m-%d")

    _make_candidate(db_session, user["id"], veranstaltungsname="Abgelaufen",
                    datum=start, end_date=ende, status="done", email_id="ret-over")
    db_session.commit()

    resp = auth_client.get("/api/emails/returns")

    assert [c["veranstaltungsname"] for c in resp.get_json()["items"]] == ["Abgelaufen"]


@pytest.mark.route
def test_returns_falls_back_to_the_start_date_without_an_end_date(auth_client, user, db_session):
    from datetime import date, timedelta
    gestern = (date.today() - timedelta(days=2)).strftime("%d.%m.%Y")

    _make_candidate(db_session, user["id"], veranstaltungsname="Eintägig",
                    datum=gestern, status="done", email_id="ret-single")
    db_session.commit()

    resp = auth_client.get("/api/emails/returns")

    assert [c["veranstaltungsname"] for c in resp.get_json()["items"]] == ["Eintägig"]


@pytest.mark.unit
def test_massgebliches_enddatum_ignores_an_end_before_the_start():
    """Zahlendreher im Enddatum darf einen Vorgang nicht vorzeitig ausblenden."""
    from datetime import date
    from vms.routes.email import massgebliches_enddatum

    start, ende = date(2026, 5, 10), date(2026, 5, 14)
    assert massgebliches_enddatum(start, ende) == ende
    assert massgebliches_enddatum(start, None) == start
    assert massgebliches_enddatum(None, ende) == ende
    # Ende vor Start = Datenmüll -> Startdatum gewinnt
    assert massgebliches_enddatum(date(2026, 5, 10), date(2026, 4, 1)) == date(2026, 5, 10)
    assert massgebliches_enddatum(None, None) is None


@pytest.mark.route
def test_for_contract_exposes_iso_date_and_skips_past_loans(auth_client, user, db_session):
    from datetime import date, timedelta
    morgen = (date.today() + timedelta(days=5)).strftime("%d.%m.%Y")
    laengst = (date.today() - timedelta(days=30)).strftime("%d.%m.%Y")

    _make_candidate(db_session, user["id"], veranstaltungsname="Anstehend",
                    datum=morgen, status="processed", email_id="fc-1")
    _make_candidate(db_session, user["id"], veranstaltungsname="Vergangen",
                    datum=laengst, status="processed", email_id="fc-2")
    db_session.commit()

    resp = auth_client.get("/api/emails/candidates/for-contract")

    assert resp.status_code == 200
    body = resp.get_json()
    assert [c["veranstaltungsname"] for c in body] == ["Anstehend"]
    # datum_iso ist das Feld, auf das das Vertragsformular zugreift.
    erwartet_iso = (date.today() + timedelta(days=5)).strftime("%Y-%m-%d")
    assert body[0]["datum_iso"] == erwartet_iso


@pytest.mark.route
def test_for_contract_keeps_an_unparsable_datum_verbatim(auth_client, user, db_session):
    """Vorher fiel der except-Zweig auf den Rohwert zurück -- to_iso_date tut
    dasselbe, statt das Feld zu leeren."""
    _make_candidate(db_session, user["id"], veranstaltungsname="Krummes Datum",
                    datum="wird noch geklärt", status="processed", email_id="fc-3")
    db_session.commit()

    body = auth_client.get("/api/emails/candidates/for-contract").get_json()

    treffer = [c for c in body if c["veranstaltungsname"] == "Krummes Datum"]
    assert treffer and treffer[0]["datum_iso"] == "wird noch geklärt"


@pytest.mark.route
def test_paged_candidates_accepts_the_german_range_shorthand(auth_client, user, db_session):
    """Die handkopierten Blöcke verwarfen '13.-15.11.26'; die kanonische
    Implementierung löst es auf den Starttag auf."""
    _make_candidate(db_session, user["id"], veranstaltungsname="Bereich",
                    datum="13.-15.11.99", status="pending", email_id="pg-1")
    db_session.commit()

    resp = auth_client.get("/api/emails/candidates/paged?direction=future")

    assert resp.status_code == 200
    namen = [c["veranstaltungsname"] for c in resp.get_json()["items"]]
    assert "Bereich" in namen


# --------------------------------------------------------------------------
# POST /api/emails/candidates/<id>/return -- Rückgabe-Workflow (email_routes.py
# ~397-464). Vergabe-/Nummernkreis-Verhalten (claim_sequential_number-Pfade)
# ist bereits in tests/test_sequential_number.py gepinnt (u.a. invalid action,
# unknown id, unknown nummer_typ, non-numeric nummer, advance/keep-max des
# Zählers, NummerBereitsVergeben). Hier: Status-/Datenwirkung des Returns
# selbst -- StorageLocation-Freigabe, Notiz-Handling, tz-aware returned_at,
# und der Rollback-Vertrag außerhalb des with-Blocks.
# --------------------------------------------------------------------------

@pytest.mark.route
def test_return_candidate_action_returned_sets_status_and_frees_storage(
        auth_client, user, db_session, mock_kanboard):
    from datetime import timezone
    from vms.domain.models import EmailCandidate, StorageLocation, CandidateStatus

    cid = _make_candidate(db_session, user["id"], status="processed")
    loc = StorageLocation(name="Schrank 1", candidate_id=cid)
    db_session.add(loc)
    db_session.commit()

    resp = auth_client.post(f"/api/emails/candidates/{cid}/return", json={
        "action": "returned", "note": "Alles vollständig zurück",
    })

    assert resp.status_code == 200
    db_session.expire_all()
    row = db_session.query(EmailCandidate).filter_by(id=cid).one()
    assert row.status == CandidateStatus.RETURNED.value
    assert row.return_note == "Alles vollständig zurück"
    assert row.returned_at is not None
    assert row.returned_at.tzinfo is not None  # tz-aware, not a naive datetime

    freed = db_session.query(StorageLocation).filter_by(id=loc.id).one()
    assert freed.candidate_id is None


@pytest.mark.route
def test_return_candidate_action_invoice_sets_invoice_pending_status(
        auth_client, user, db_session, mock_kanboard):
    from vms.domain.models import EmailCandidate, CandidateStatus

    cid = _make_candidate(db_session, user["id"], status="done")
    db_session.commit()

    resp = auth_client.post(f"/api/emails/candidates/{cid}/return", json={
        "action": "invoice",
    })

    assert resp.status_code == 200
    db_session.expire_all()
    row = db_session.query(EmailCandidate).filter_by(id=cid).one()
    assert row.status == CandidateStatus.INVOICE_PENDING.value
    assert row.returned_at is not None


@pytest.mark.route
def test_return_candidate_empty_note_persists_as_none(
        auth_client, user, db_session, mock_kanboard):
    from vms.domain.models import EmailCandidate

    cid = _make_candidate(db_session, user["id"], status="processed")
    db_session.commit()

    resp = auth_client.post(f"/api/emails/candidates/{cid}/return", json={
        "action": "returned", "note": "",
    })

    assert resp.status_code == 200
    db_session.expire_all()
    row = db_session.query(EmailCandidate).filter_by(id=cid).one()
    assert row.return_note is None


@pytest.mark.route
def test_return_candidate_money_path_claims_number_and_matches_counter(
        auth_client, user, db_session, mock_kanboard):
    """Money-Pfad: eine explizit vorgegebene freie Nummer wird über
    claim_sequential_number reserviert und am Vorgang gespeichert."""
    from vms.domain.models import EmailCandidate, SequentialNumber

    cid = _make_candidate(db_session, user["id"], status="done")
    db_session.commit()

    resp = auth_client.post(f"/api/emails/candidates/{cid}/return", json={
        "action": "invoice", "laufende_nummer": "42", "nummer_typ": "umbuchung",
    })

    assert resp.status_code == 200
    db_session.expire_all()
    row = db_session.query(EmailCandidate).filter_by(id=cid).one()
    assert row.laufende_nummer == "42"
    assert row.nummer_typ == "umbuchung"

    seq = db_session.query(SequentialNumber).filter_by(number_type="umbuchung").one()
    assert seq.last_number == 42


@pytest.mark.route
def test_return_candidate_rejects_a_zero_laufende_nummer(
        auth_client, user, db_session, mock_kanboard):
    """claim_sequential_number lehnt <= 0 ab (models.py:408-410) -- '0' ist
    truthy und käme sonst als echte Nummer durch."""
    from vms.domain.models import EmailCandidate, SequentialNumber

    cid = _make_candidate(db_session, user["id"], status="done")
    db_session.commit()

    resp = auth_client.post(f"/api/emails/candidates/{cid}/return", json={
        "action": "invoice", "laufende_nummer": "0", "nummer_typ": "rechnung",
    })

    assert resp.status_code == 400
    # Rollback-Vertrag: die ganze Transaktion (inkl. der ON-CONFLICT-Zeile aus
    # claim_sequential_number) fällt zurück, kein Zähler bleibt liegen.
    db_session.expire_all()
    assert db_session.query(SequentialNumber).filter_by(number_type="rechnung").first() is None
    row = db_session.query(EmailCandidate).filter_by(id=cid).one()
    assert row.status == "done"
    assert row.returned_at is None


@pytest.mark.route
def test_return_candidate_rollback_contract_on_number_conflict(
        auth_client, user, db_session, mock_kanboard):
    """Pinnt den Kommentar email_routes.py:428-431: Fehlerbehandlung liegt
    bewusst außerhalb des with-Blocks, damit ein Fehler die schon gesetzten
    Felder (status, returned_at) NICHT festschreibt."""
    from vms.domain.models import EmailCandidate

    belegt = _make_candidate(db_session, user["id"], email_id="belegt",
                             status="invoiced", laufende_nummer="7", nummer_typ="rechnung")
    cid = _make_candidate(db_session, user["id"], email_id="offen", status="done")
    db_session.commit()

    resp = auth_client.post(f"/api/emails/candidates/{cid}/return", json={
        "action": "invoice", "laufende_nummer": "7", "nummer_typ": "rechnung",
    })

    assert resp.status_code == 409
    db_session.expire_all()
    row = db_session.query(EmailCandidate).filter_by(id=cid).one()
    # Vor-Rückgabe-Zustand ist unverändert stehen geblieben -- kein
    # returned_at/status/laufende_nummer wurde committet.
    assert row.status == "done"
    assert row.returned_at is None
    assert row.laufende_nummer is None
    assert row.return_note is None


@pytest.mark.route
def test_return_candidate_rejects_returning_an_already_returned_candidate(
        auth_client, user, db_session, mock_kanboard):
    from datetime import datetime, timezone
    from vms.domain.models import EmailCandidate, CandidateStatus

    original_returned_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    cid = _make_candidate(db_session, user["id"], status=CandidateStatus.RETURNED.value,
                          returned_at=original_returned_at, return_note="erste Rückgabe")
    db_session.commit()

    resp = auth_client.post(f"/api/emails/candidates/{cid}/return", json={
        "action": "returned", "note": "zweite Rückgabe",
    })

    assert resp.status_code in (400, 409)
    db_session.expire_all()
    row = db_session.query(EmailCandidate).filter_by(id=cid).one()
    assert row.returned_at == original_returned_at
    assert row.return_note == "erste Rückgabe"


@pytest.mark.route
def test_return_candidate_without_json_body_returns_400(auth_client, user, db_session):
    cid = _make_candidate(db_session, user["id"], status="processed")
    db_session.commit()

    resp = auth_client.post(f"/api/emails/candidates/{cid}/return")

    assert resp.status_code == 400

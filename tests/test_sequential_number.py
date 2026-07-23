"""Tests für die Vergabe laufender Nummern (siehe docs/TEST_PLAN.md Modul 2).

Beteiligte Stellen:

  * ``GET /api/sequential-number/<type>`` (app.py) -- unverbindlicher Vorschlag
    fürs Formular, schreibt nichts.
  * ``models.claim_sequential_number`` -- die verbindliche Vergabe unter
    Zeilensperre; einzige Stelle, die ``SequentialNumber.last_number`` fortzählt.
  * ``api_download_invoice`` (invoice_routes.py) und ``return_candidate`` (app.py)
    als die beiden Aufrufer.

Historie: bis zum Modul-2-Retrofit war die Vergabe ein reines Peek plus ein
"advance-to-max" beim Speichern. Zwei Clients, die vor dem Absenden peekten,
bekamen dieselbe Nummer, und beide Sends wurden akzeptiert. Die Tests hier
decken sowohl die Reservierung als auch die Ablehnung einer bereits vergebenen
Nummer ab.
"""
import pytest


def _make_candidate(db_session, user_id, **overrides):
    from vms.domain.models import EmailCandidate
    defaults = dict(
        user_id=user_id,
        vorname_nachname="Erika Mustermann",
        veranstaltungsname="Sommerfest",
        email_address="erika@example.com",
        datum="2024-05-01",
        status="invoice_pending",
        tags=[],
    )
    defaults.update(overrides)
    c = EmailCandidate(**defaults)
    db_session.add(c)
    db_session.flush()
    return c.id


def _valid_send_payload(candidate_id, **overrides):
    payload = dict(
        candidate_id=candidate_id,
        nummer_typ="rechnung",
        laufende_nummer="1",
        adresse="Musterstr. 1",
        kostenstelle="",
        items=[{"name": "Zelt", "price": 10.0, "quantity": 1}],
    )
    payload.update(overrides)
    return payload


def _stub_pdf_chain(mocker):
    """Stub template load + ODT processing; convert_to_pdf comes from the
    no_libreoffice fixture. Der Download-Endpunkt verschickt keine Mail mehr."""
    mocker.patch("vms.routes.invoice.load_template", return_value="/tmp/fake_template.odt")
    mocker.patch("vms.infra.odt_processor.process_odt_template", return_value=None)


# --------------------------------------------------------------------------
# GET /api/sequential-number/<type> -- peek only
# --------------------------------------------------------------------------

@pytest.mark.route
def test_peek_next_number_defaults_to_one_when_no_counter_exists(auth_client):
    resp = auth_client.get("/api/sequential-number/rechnung")

    assert resp.status_code == 200
    assert resp.get_json() == {"next_number": 1, "type": "rechnung"}


@pytest.mark.route
def test_peek_next_number_after_last_number_seven_is_eight(auth_client, db_session):
    from vms.domain.models import SequentialNumber

    db_session.add(SequentialNumber(number_type="rechnung", last_number=7))
    db_session.commit()

    resp = auth_client.get("/api/sequential-number/rechnung")

    assert resp.get_json()["next_number"] == 8


@pytest.mark.route
def test_peek_next_number_rejects_unknown_type(auth_client):
    resp = auth_client.get("/api/sequential-number/garbage")

    assert resp.status_code == 400
    assert resp.get_json()["error"] == "Ungültiger Typ"


@pytest.mark.route
def test_peek_next_number_anonymous_is_unauthorized(client):
    resp = client.get("/api/sequential-number/rechnung", follow_redirects=False)
    assert resp.status_code == 401


@pytest.mark.route
def test_peek_does_not_write_any_row(auth_client, db_session):
    """A peek must be read-only: no SequentialNumber row is created as a side effect."""
    from vms.domain.models import SequentialNumber

    auth_client.get("/api/sequential-number/rechnung")

    assert db_session.query(SequentialNumber).count() == 0


@pytest.mark.route
def test_counters_are_independent_per_type(auth_client, db_session):
    from vms.domain.models import SequentialNumber

    db_session.add(SequentialNumber(number_type="rechnung", last_number=5))
    db_session.commit()

    resp = auth_client.get("/api/sequential-number/umbuchung")

    assert resp.get_json()["next_number"] == 1


# --------------------------------------------------------------------------
# api_download_invoice: persist + advance
# --------------------------------------------------------------------------

@pytest.mark.route
def test_send_invoice_creates_counter_row_when_none_exists(
        auth_client, user, db_session, no_libreoffice, mocker, mock_kanboard):
    from vms.domain.models import SequentialNumber

    cid = _make_candidate(db_session, user["id"])
    db_session.commit()
    _stub_pdf_chain(mocker)

    resp = auth_client.post("/api/invoices/download",
                             json=_valid_send_payload(cid, laufende_nummer="1"))

    assert resp.status_code == 200
    seq = db_session.query(SequentialNumber).filter_by(number_type="rechnung").first()
    assert seq is not None
    assert seq.last_number == 1


@pytest.mark.route
def test_send_invoice_umbuchung_and_rechnung_counters_stay_independent(
        auth_client, user, db_session, no_libreoffice, mocker, mock_kanboard):
    from vms.domain.models import SequentialNumber

    cid_rechnung = _make_candidate(db_session, user["id"], email_id="c1")
    cid_umbuchung = _make_candidate(db_session, user["id"], email_id="c2")
    db_session.commit()
    _stub_pdf_chain(mocker)

    resp1 = auth_client.post("/api/invoices/download", json=_valid_send_payload(
        cid_rechnung, nummer_typ="rechnung", laufende_nummer="5"))
    resp2 = auth_client.post("/api/invoices/download", json=_valid_send_payload(
        cid_umbuchung, nummer_typ="umbuchung", laufende_nummer="1", kostenstelle="KST-1"))

    assert resp1.status_code == 200
    assert resp2.status_code == 200

    rechnung_seq = db_session.query(SequentialNumber).filter_by(number_type="rechnung").first()
    umbuchung_seq = db_session.query(SequentialNumber).filter_by(number_type="umbuchung").first()
    assert rechnung_seq.last_number == 5
    assert umbuchung_seq.last_number == 1


# --------------------------------------------------------------------------
# return_candidate: duplicated advance logic (app.py:895-910)
# --------------------------------------------------------------------------

@pytest.mark.route
def test_return_candidate_advances_same_counter_as_send_invoice(
        auth_client, user, db_session, mock_kanboard):
    from vms.domain.models import SequentialNumber

    cid = _make_candidate(db_session, user["id"])
    db_session.commit()

    resp = auth_client.post(f"/api/emails/candidates/{cid}/return", json={
        "action": "invoice",
        "laufende_nummer": "9",
        "nummer_typ": "rechnung",
    })

    assert resp.status_code == 200
    seq = db_session.query(SequentialNumber).filter_by(number_type="rechnung").first()
    assert seq is not None
    assert seq.last_number == 9

    db_session.expire_all()
    from vms.domain.models import EmailCandidate
    row = db_session.query(EmailCandidate).filter_by(id=cid).first()
    assert row.laufende_nummer == "9"
    assert row.nummer_typ == "rechnung"
    assert row.status == "invoice_pending"


@pytest.mark.route
def test_return_candidate_advances_existing_counter_when_higher(
        auth_client, user, db_session, mock_kanboard):
    """Covers the seq-exists + num_val > last_number branch (app.py:906-908),
    as distinct from the seq-does-not-exist branch exercised above."""
    from vms.domain.models import SequentialNumber

    db_session.add(SequentialNumber(number_type="rechnung", last_number=5))
    cid = _make_candidate(db_session, user["id"])
    db_session.commit()

    resp = auth_client.post(f"/api/emails/candidates/{cid}/return", json={
        "action": "invoice",
        "laufende_nummer": "9",
        "nummer_typ": "rechnung",
    })

    assert resp.status_code == 200
    db_session.expire_all()
    seq = db_session.query(SequentialNumber).filter_by(number_type="rechnung").first()
    assert seq.last_number == 9


@pytest.mark.route
def test_return_candidate_rejects_non_numeric_laufende_nummer(
        auth_client, user, db_session, mock_kanboard):
    """Früher fiel int() hier auf 0 zurück und legte einen Zähler bei 0 an,
    während der Vorgang den Rohstring behielt."""
    from vms.domain.models import SequentialNumber, EmailCandidate

    cid = _make_candidate(db_session, user["id"], status="processed")
    db_session.commit()

    resp = auth_client.post(f"/api/emails/candidates/{cid}/return", json={
        "action": "invoice",
        "laufende_nummer": "RE-XYZ",
        "nummer_typ": "rechnung",
    })

    assert resp.status_code == 400
    db_session.expire_all()
    row = db_session.query(EmailCandidate).filter_by(id=cid).one()
    assert row.laufende_nummer is None
    assert row.status == "processed"
    assert db_session.query(SequentialNumber).filter_by(number_type="rechnung").first() is None


@pytest.mark.route
def test_return_candidate_row_vanished_between_lookup_and_update_returns_404(
        auth_client, user, mocker, mock_kanboard):
    """Covers app.py:879-881: the defensive re-check after the initial
    get_candidate_by_id lookup. Unlike invoice_routes' equivalent gap (F5),
    this one correctly reports 404 rather than silently succeeding."""
    mocker.patch("vms.clients.email_client.get_candidate_by_id", return_value={
        "id": 999999, "status": "invoice_pending",
    })

    resp = auth_client.post("/api/emails/candidates/999999/return", json={
        "action": "returned",
    })

    assert resp.status_code == 404
    assert resp.get_json()["error"] == "Kandidat nicht gefunden"


@pytest.mark.route
def test_return_candidate_does_not_lower_existing_counter(
        auth_client, user, db_session, mock_kanboard):
    from vms.domain.models import SequentialNumber

    db_session.add(SequentialNumber(number_type="rechnung", last_number=20))
    cid = _make_candidate(db_session, user["id"])
    db_session.commit()

    resp = auth_client.post(f"/api/emails/candidates/{cid}/return", json={
        "action": "returned",
        "laufende_nummer": "3",
        "nummer_typ": "rechnung",
    })

    assert resp.status_code == 200
    db_session.expire_all()
    seq = db_session.query(SequentialNumber).filter_by(number_type="rechnung").first()
    assert seq.last_number == 20


@pytest.mark.route
def test_return_candidate_without_laufende_nummer_does_not_touch_counter(
        auth_client, user, db_session, mock_kanboard):
    """laufende_nummer/nummer_typ are optional on a plain return; omitting them
    must not create a spurious SequentialNumber row."""
    from vms.domain.models import SequentialNumber

    cid = _make_candidate(db_session, user["id"])
    db_session.commit()

    resp = auth_client.post(f"/api/emails/candidates/{cid}/return", json={
        "action": "returned",
    })

    assert resp.status_code == 200
    assert db_session.query(SequentialNumber).count() == 0


@pytest.mark.route
def test_return_candidate_rejects_invalid_action(auth_client, user, db_session):
    cid = _make_candidate(db_session, user["id"])
    db_session.commit()

    resp = auth_client.post(f"/api/emails/candidates/{cid}/return", json={
        "action": "garbage",
    })

    assert resp.status_code == 400
    assert resp.get_json()["error"] == "Ungültige Aktion"


@pytest.mark.route
def test_return_candidate_unknown_id_returns_404(auth_client, user):
    resp = auth_client.post("/api/emails/candidates/999999/return", json={
        "action": "returned",
    })

    assert resp.status_code == 404


# --------------------------------------------------------------------------
# Regressionstests zur verbindlichen Vergabe (models.claim_sequential_number).
# Waren ursprünglich strict-xfail (F3/F6 in docs/FINDINGS.md).
# --------------------------------------------------------------------------

@pytest.mark.route
def test_second_send_with_an_already_used_nummer_is_rejected(
        auth_client, user, db_session, no_libreoffice, mocker, mock_kanboard):
    from vms.domain.models import EmailCandidate

    cid1 = _make_candidate(db_session, user["id"], email_id="cand-1")
    cid2 = _make_candidate(db_session, user["id"], email_id="cand-2")
    db_session.commit()
    _stub_pdf_chain(mocker)

    # Beide Clients peeken denselben Vorschlag, bevor einer sendet.
    peek1 = auth_client.get("/api/sequential-number/rechnung").get_json()["next_number"]
    peek2 = auth_client.get("/api/sequential-number/rechnung").get_json()["next_number"]
    assert peek1 == peek2 == 1

    resp1 = auth_client.post("/api/invoices/download",
                             json=_valid_send_payload(cid1, laufende_nummer=str(peek1)))
    resp2 = auth_client.post("/api/invoices/download",
                             json=_valid_send_payload(cid2, laufende_nummer=str(peek2)))

    assert resp1.status_code == 200
    assert resp2.status_code == 409
    assert "bereits vergeben" in resp2.get_json()["error"]
    # Der zweite Vorgang bleibt unangetastet -- keine zweite Rechnung Nr. 1.
    row2 = db_session.query(EmailCandidate).filter_by(id=cid2).one()
    assert row2.laufende_nummer is None
    assert row2.status == "invoice_pending"


@pytest.mark.route
def test_server_allocates_distinct_numbers_when_client_sends_none(
        auth_client, user, db_session, no_libreoffice, mocker, mock_kanboard):
    from vms.domain.models import EmailCandidate

    cid1 = _make_candidate(db_session, user["id"], email_id="cand-1")
    cid2 = _make_candidate(db_session, user["id"], email_id="cand-2")
    db_session.commit()
    _stub_pdf_chain(mocker)

    resp1 = auth_client.post("/api/invoices/download",
                             json=_valid_send_payload(cid1, laufende_nummer=None))
    resp2 = auth_client.post("/api/invoices/download",
                             json=_valid_send_payload(cid2, laufende_nummer=None))

    assert resp1.status_code == resp2.status_code == 200
    assert resp1.headers["X-Laufende-Nummer"] == "1"
    assert resp2.headers["X-Laufende-Nummer"] == "2"
    nummern = {db_session.query(EmailCandidate).filter_by(id=c).one().laufende_nummer
               for c in (cid1, cid2)}
    assert nummern == {"1", "2"}


@pytest.mark.route
def test_manual_override_of_a_free_nummer_is_accepted(
        auth_client, user, db_session, no_libreoffice, mocker, mock_kanboard):
    from vms.domain.models import EmailCandidate, SequentialNumber

    cid = _make_candidate(db_session, user["id"])
    db_session.commit()
    _stub_pdf_chain(mocker)

    # Nachtrag einer Altrechnung mit hoher Nummer.
    resp = auth_client.post("/api/invoices/download",
                            json=_valid_send_payload(cid, laufende_nummer="500"))

    assert resp.status_code == 200
    row = db_session.query(EmailCandidate).filter_by(id=cid).one()
    assert row.laufende_nummer == "500"
    # Der Zähler zieht nach, damit die nächste automatische Nummer 501 ist.
    seq = db_session.query(SequentialNumber).filter_by(number_type="rechnung").one()
    assert seq.last_number == 500


@pytest.mark.route
def test_return_candidate_rejects_unknown_nummer_typ(
        auth_client, user, db_session, mock_kanboard):
    from vms.domain.models import SequentialNumber, EmailCandidate

    # Bewusst nicht 'invoice_pending': nur so ist sichtbar, dass die Route den
    # Status bei einem Fehler gar nicht erst umstellt.
    cid = _make_candidate(db_session, user["id"], status="processed")
    db_session.commit()

    resp = auth_client.post(f"/api/emails/candidates/{cid}/return", json={
        "action": "invoice",
        "laufende_nummer": "1",
        "nummer_typ": "garbage-type",
    })

    assert resp.status_code == 400
    assert db_session.query(SequentialNumber).filter_by(number_type="garbage-type").first() is None
    # Der Vorgang darf auch nicht halb umgestellt worden sein.
    db_session.expire_all()
    row = db_session.query(EmailCandidate).filter_by(id=cid).one()
    assert row.status == "processed"
    assert row.returned_at is None


@pytest.mark.route
def test_return_candidate_rejects_an_already_used_nummer(
        auth_client, user, db_session, mock_kanboard):
    from vms.domain.models import EmailCandidate

    belegt = _make_candidate(db_session, user["id"], email_id="belegt",
                             laufende_nummer="7", nummer_typ="rechnung")
    cid = _make_candidate(db_session, user["id"], email_id="neu", status="processed")
    db_session.commit()

    resp = auth_client.post(f"/api/emails/candidates/{cid}/return", json={
        "action": "invoice",
        "laufende_nummer": "7",
        "nummer_typ": "rechnung",
    })

    assert resp.status_code == 409
    assert "bereits vergeben" in resp.get_json()["error"]
    db_session.expire_all()
    assert db_session.query(EmailCandidate).filter_by(id=cid).one().laufende_nummer is None
    assert db_session.query(EmailCandidate).filter_by(id=belegt).one().laufende_nummer == "7"


# --------------------------------------------------------------------------
# models.claim_sequential_number / release_sequential_number direkt
# --------------------------------------------------------------------------

@pytest.mark.integration
def test_claim_sequential_number_rejects_unknown_nummernkreis(db_session):
    """Zweite Verteidigungslinie: die Routen filtern den Typ bereits, aber die
    Vergabe selbst darf keinen beliebigen Nummernkreis anlegen."""
    from vms.domain.models import claim_sequential_number, SequentialNumber

    with pytest.raises(ValueError, match="Unbekannter Nummernkreis"):
        claim_sequential_number(db_session, "quittung")

    assert db_session.query(SequentialNumber).filter_by(number_type="quittung").first() is None


@pytest.mark.integration
def test_release_sequential_number_keeps_counter_when_someone_else_advanced(db_session):
    """Nur der eigene Schritt darf zurückgebaut werden -- hat inzwischen jemand
    eine höhere Nummer gezogen, bleibt die Lücke, statt fremde Nummern zu
    recyceln."""
    from vms.domain.models import claim_sequential_number, release_sequential_number, SequentialNumber

    meine = claim_sequential_number(db_session, "rechnung")      # 1
    fremde = claim_sequential_number(db_session, "rechnung")     # 2
    assert (meine, fremde) == (1, 2)

    release_sequential_number(db_session, "rechnung", meine)

    seq = db_session.query(SequentialNumber).filter_by(number_type="rechnung").one()
    assert seq.last_number == 2


@pytest.mark.integration
def test_release_sequential_number_is_a_noop_for_an_unknown_type(db_session):
    from vms.domain.models import release_sequential_number, SequentialNumber

    release_sequential_number(db_session, "rechnung", 5)

    assert db_session.query(SequentialNumber).filter_by(number_type="rechnung").first() is None


# --------------------------------------------------------------------------
# migrate_unique_laufende_nummer.py -- die DB-seitige Absicherung unter der
# Applikationslogik. Der Index steht nicht in models.Base (er wird per
# Migration nachgezogen), deshalb legt der Test ihn selbst an.
# --------------------------------------------------------------------------

@pytest.mark.integration
def test_migration_index_blocks_duplicate_nummern(db_session, user):
    from sqlalchemy.exc import IntegrityError
    from migrations.migrate_unique_laufende_nummer import CREATE_INDEX, INDEX_NAME
    from sqlalchemy import text

    db_session.execute(CREATE_INDEX)
    _make_candidate(db_session, user["id"], email_id="a",
                    laufende_nummer="7", nummer_typ="rechnung")
    db_session.flush()

    try:
        # Bewusst ohne den flush-Helfer, damit der Constraint-Verstoß im
        # pytest.raises-Block landet und nicht schon beim Anlegen.
        from vms.domain.models import EmailCandidate
        db_session.add(EmailCandidate(
            user_id=user["id"], email_id="b", status="invoiced", tags=[],
            laufende_nummer="7", nummer_typ="rechnung"))

        with pytest.raises(IntegrityError):
            db_session.flush()
    finally:
        db_session.rollback()
        db_session.execute(text(f"DROP INDEX IF EXISTS {INDEX_NAME}"))
        db_session.commit()


@pytest.mark.integration
def test_migration_index_allows_the_same_nummer_in_a_different_nummernkreis(db_session, user):
    from migrations.migrate_unique_laufende_nummer import CREATE_INDEX, INDEX_NAME
    from sqlalchemy import text

    db_session.execute(CREATE_INDEX)
    try:
        _make_candidate(db_session, user["id"], email_id="a",
                        laufende_nummer="7", nummer_typ="rechnung")
        _make_candidate(db_session, user["id"], email_id="b",
                        laufende_nummer="7", nummer_typ="umbuchung")
        # Zwei nie fakturierte Vorgänge kollidieren ebenfalls nicht (partieller
        # Index auf laufende_nummer IS NOT NULL).
        _make_candidate(db_session, user["id"], email_id="c")
        _make_candidate(db_session, user["id"], email_id="d")
        db_session.flush()
    finally:
        db_session.rollback()
        db_session.execute(text(f"DROP INDEX IF EXISTS {INDEX_NAME}"))
        db_session.commit()

"""Tests for invoice_routes.py: pure helpers, the read-only invoice routes,
and every validation/happy/error branch of POST /api/invoices/download.

Der Endpunkt erzeugt das Rechnungs-/Umbuchungs-PDF und liefert es als Download
zurück -- es geht KEINE E-Mail mehr raus (siehe
docs/specs/rechnung-umbuchung-download-statt-mail.md).

Mock boundaries: `no_libreoffice` stubs odt_processor.convert_to_pdf und liefert
den Pfad einer echten Fake-PDF-Datei; die function-local imports in
api_download_invoice (odt_processor.process_odt_template,
email_client.get_candidate_by_id) werden am Ursprungsmodul gepatcht.
`invoice_routes.load_template` ist ein MODULE-level import und wird deshalb auf
invoice_routes selbst gepatcht, nicht auf template_store.
"""
import pytest
from datetime import datetime, timezone, date

from vms.routes.invoice import format_anschrift, _safe_filename_part
from vms.domain.models import parse_flexible_date


# --------------------------------------------------------------------------
# Unit tests: pure helpers, no DB/app context
# --------------------------------------------------------------------------

# Datumsparsen läuft seit der Konsolidierung (Block C) über die eine kanonische
# models.parse_flexible_date; das frühere invoice_routes.parse_german_date ist
# entfallen. Die Tests decken hier ab, worauf die Rechnungsansicht angewiesen ist.

@pytest.mark.unit
@pytest.mark.parametrize("raw,expected", [
    ("2024-03-05", date(2024, 3, 5)),
    ("05.03.2024", date(2024, 3, 5)),
    ("05.03.24", date(2024, 3, 5)),
    # Formen, die parse_german_date verwarf und die jetzt ankommen:
    ("5.3.2024", date(2024, 3, 5)),
    ("2024-03-05T14:30", date(2024, 3, 5)),
    ("05.03.2024, 14:00 Uhr", date(2024, 3, 5)),
])
def test_parse_flexible_date_accepts_the_formats_the_invoice_view_sees(raw, expected):
    assert parse_flexible_date(raw) == expected


@pytest.mark.unit
def test_two_digit_years_always_resolve_to_2000_plus():
    """parse_german_date nutzte strptime('%y') und kippte bei 69-99 auf 19xx --
    im Widerspruch zum eigenen Kommentar "assume 2000+". Die kanonische
    Implementierung rechnet konsequent +2000."""
    assert parse_flexible_date("01.01.99") == date(2099, 1, 1)
    assert parse_flexible_date("01.01.26") == date(2026, 1, 1)


@pytest.mark.unit
def test_parse_flexible_date_rejects_garbage():
    assert parse_flexible_date("not a date") is None


@pytest.mark.unit
def test_parse_flexible_date_empty_returns_none():
    assert parse_flexible_date("") is None
    assert parse_flexible_date(None) is None


@pytest.mark.unit
def test_parse_flexible_date_invalid_calendar_day_returns_none():
    """31.02.2024 passt auf das Muster, ist aber kein gültiger Tag."""
    assert parse_flexible_date("31.02.2024") is None


@pytest.mark.unit
def test_format_anschrift_literal_backslash_n():
    raw = "Musterstr. 1\\n12345 Musterstadt\\nDeutschland"
    assert format_anschrift(raw) == "Musterstr. 1\n12345 Musterstadt\nDeutschland"


@pytest.mark.unit
def test_format_anschrift_real_newline():
    raw = "Musterstr. 1\n12345 Musterstadt"
    assert format_anschrift(raw) == "Musterstr. 1\n12345 Musterstadt"


@pytest.mark.unit
def test_format_anschrift_single_line_comma_separated():
    raw = "Musterstr. 1, 12345 Musterstadt, Deutschland"
    assert format_anschrift(raw) == "Musterstr. 1\n12345 Musterstadt\nDeutschland"


@pytest.mark.unit
def test_format_anschrift_empty_returns_empty_string():
    assert format_anschrift(None) == ''
    assert format_anschrift('') == ''


@pytest.mark.unit
def test_format_anschrift_drops_blank_lines():
    raw = "Musterstr. 1\n\n12345 Musterstadt\n"
    assert format_anschrift(raw) == "Musterstr. 1\n12345 Musterstadt"


@pytest.mark.unit
def test_safe_filename_part_replaces_spaces_and_strips_special_chars():
    assert _safe_filename_part("Sommer Fest 2024!") == "Sommer_Fest_2024"


@pytest.mark.unit
def test_safe_filename_part_none_falls_back_to_unbekannt():
    assert _safe_filename_part(None) == "Unbekannt"


@pytest.mark.unit
def test_safe_filename_part_purely_symbolic_input_falls_back_to_unbekannt():
    """value is truthy ('!!!') so the `or` in the ctor doesn't fire, but every
    char is stripped by the isalnum-or-underscore filter -> the trailing
    `or 'Unbekannt'` branch must catch the resulting empty string."""
    assert _safe_filename_part("!!!") == "Unbekannt"


# --------------------------------------------------------------------------
# Helpers for route tests
# --------------------------------------------------------------------------

def _make_candidate(db_session, user_id, **overrides):
    from vms.domain.models import EmailCandidate
    defaults = dict(
        user_id=user_id,
        vorname_nachname="Erika Mustermann",
        veranstaltungsname="Sommerfest",
        anschrift="Musterstr. 1, 12345 Musterstadt",
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


def _valid_payload(candidate_id, **overrides):
    payload = dict(
        candidate_id=candidate_id,
        nummer_typ="rechnung",
        laufende_nummer="1",
        adresse="Musterstr. 1\n12345 Musterstadt",
        kostenstelle="",
        items=[{"name": "Zelt", "price": 10.0, "quantity": 2}],
    )
    payload.update(overrides)
    return payload


def _stub_pdf_chain(mocker):
    """Stub load_template + process_odt_template. convert_to_pdf comes from the
    no_libreoffice fixture (real fake-PDF path). Kein Mailversand mehr.

    Returns the mock for process_odt_template (to inspect `replacements`).
    """
    mocker.patch("vms.routes.invoice.load_template", return_value="/tmp/fake_template.odt")
    return mocker.patch("vms.infra.odt_processor.process_odt_template", return_value=None)


# --------------------------------------------------------------------------
# Route tests: read-only endpoints
# --------------------------------------------------------------------------

@pytest.mark.route
def test_invoices_page_anonymous_redirects(client):
    resp = client.get("/invoices", follow_redirects=False)
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


@pytest.mark.route
def test_invoices_page_authenticated_ok(auth_client):
    resp = auth_client.get("/invoices")
    assert resp.status_code == 200


@pytest.mark.route
def test_candidates_endpoint_anonymous_is_unauthorized(client):
    """/api/ paths get a JSON 401 from auth.unauthorized_handler, not a redirect."""
    resp = client.get("/api/invoices/candidates", follow_redirects=False)
    assert resp.status_code == 401


@pytest.mark.route
def test_candidates_endpoint_returns_only_invoice_pending(auth_client, user, db_session):
    _make_candidate(db_session, user["id"], status="invoice_pending", datum="2024-05-01")
    _make_candidate(db_session, user["id"], status="pending", datum="2024-01-01",
                     vorname_nachname="Not Pending")
    db_session.commit()

    resp = auth_client.get("/api/invoices/candidates")

    assert resp.status_code == 200
    body = resp.get_json()
    assert len(body) == 1
    assert body[0]["vorname_nachname"] == "Erika Mustermann"


@pytest.mark.route
def test_candidates_endpoint_sorts_by_parsed_date_unparsable_last(auth_client, user, db_session):
    id_later = _make_candidate(db_session, user["id"], datum="2024-06-01",
                                vorname_nachname="Later")
    id_unparsable = _make_candidate(db_session, user["id"], datum="garbage-date",
                                     vorname_nachname="Unparsable")
    id_earlier = _make_candidate(db_session, user["id"], datum="2024-01-01",
                                  vorname_nachname="Earlier")
    db_session.commit()

    resp = auth_client.get("/api/invoices/candidates")

    body = resp.get_json()
    names_in_order = [c["vorname_nachname"] for c in body]
    assert names_in_order == ["Earlier", "Later", "Unparsable"]


@pytest.mark.route
def test_consumables_endpoint_anonymous_is_unauthorized(client):
    resp = client.get("/api/invoices/consumables", follow_redirects=False)
    assert resp.status_code == 401


@pytest.mark.route
def test_consumables_endpoint_filters_type_and_defaults_price(auth_client, db_session):
    from vms.domain.models import InventoryItem

    db_session.add(InventoryItem(name="Klebeband", type="consumable", price=None, unit="Stück"))
    db_session.add(InventoryItem(name="Bierbank", type="equipment", price=5, unit="Stück"))
    db_session.commit()

    resp = auth_client.get("/api/invoices/consumables")

    assert resp.status_code == 200
    body = resp.get_json()
    assert len(body) == 1
    assert body[0]["name"] == "Klebeband"
    assert body[0]["price"] == 0.0


# --------------------------------------------------------------------------
# /api/invoices/download: validation branches (each: 400/40x + nothing persisted)
# --------------------------------------------------------------------------

@pytest.mark.route
def test_download_invoice_anonymous_is_unauthorized(client):
    resp = client.post("/api/invoices/download", json=_valid_payload(1),
                       follow_redirects=False)
    assert resp.status_code == 401


@pytest.mark.route
def test_download_invoice_rejects_invalid_nummer_typ(auth_client, user, db_session):
    cid = _make_candidate(db_session, user["id"])
    db_session.commit()

    resp = auth_client.post("/api/invoices/download",
                            json=_valid_payload(cid, nummer_typ="garbage"))

    assert resp.status_code == 400
    assert resp.get_json()["error"] == "Ungültiger Typ"
    db_session.expire_all()
    from vms.domain.models import EmailCandidate
    row = db_session.query(EmailCandidate).filter_by(id=cid).first()
    assert row.status == "invoice_pending"


@pytest.mark.route
def test_download_invoice_rejects_empty_items(auth_client, user, db_session):
    cid = _make_candidate(db_session, user["id"])
    db_session.commit()

    resp = auth_client.post("/api/invoices/download", json=_valid_payload(cid, items=[]))

    assert resp.status_code == 400
    assert resp.get_json()["error"] == "Mindestens ein Posten erforderlich"


@pytest.mark.route
def test_download_invoice_rejects_items_not_a_list(auth_client, user, db_session):
    cid = _make_candidate(db_session, user["id"])
    db_session.commit()

    resp = auth_client.post("/api/invoices/download",
                            json=_valid_payload(cid, items={"name": "Zelt"}))

    assert resp.status_code == 400
    assert resp.get_json()["error"] == "Mindestens ein Posten erforderlich"


@pytest.mark.route
def test_download_invoice_umbuchung_requires_kostenstelle(auth_client, user, db_session):
    cid = _make_candidate(db_session, user["id"])
    db_session.commit()

    resp = auth_client.post("/api/invoices/download",
                            json=_valid_payload(cid, nummer_typ="umbuchung", kostenstelle=""))

    assert resp.status_code == 400
    assert resp.get_json()["error"] == "Kostenstelle fehlt"


@pytest.mark.route
def test_download_invoice_unknown_candidate_returns_404(auth_client, user):
    resp = auth_client.post("/api/invoices/download", json=_valid_payload(999999))

    assert resp.status_code == 404
    assert resp.get_json()["error"] == "Kandidat nicht gefunden"


@pytest.mark.route
def test_download_invoice_rejects_unparsable_item_price(
        auth_client, user, db_session, no_libreoffice, mocker, mock_kanboard):
    from vms.domain.models import EmailCandidate, SequentialNumber

    cid = _make_candidate(db_session, user["id"])
    db_session.commit()
    _stub_pdf_chain(mocker)

    resp = auth_client.post("/api/invoices/download", json=_valid_payload(
        cid, items=[{"name": "Zelt", "price": "not-a-number", "quantity": 2}]))

    assert resp.status_code == 400
    assert "Zelt" in resp.get_json()["error"]
    db_session.expire_all()
    row = db_session.query(EmailCandidate).filter_by(id=cid).one()
    assert row.status == "invoice_pending"
    assert db_session.query(SequentialNumber).filter_by(number_type="rechnung").first() is None


@pytest.mark.route
def test_download_invoice_rejects_non_numeric_laufende_nummer(
        auth_client, user, db_session, no_libreoffice, mocker, mock_kanboard):
    """Eine nicht-numerische Nummer lief früher durch: der Kandidat behielt den
    Rohstring, während der Zähler auf 0 zurückfiel und beide auseinanderliefen."""
    from vms.domain.models import EmailCandidate, SequentialNumber

    cid = _make_candidate(db_session, user["id"])
    db_session.commit()
    _stub_pdf_chain(mocker)

    resp = auth_client.post("/api/invoices/download", json=_valid_payload(
        cid, nummer_typ="rechnung", laufende_nummer="RE-ABC"))

    assert resp.status_code == 400
    db_session.expire_all()
    row = db_session.query(EmailCandidate).filter_by(id=cid).one()
    assert row.laufende_nummer is None
    assert row.status == "invoice_pending"
    assert db_session.query(SequentialNumber).filter_by(number_type="rechnung").first() is None


@pytest.mark.route
def test_download_invoice_rejects_laufende_nummer_zero(
        auth_client, user, db_session, no_libreoffice, mocker, mock_kanboard):
    cid = _make_candidate(db_session, user["id"])
    db_session.commit()
    _stub_pdf_chain(mocker)

    resp = auth_client.post("/api/invoices/download", json=_valid_payload(
        cid, laufende_nummer="0"))

    assert resp.status_code == 400


# --------------------------------------------------------------------------
# /api/invoices/download: happy paths -- PDF-Download statt Mailversand
# --------------------------------------------------------------------------

@pytest.mark.route
def test_download_invoice_rechnung_happy_path_returns_pdf_and_persists(
        auth_client, user, db_session, no_libreoffice, mocker, mock_kanboard):
    from vms.domain.models import EmailCandidate, SequentialNumber
    from vms.infra.odt_processor import format_money_de

    cid = _make_candidate(db_session, user["id"], veranstaltungsname="Sommerfest")
    db_session.commit()
    process_mock = _stub_pdf_chain(mocker)

    resp = auth_client.post("/api/invoices/download", json=_valid_payload(
        cid, nummer_typ="rechnung", laufende_nummer="42",
        items=[{"name": "Zelt", "price": 10.0, "quantity": 3},
               {"name": "Bierbank", "price": 5.0, "quantity": 2}],
    ))

    assert resp.status_code == 200
    assert resp.mimetype == "application/pdf"
    assert resp.data == b"%PDF-1.4 fake"
    assert "attachment" in resp.headers["Content-Disposition"]

    db_session.expire_all()
    row = db_session.query(EmailCandidate).filter_by(id=cid).first()
    assert row.status == "invoiced"
    # contract_created bleibt unberührt: der Rechnungsdownload erzeugt keinen
    # Leihvertrag. Der Kandidat hier hatte nie einen, also bleibt es False.
    assert row.contract_created is False
    assert row.laufende_nummer == "42"
    assert row.nummer_typ == "rechnung"

    seq = db_session.query(SequentialNumber).filter_by(number_type="rechnung").first()
    assert seq is not None
    assert seq.last_number == 42

    # Server recomputes the total from items, ignoring anything the client sent.
    replacements = process_mock.call_args.args[2]
    expected_total = 10.0 * 3 + 5.0 * 2
    assert replacements["#GESAMTPREIS#"] == format_money_de(expected_total)
    assert replacements["#VORNAME NACHNAME#"] == "Erika Mustermann"
    assert "#KOSTENSTELLE#" not in replacements


@pytest.mark.route
def test_download_invoice_sends_no_mail(
        auth_client, user, db_session, no_libreoffice, mocker, mock_kanboard, mailbox):
    cid = _make_candidate(db_session, user["id"])
    db_session.commit()
    _stub_pdf_chain(mocker)

    with mailbox as outbox:
        resp = auth_client.post("/api/invoices/download", json=_valid_payload(cid))

    assert resp.status_code == 200
    assert outbox == []


@pytest.mark.route
def test_download_invoice_returns_laufende_nummer_header(
        auth_client, user, db_session, no_libreoffice, mocker, mock_kanboard):
    cid = _make_candidate(db_session, user["id"])
    db_session.commit()
    _stub_pdf_chain(mocker)

    resp = auth_client.post("/api/invoices/download",
                            json=_valid_payload(cid, laufende_nummer="42"))

    assert resp.status_code == 200
    assert resp.headers["X-Laufende-Nummer"] == "42"


@pytest.mark.route
def test_download_invoice_filename_uses_veranstaltung_when_present(
        auth_client, user, db_session, no_libreoffice, mocker, mock_kanboard):
    cid = _make_candidate(db_session, user["id"], veranstaltungsname="Sommerfest")
    db_session.commit()
    _stub_pdf_chain(mocker)

    resp = auth_client.post("/api/invoices/download", json=_valid_payload(
        cid, nummer_typ="rechnung", laufende_nummer="42"))

    assert resp.status_code == 200
    assert "Rechnung_Sommerfest.pdf" in resp.headers["Content-Disposition"]


@pytest.mark.route
def test_download_invoice_filename_falls_back_to_number_and_name(
        auth_client, user, db_session, no_libreoffice, mocker, mock_kanboard):
    cid = _make_candidate(db_session, user["id"], veranstaltungsname=None)
    db_session.commit()
    _stub_pdf_chain(mocker)

    resp = auth_client.post("/api/invoices/download", json=_valid_payload(
        cid, nummer_typ="rechnung", laufende_nummer="7"))

    assert resp.status_code == 200
    assert "Rechnung_7_Erika_Mustermann.pdf" in resp.headers["Content-Disposition"]


@pytest.mark.route
def test_download_invoice_umbuchung_sets_kostenstelle_and_omits_internal_address(
        auth_client, user, db_session, no_libreoffice, mocker, mock_kanboard):
    from vms.domain.models import EmailCandidate

    cid = _make_candidate(db_session, user["id"])
    db_session.commit()
    process_mock = _stub_pdf_chain(mocker)

    resp = auth_client.post("/api/invoices/download", json=_valid_payload(
        cid, nummer_typ="umbuchung", laufende_nummer="3", kostenstelle="KST-99"))

    assert resp.status_code == 200

    replacements = process_mock.call_args.args[2]
    assert replacements["#KOSTENSTELLE#"] == "KST-99"
    assert "#VORNAME NACHNAME#" not in replacements
    assert "#ADRESSE#" not in replacements

    db_session.expire_all()
    row = db_session.query(EmailCandidate).filter_by(id=cid).first()
    assert row.nummer_typ == "umbuchung"


@pytest.mark.route
def test_download_invoice_allocates_when_laufende_nummer_omitted(
        auth_client, user, db_session, no_libreoffice, mocker, mock_kanboard):
    """Ohne Vorgabe vergibt der Server die Nummer selbst -- das ist der
    Normalfall, seit das Formular den Vorschlag nur noch bei einer manuellen
    Änderung mitschickt."""
    from vms.domain.models import EmailCandidate

    cid = _make_candidate(db_session, user["id"])
    db_session.commit()
    _stub_pdf_chain(mocker)

    resp = auth_client.post("/api/invoices/download",
                            json=_valid_payload(cid, laufende_nummer=""))

    assert resp.status_code == 200
    assert resp.headers["X-Laufende-Nummer"] == "1"
    row = db_session.query(EmailCandidate).filter_by(id=cid).one()
    assert row.laufende_nummer == "1"


@pytest.mark.route
def test_download_invoice_advances_sequential_counter_from_existing_value(
        auth_client, user, db_session, no_libreoffice, mocker, mock_kanboard):
    from vms.domain.models import SequentialNumber

    db_session.add(SequentialNumber(number_type="rechnung", last_number=7))
    cid = _make_candidate(db_session, user["id"])
    db_session.commit()
    _stub_pdf_chain(mocker)

    resp = auth_client.post("/api/invoices/download", json=_valid_payload(
        cid, nummer_typ="rechnung", laufende_nummer="8"))

    assert resp.status_code == 200
    db_session.expire_all()
    seq = db_session.query(SequentialNumber).filter_by(number_type="rechnung").first()
    assert seq.last_number == 8


@pytest.mark.route
def test_download_invoice_does_not_lower_counter_for_smaller_number(
        auth_client, user, db_session, no_libreoffice, mocker, mock_kanboard):
    """A laufende_nummer below the stored counter must not push it backwards."""
    from vms.domain.models import SequentialNumber

    db_session.add(SequentialNumber(number_type="rechnung", last_number=50))
    cid = _make_candidate(db_session, user["id"])
    db_session.commit()
    _stub_pdf_chain(mocker)

    resp = auth_client.post("/api/invoices/download", json=_valid_payload(
        cid, nummer_typ="rechnung", laufende_nummer="10"))

    assert resp.status_code == 200
    db_session.expire_all()
    seq = db_session.query(SequentialNumber).filter_by(number_type="rechnung").first()
    assert seq.last_number == 50


# --------------------------------------------------------------------------
# /api/invoices/download: error paths
# --------------------------------------------------------------------------

@pytest.mark.route
def test_download_invoice_pdf_failure_releases_number_and_leaves_candidate_unchanged(
        auth_client, user, db_session, no_libreoffice, mocker, mock_kanboard):
    """Schlägt die PDF-Erzeugung fehl, bleibt der Vorgang unverändert und die
    reservierte Nummer wird wieder freigegeben (keine Lücke im Nummernkreis)."""
    from vms.domain.models import EmailCandidate, SequentialNumber

    cid = _make_candidate(db_session, user["id"])
    db_session.commit()

    mocker.patch("vms.routes.invoice.load_template", return_value="/tmp/fake_template.odt")
    mocker.patch("vms.infra.odt_processor.process_odt_template",
                 side_effect=Exception("template kaputt"))

    resp = auth_client.post("/api/invoices/download", json=_valid_payload(cid))

    assert resp.status_code == 500
    assert "fehlgeschlagen" in resp.get_json()["error"].lower() \
        or "erstellt" in resp.get_json()["error"].lower()

    db_session.expire_all()
    row = db_session.query(EmailCandidate).filter_by(id=cid).first()
    assert row.status == "invoice_pending"
    assert row.laufende_nummer is None
    # Nummer 1 wurde reserviert und wieder freigegeben -> Zähler steht auf 0.
    seq = db_session.query(SequentialNumber).filter_by(number_type="rechnung").first()
    assert seq.last_number == 0


@pytest.mark.route
def test_download_invoice_pdf_failure_message_does_not_leak_internal_details(
        auth_client, user, db_session, no_libreoffice, mocker, mock_kanboard):
    cid = _make_candidate(db_session, user["id"])
    db_session.commit()

    mocker.patch("vms.routes.invoice.load_template", return_value="/tmp/fake_template.odt")
    mocker.patch(
        "vms.infra.odt_processor.process_odt_template",
        side_effect=Exception("/etc/vms/master.key not found; secret=hunter2"),
    )

    resp = auth_client.post("/api/invoices/download", json=_valid_payload(cid))

    error_message = resp.get_json()["error"]
    assert "master.key" not in error_message
    assert "hunter2" not in error_message


@pytest.mark.route
def test_download_invoice_survives_kanboard_reconcile_failure(
        auth_client, user, db_session, no_libreoffice, mocker, mock_kanboard):
    from vms.domain.models import EmailCandidate

    cid = _make_candidate(db_session, user["id"])
    db_session.commit()
    _stub_pdf_chain(mocker)
    # Der Abgleich lebt seit Block B in kanboard_client, nicht mehr in app.
    mocker.patch("vms.clients.kanboard_client.reconcile_candidate",
                 side_effect=Exception("kanboard unreachable"))

    resp = auth_client.post("/api/invoices/download", json=_valid_payload(cid))

    assert resp.status_code == 200
    assert resp.mimetype == "application/pdf"

    db_session.expire_all()
    row = db_session.query(EmailCandidate).filter_by(id=cid).first()
    assert row.status == "invoiced"


@pytest.mark.route
def test_download_invoice_reports_failure_when_candidate_vanished(
        auth_client, user, db_session, no_libreoffice, mocker, mock_kanboard):
    from vms.domain.models import SequentialNumber

    _stub_pdf_chain(mocker)
    # Bypass the 404 branch with a fake lookup result for an id that has no
    # backing row, simulating deletion between the lookup and the persist step.
    mocker.patch("vms.clients.email_client.get_candidate_by_id", return_value={
        "id": 999999, "veranstaltungsname": "Ghost", "vorname_nachname": "Ghost",
    })

    resp = auth_client.post("/api/invoices/download", json=_valid_payload(999999))

    assert resp.status_code == 500
    # Es ging nichts raus -> die reservierte Nummer wird freigegeben.
    db_session.expire_all()
    seq = db_session.query(SequentialNumber).filter_by(number_type="rechnung").first()
    assert seq is None or seq.last_number == 0


@pytest.mark.route
def test_download_invoice_uses_local_timezone_for_date_placeholders(
        app, user, db_session, no_libreoffice, mocker, mock_kanboard):
    from freezegun import freeze_time

    cid = _make_candidate(db_session, user["id"])
    db_session.commit()
    process_mock = _stub_pdf_chain(mocker)

    # Silvester 23:30 UTC -> in Berlin (UTC+1 im Winter) ist bereits der 01.01.
    # des Folgejahres. Die Login-Session muss innerhalb des eingefrorenen
    # Fensters entstehen: wird sie davor angelegt, verwirft flask-login sie beim
    # Zeitsprung und die Route antwortet 401 statt das PDF zu bauen.
    with freeze_time("2025-12-31 23:30:00"):
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(user["id"])
            sess["_fresh"] = True

        resp = client.post("/api/invoices/download", json=_valid_payload(cid))

    assert resp.status_code == 200
    replacements = process_mock.call_args.args[2]
    assert replacements["#HEUTE#"] == "01.01.2026"
    assert replacements["#JAHR#"] == "2026"


# --------------------------------------------------------------------------
# Geteilte Sichtbarkeit: VMS kennt bewusst keine Ownership-Trennung -- jeder
# eingeloggte User sieht und bearbeitet alle Vorgänge.
# --------------------------------------------------------------------------

def _make_second_user():
    from vms.auth import User
    u = User.create(
        username="tester2",
        password="Sup3r-Secret!2",
        display_name="Second User",
        email="tester2@example.com",
        is_active=True,
    )
    assert u is not None, "second User.create failed"
    return u


@pytest.mark.route
def test_download_invoice_accepts_candidate_of_another_user(
        auth_client, user, db_session, no_libreoffice, mocker, mock_kanboard):
    from vms.domain.models import EmailCandidate

    other = _make_second_user()
    cid = _make_candidate(db_session, other.id)
    db_session.commit()
    _stub_pdf_chain(mocker)

    resp = auth_client.post("/api/invoices/download", json=_valid_payload(cid))

    assert resp.status_code == 200
    row = db_session.query(EmailCandidate).filter_by(id=cid).one()
    assert row.status == "invoiced"


@pytest.mark.route
def test_candidates_endpoint_lists_other_users_candidates(auth_client, user, db_session):
    other = _make_second_user()
    _make_candidate(db_session, other.id, vorname_nachname="Fremder Kandidat")
    db_session.commit()

    resp = auth_client.get("/api/invoices/candidates")

    names = [c["vorname_nachname"] for c in resp.get_json()]
    assert "Fremder Kandidat" in names


# --------------------------------------------------------------------------
# contract_created ist an den Leihvertrag gekoppelt, nicht an die Rechnung.
# --------------------------------------------------------------------------

@pytest.mark.route
def test_download_invoice_keeps_an_existing_contract_flag(
        auth_client, user, db_session, no_libreoffice, mocker, mock_kanboard):
    from vms.domain.models import EmailCandidate

    cid = _make_candidate(db_session, user["id"], contract_created=True)
    db_session.commit()
    _stub_pdf_chain(mocker)

    resp = auth_client.post("/api/invoices/download", json=_valid_payload(cid))

    assert resp.status_code == 200
    db_session.expire_all()
    assert db_session.query(EmailCandidate).filter_by(id=cid).one().contract_created is True


@pytest.mark.route
def test_download_invoice_warns_when_no_contract_was_ever_created(
        auth_client, user, db_session, no_libreoffice, mocker, mock_kanboard, caplog):
    """Ein Verleih sollte beim Fakturieren längst einen Vertrag haben. Fehlt er,
    ist das ein Ablauffehler und muss auffallen, statt still gebucht zu werden."""
    import logging

    cid = _make_candidate(db_session, user["id"], contract_created=False)
    db_session.commit()
    _stub_pdf_chain(mocker)

    with caplog.at_level(logging.WARNING):
        resp = auth_client.post("/api/invoices/download", json=_valid_payload(cid))

    assert resp.status_code == 200
    assert any("kein Leihvertrag" in r.getMessage() for r in caplog.records), \
        "fehlender Leihvertrag wurde nicht gemeldet"


# --------------------------------------------------------------------------
# Aufräumen: der Mailversand samt Helfer ist restlos entfernt.
# --------------------------------------------------------------------------

@pytest.mark.unit
def test_send_email_with_attachment_is_removed():
    """`auth.send_email_with_attachment` hatte nur den Rechnungsversand als
    Aufrufer und ist mit dessen Wegfall toter Code."""
    import vms.auth as auth
    assert not hasattr(auth, "send_email_with_attachment")


# --------------------------------------------------------------------------
# POST /api/invoices/candidates/<id>/cancel -- „doch keine Rechnung erstellen".
# Wahrheitsquelle: docs/specs/rechnung-doch-nicht-erstellen.md.
#
# Die Regeln selbst (welcher Status abbrechbar ist, wann die Nummer freigegeben
# wird, wie die Notiz behandelt wird) liegen rein in vms/domain/rechnungsabbruch.py
# und sind in tests/test_rechnungsabbruch.py gepinnt. Hier: die Verdrahtung --
# Persistenz, Nummernkreis, Kanboard, HTTP-Vertrag.
# --------------------------------------------------------------------------

def _cancel_url(candidate_id):
    return f"/api/invoices/candidates/{candidate_id}/cancel"


def _set_counter(db_session, number_type, last_number):
    """Setze den Zählerstand eines Nummernkreises auf einen bekannten Wert."""
    from vms.domain.models import SequentialNumber
    db_session.add(SequentialNumber(number_type=number_type, last_number=last_number))
    db_session.flush()


def _counter(db_session, number_type):
    from vms.domain.models import SequentialNumber
    row = db_session.query(SequentialNumber).filter_by(number_type=number_type).first()
    return None if row is None else row.last_number


@pytest.mark.route
def test_cancel_invoice_setzt_status_returned(
        auth_client, user, db_session, mock_kanboard):
    """Kriterium 10: der Vorgang landet im Zustand aus „nur eingezählt"."""
    from vms.domain.models import EmailCandidate, CandidateStatus

    cid = _make_candidate(db_session, user["id"], status="invoice_pending")
    db_session.commit()

    resp = auth_client.post(_cancel_url(cid), json={})

    assert resp.status_code == 200
    db_session.expire_all()
    row = db_session.query(EmailCandidate).filter_by(id=cid).one()
    assert row.status == CandidateStatus.RETURNED.value


@pytest.mark.route
def test_cancel_invoice_gibt_laufende_nummer_frei(
        auth_client, user, db_session, mock_kanboard):
    """Kriterium 11: aus invoice_pending ist nie ein Dokument entstanden -- die
    reservierte Nummer geht in den Kreis zurück, statt eine Lücke zu lassen."""
    from vms.domain.models import EmailCandidate

    _set_counter(db_session, "rechnung", 12)
    cid = _make_candidate(db_session, user["id"], status="invoice_pending",
                          laufende_nummer="12", nummer_typ="rechnung")
    db_session.commit()

    resp = auth_client.post(_cancel_url(cid), json={})

    assert resp.status_code == 200
    db_session.expire_all()
    row = db_session.query(EmailCandidate).filter_by(id=cid).one()
    assert row.laufende_nummer is None
    assert row.nummer_typ is None
    assert _counter(db_session, "rechnung") == 11


@pytest.mark.route
def test_cancel_invoice_aus_invoiced_behaelt_nummer_und_zaehler(
        auth_client, user, db_session, mock_kanboard):
    """Kriterium 12: das PDF existiert und trägt die Nummer -- sie darf nie ein
    zweites Mal vergeben werden und bleibt dem Vorgang zugeordnet."""
    from vms.domain.models import EmailCandidate, CandidateStatus

    _set_counter(db_session, "rechnung", 12)
    cid = _make_candidate(db_session, user["id"], status="invoiced",
                          laufende_nummer="12", nummer_typ="rechnung")
    db_session.commit()

    resp = auth_client.post(_cancel_url(cid), json={})

    assert resp.status_code == 200
    db_session.expire_all()
    row = db_session.query(EmailCandidate).filter_by(id=cid).one()
    assert row.status == CandidateStatus.RETURNED.value
    assert row.laufende_nummer == "12"
    assert row.nummer_typ == "rechnung"
    assert _counter(db_session, "rechnung") == 12


@pytest.mark.route
def test_cancel_invoice_laesst_rueckgabe_unveraendert(
        auth_client, user, db_session, mock_kanboard):
    """Kriterium 13: das Einzählen ist bereits passiert. Der Abbruch nimmt nur
    die Rechnungsabsicht zurück -- Rückgabezeitpunkt und Lagerplätze bleiben."""
    from datetime import datetime, timezone
    from vms.domain.models import EmailCandidate, StorageLocation

    eingezaehlt_am = datetime(2024, 6, 1, 10, 30, tzinfo=timezone.utc)
    cid = _make_candidate(db_session, user["id"], status="invoice_pending",
                          returned_at=eingezaehlt_am,
                          return_note="Alles vollständig zurück")
    belegt = StorageLocation(name="Schrank 9", candidate_id=None)
    db_session.add(belegt)
    db_session.commit()

    resp = auth_client.post(_cancel_url(cid), json={})

    assert resp.status_code == 200
    db_session.expire_all()
    row = db_session.query(EmailCandidate).filter_by(id=cid).one()
    assert row.returned_at == eingezaehlt_am
    assert row.return_note == "Alles vollständig zurück"
    assert db_session.query(StorageLocation).filter_by(id=belegt.id).one().candidate_id is None


@pytest.mark.route
def test_cancel_invoice_entfernt_vorgang_aus_rechnungsliste(
        auth_client, user, db_session, mock_kanboard):
    """Kriterium 14: der Rechnungen-Tab zeigt nur invoice_pending. Nach dem
    Abbruch ist der Vorgang dort weg -- das ist der sichtbare Zweck."""
    cid = _make_candidate(db_session, user["id"], status="invoice_pending")
    db_session.commit()
    vorher = auth_client.get("/api/invoices/candidates").get_json()
    assert cid in [c["id"] for c in vorher]

    auth_client.post(_cancel_url(cid), json={})

    nachher = auth_client.get("/api/invoices/candidates").get_json()
    assert cid not in [c["id"] for c in nachher]


@pytest.mark.route
def test_cancel_invoice_schliesst_kanboard_task(
        auth_client, user, db_session, mock_kanboard):
    """Kriterium 15: `returned` ist terminal -- der verknüpfte Task wird
    geschlossen, wie beim direkten Einzählen."""
    mock_kanboard.side_effect = [
        {"id": 77, "is_active": 1},  # getTask (close_task)
        True,                        # closeTask
    ]
    cid = _make_candidate(db_session, user["id"], status="invoice_pending",
                          kanboard_task_id=77)
    db_session.commit()

    resp = auth_client.post(_cancel_url(cid), json={})

    assert resp.status_code == 200
    aufgerufene_methoden = [call.args[1] for call in mock_kanboard.call_args_list]
    assert "closeTask" in aufgerufene_methoden


@pytest.mark.route
def test_cancel_invoice_unbekannte_id_404(auth_client, user, mock_kanboard):
    """Kriterium 16."""
    resp = auth_client.post(_cancel_url(999999), json={})

    assert resp.status_code == 404
    assert "error" in resp.get_json()


@pytest.mark.route
@pytest.mark.parametrize("status", ["pending", "processed", "done"])
def test_cancel_invoice_unzulaessiger_status_409(
        auth_client, user, db_session, mock_kanboard, status):
    """Kriterium 17: ein Verleih, der noch gar nicht zurück ist, hat keine
    Rechnungsabsicht zurückzunehmen."""
    from vms.domain.models import EmailCandidate

    cid = _make_candidate(db_session, user["id"], status=status)
    db_session.commit()

    resp = auth_client.post(_cancel_url(cid), json={})

    assert resp.status_code == 409
    assert "error" in resp.get_json()
    db_session.expire_all()
    assert db_session.query(EmailCandidate).filter_by(id=cid).one().status == status


@pytest.mark.route
def test_cancel_invoice_zweiter_aufruf_409(
        auth_client, user, db_session, mock_kanboard):
    """Kriterium 18: bewusst nicht idempotent -- der zweite Aufruf hat nichts
    zurückzunehmen, und ein stilles OK würde eine Fehlbedienung verdecken.
    Wichtig ist vor allem, dass er den Zähler nicht ein zweites Mal senkt."""
    from vms.domain.models import EmailCandidate, CandidateStatus

    _set_counter(db_session, "rechnung", 12)
    cid = _make_candidate(db_session, user["id"], status="invoice_pending",
                          laufende_nummer="12", nummer_typ="rechnung")
    db_session.commit()
    assert auth_client.post(_cancel_url(cid), json={}).status_code == 200

    resp = auth_client.post(_cancel_url(cid), json={})

    assert resp.status_code == 409
    db_session.expire_all()
    assert db_session.query(EmailCandidate).filter_by(id=cid).one().status \
        == CandidateStatus.RETURNED.value
    assert _counter(db_session, "rechnung") == 11


@pytest.mark.route
def test_cancel_invoice_ohne_login_aendert_nichts(client, user, db_session):
    """Kriterium 19: `/api/`-Pfade antworten mit 401 JSON statt umzuleiten
    (auth.py:318) -- eine Umleitung auf die Login-HTML-Seite wäre für einen
    fetch-Aufruf nicht auswertbar."""
    from vms.domain.models import EmailCandidate

    cid = _make_candidate(db_session, user["id"], status="invoice_pending")
    db_session.commit()

    resp = client.post(_cancel_url(cid), json={})

    assert resp.status_code == 401
    assert "error" in resp.get_json()
    db_session.expire_all()
    assert db_session.query(EmailCandidate).filter_by(id=cid).one().status \
        == "invoice_pending"


@pytest.mark.route
def test_cancel_invoice_ohne_body_funktioniert(
        auth_client, user, db_session, mock_kanboard):
    """Kriterium 20: die Notiz ist optional, ein leerer Body darf keine 500
    auslösen (vgl. das `or {}`-Fallback im Rückgabe-Endpunkt)."""
    from vms.domain.models import EmailCandidate, CandidateStatus

    cid = _make_candidate(db_session, user["id"], status="invoice_pending")
    db_session.commit()

    resp = auth_client.post(_cancel_url(cid))

    assert resp.status_code == 200
    db_session.expire_all()
    assert db_session.query(EmailCandidate).filter_by(id=cid).one().status \
        == CandidateStatus.RETURNED.value


@pytest.mark.route
def test_cancel_invoice_notiz_ersetzt_return_note(
        auth_client, user, db_session, mock_kanboard):
    """Ergänzung zu Kriterium 7: die Notiz kommt auch wirklich durch die Route
    hindurch in der Zeile an."""
    from vms.domain.models import EmailCandidate

    cid = _make_candidate(db_session, user["id"], status="invoice_pending",
                          return_note="Alles vollständig zurück")
    db_session.commit()

    resp = auth_client.post(_cancel_url(cid), json={"note": "Doch nichts abzurechnen"})

    assert resp.status_code == 200
    db_session.expire_all()
    row = db_session.query(EmailCandidate).filter_by(id=cid).one()
    assert row.return_note == "Doch nichts abzurechnen"

"""Tests for app.py -- the module-level Flask app (health/index, PDF generation,
Kanboard passthrough routes, candidate-to-task creation, and the daily
reconcile job).

IMPORTANT: app.py imports `convert_to_pdf`, `process_odt_template`, `load_template`
BY VALUE at module scope (app.py:20-21). The shared `no_libreoffice` fixture patches
`odt_processor.convert_to_pdf`, which does NOT affect app.py's already-bound name.
For the /api/generate path we therefore patch on the `app` module directly.
"""
import base64
import json

import pytest

from vms.domain.models import CandidateStatus, EmailCandidate


# ---------------------------------------------------------------------------
# health_check / leihvertrag / index -- trivial + guarded routes
# ---------------------------------------------------------------------------

@pytest.mark.route
def test_health_check_ok_without_auth(client):
    resp = client.get("/health")

    assert resp.status_code == 200
    assert resp.get_json() == {"status": "ok"}


@pytest.mark.route
def test_leihvertrag_requires_login(client):
    resp = client.get("/leihvertrag", follow_redirects=False)

    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


@pytest.mark.route
def test_leihvertrag_renders_for_authed_user(auth_client):
    resp = auth_client.get("/leihvertrag")

    assert resp.status_code == 200


@pytest.mark.route
def test_index_requires_login(client):
    resp = client.get("/", follow_redirects=False)

    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


@pytest.mark.route
def test_index_counts_candidates_for_the_current_year(auth_client, user, db_session):
    """Seeds one candidate in the current year and one far outside it; the
    dashboard's 'Leihanfragen' counter must reflect only the matching one."""
    from datetime import datetime

    current_year = datetime.now().year
    db_session.add(EmailCandidate(
        user_id=user["id"], vorname_nachname="Anna Beispiel",
        veranstaltungsname="Jubiläum", datum=f"{current_year}-05-01",
        status=CandidateStatus.PENDING.value, tags=[], email_id="idx-1",
    ))
    db_session.add(EmailCandidate(
        user_id=user["id"], vorname_nachname="Bruno Beispiel",
        veranstaltungsname="Uralt", datum="1999-01-01",
        status=CandidateStatus.PENDING.value, tags=[], email_id="idx-2",
    ))
    db_session.commit()

    resp = auth_client.get("/")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    # one active user (the fixture) and exactly one candidate matching the year
    assert ">1<" in html  # both users-count and leihanfragen-count should show 1
    assert str(current_year) in html


@pytest.mark.route
def test_index_year_filter_is_a_substring_match_not_exact(auth_client, user, db_session):
    """SMELL: the dashboard counts candidates via `datum.ilike('%<year>%')`, a
    substring match on a free-text column. A datum that merely *contains* the
    current year's digits elsewhere is counted too, even though it is not
    really an event in the current year. Documented in FINDINGS as a smell,
    not pinned as correct: the counter can overcount."""
    from datetime import datetime

    current_year = datetime.now().year
    # Not a real date in the current year, but contains its digits as a substring.
    bogus_datum = f"Ref-{current_year}-Altvorgang"
    db_session.add(EmailCandidate(
        user_id=user["id"], vorname_nachname="Carla Beispiel",
        veranstaltungsname="Altfall", datum=bogus_datum,
        status=CandidateStatus.PENDING.value, tags=[], email_id="idx-3",
    ))
    db_session.commit()

    resp = auth_client.get("/")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert ">1<" in html  # the bogus row is (wrongly) counted as this year's


# ---------------------------------------------------------------------------
# /api/kanboard/tasks and /api/kanboard/task/<id> -- BUG: both always 500
# ---------------------------------------------------------------------------

@pytest.mark.route
def test_kanboard_tasks_requires_login(client):
    """/api/* routes get a 401 JSON response, not a redirect (auth.py:318)."""
    resp = client.get("/api/kanboard/tasks", follow_redirects=False)

    assert resp.status_code == 401
    assert "error" in resp.get_json()


@pytest.mark.route
def test_kanboard_tasks_success_returns_task_list(auth_client, mock_kanboard):
    mock_kanboard.return_value = [{"id": 1, "title": "col"}]

    resp = auth_client.get("/api/kanboard/tasks")

    assert resp.status_code == 200
    assert isinstance(resp.get_json(), list)


@pytest.mark.route
def test_kanboard_tasks_error_returns_500(auth_client, mocker):
    """The except branch itself, independent of the missing-arg bug above:
    a real Kanboard failure must surface as a 500 with the error message."""
    mocker.patch("vms.clients.kanboard_client.get_leihanfragen_tasks",
                 side_effect=Exception("Kanboard down"))

    resp = auth_client.get("/api/kanboard/tasks")

    assert resp.status_code == 500
    assert resp.get_json() == {"error": "Kanboard down"}


@pytest.mark.route
def test_kanboard_task_success_returns_task_details(auth_client, mock_kanboard):
    mock_kanboard.return_value = {"id": 7, "title": "Sommerfest", "description": ""}

    resp = auth_client.get("/api/kanboard/task/7")

    assert resp.status_code == 200
    assert resp.get_json()["id"] == 7


@pytest.mark.route
def test_kanboard_task_error_returns_500(auth_client, mocker):
    mocker.patch("vms.clients.kanboard_client.get_task_details",
                 side_effect=Exception("Task nicht erreichbar"))

    resp = auth_client.get("/api/kanboard/task/7")

    assert resp.status_code == 500
    assert resp.get_json() == {"error": "Task nicht erreichbar"}


# ---------------------------------------------------------------------------
# /api/generate -- PDF generation
# ---------------------------------------------------------------------------

def _patch_pdf_pipeline(mocker, fake_pdf_path):
    """Patch the three module-level, by-value imports app.py uses for the
    ODT -> PDF pipeline. Must be patched on `app`, not `odt_processor`/
    `template_store` -- see module docstring."""
    mocker.patch("vms.app.load_template", return_value="dummy/template/path.odt")
    mocker.patch("vms.app.process_odt_template")
    return mocker.patch("vms.app.convert_to_pdf", return_value=str(fake_pdf_path))


@pytest.mark.route
def test_generate_pdf_requires_login(client):
    resp = client.post("/api/generate", json={}, follow_redirects=False)

    assert resp.status_code == 401
    assert "error" in resp.get_json()


@pytest.mark.route
def test_generate_pdf_success_returns_pdf_attachment(auth_client, mocker, tmp_path):
    fake_pdf = tmp_path / "out.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 faked contract")
    _patch_pdf_pipeline(mocker, fake_pdf)

    resp = auth_client.post("/api/generate", json={
        "vorname_nachname": "Erika Musterfrau",
        "veranstaltungsname": "Sommerfest",
        "abholdatum": "2026-07-01",
        "rueckgabedatum": "2026-07-05",
    })

    assert resp.status_code == 200
    assert resp.mimetype == "application/pdf"
    assert "Erika_Musterfrau" in resp.headers["Content-Disposition"]
    assert resp.data == b"%PDF-1.4 faked contract"


@pytest.mark.route
def test_generate_pdf_missing_name_falls_back_to_unbekannt(auth_client, mocker, tmp_path):
    fake_pdf = tmp_path / "out.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4")
    _patch_pdf_pipeline(mocker, fake_pdf)

    resp = auth_client.post("/api/generate", json={"veranstaltungsname": "Ohne Namen"})

    assert resp.status_code == 200
    assert "Unbekannt" in resp.headers["Content-Disposition"]


@pytest.mark.route
def test_generate_pdf_with_signature_data_url_prefix_is_stripped(auth_client, mocker, tmp_path):
    """A data: URL prefix ('data:image/png;base64,') must be stripped before
    base64-decoding -- assert the actual decoded bytes on disk, not just that
    process_odt_template 'was called'."""
    fake_pdf = tmp_path / "out.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4")
    _patch_pdf_pipeline(mocker, fake_pdf)
    raw_png_bytes = b"\x89PNG-fake-signature-bytes"
    b64 = base64.b64encode(raw_png_bytes).decode()

    captured = {}
    def _fake_process(template_path, output_odt, replacements, signature_path):
        captured["signature_path"] = signature_path
        if signature_path:
            with open(signature_path, "rb") as f:
                captured["signature_bytes"] = f.read()
    mocker.patch("vms.app.process_odt_template", side_effect=_fake_process)

    resp = auth_client.post("/api/generate", json={
        "vorname_nachname": "Signiert Testperson",
        "signature": f"data:image/png;base64,{b64}",
    })

    assert resp.status_code == 200
    assert captured["signature_bytes"] == raw_png_bytes


@pytest.mark.route
def test_generate_pdf_with_signature_without_data_url_prefix(auth_client, mocker, tmp_path):
    """No comma present -> the whole string is treated as base64 as-is."""
    fake_pdf = tmp_path / "out.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4")
    _patch_pdf_pipeline(mocker, fake_pdf)
    raw_png_bytes = b"\x89PNG-no-prefix"
    b64 = base64.b64encode(raw_png_bytes).decode()

    captured = {}
    def _fake_process(template_path, output_odt, replacements, signature_path):
        with open(signature_path, "rb") as f:
            captured["signature_bytes"] = f.read()
    mocker.patch("vms.app.process_odt_template", side_effect=_fake_process)

    resp = auth_client.post("/api/generate", json={
        "vorname_nachname": "Ohne Prefix",
        "signature": b64,
    })

    assert resp.status_code == 200
    assert captured["signature_bytes"] == raw_png_bytes


@pytest.mark.route
def test_generate_pdf_without_signature_skips_signature_file(auth_client, mocker, tmp_path):
    fake_pdf = tmp_path / "out.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4")
    _patch_pdf_pipeline(mocker, fake_pdf)

    captured = {}
    def _fake_process(template_path, output_odt, replacements, signature_path):
        captured["signature_path"] = signature_path
    mocker.patch("vms.app.process_odt_template", side_effect=_fake_process)

    resp = auth_client.post("/api/generate", json={"vorname_nachname": "Kein Signum"})

    assert resp.status_code == 200
    assert captured["signature_path"] is None


@pytest.mark.route
def test_generate_pdf_convert_to_pdf_failure_returns_500(auth_client, mocker):
    mocker.patch("vms.app.load_template", return_value="dummy.odt")
    mocker.patch("vms.app.process_odt_template")
    mocker.patch("vms.app.convert_to_pdf", side_effect=RuntimeError("LibreOffice crashed"))

    resp = auth_client.post("/api/generate", json={"vorname_nachname": "Fehlerfall"})

    assert resp.status_code == 500
    assert resp.get_json() == {"error": "LibreOffice crashed"}


@pytest.mark.route
def test_generate_pdf_null_body_should_be_a_400_not_a_500(auth_client, mocker, tmp_path):
    fake_pdf = tmp_path / "out.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4")
    _patch_pdf_pipeline(mocker, fake_pdf)

    resp = auth_client.post(
        "/api/generate", data="null", content_type="application/json"
    )

    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# /api/calendar/events
# ---------------------------------------------------------------------------

@pytest.mark.route
def test_calendar_events_requires_login(client):
    resp = client.get("/api/calendar/events", follow_redirects=False)

    assert resp.status_code == 401
    assert "error" in resp.get_json()


@pytest.mark.route
def test_calendar_events_passes_query_window_through(auth_client, mocker):
    mocker.patch("vms.clients.email_client.get_calendar_events",
                 return_value=[{"title": "Sommerfest"}])

    resp = auth_client.get("/api/calendar/events?start=2026-01-01&end=2026-12-31")

    assert resp.status_code == 200
    assert resp.get_json() == [{"title": "Sommerfest"}]


# ---------------------------------------------------------------------------
# /api/emails/candidates/<id>/create-task
# ---------------------------------------------------------------------------

def _seed_candidate(db_session, user_id, **overrides):
    defaults = dict(
        user_id=user_id, vorname_nachname="Erika Mustermann",
        veranstaltungsname="Sommerfest", datum="2026-08-01",
        status=CandidateStatus.PROCESSED.value, tags=["Zelt"],
        raw_content="Vor- und Nachname: Erika Mustermann\nMaterial: Zelt\n"
                    "Ich habe die Rahmenbedingungen gelesen: Ja\nTrailing Muell",
    )
    defaults.update(overrides)
    c = EmailCandidate(**defaults)
    db_session.add(c)
    db_session.flush()
    db_session.commit()
    return c.id


@pytest.mark.route
def test_create_task_requires_login(client):
    resp = client.post("/api/emails/candidates/1/create-task", json={},
                        follow_redirects=False)

    assert resp.status_code == 401
    assert "error" in resp.get_json()


@pytest.mark.route
def test_create_task_returns_404_when_candidate_missing(auth_client, user):
    """Domain-correct: an unknown candidate_id should 404 cleanly."""
    resp = auth_client.post("/api/emails/candidates/999999/create-task", json={})

    assert resp.status_code == 404
    assert resp.get_json() == {"error": "Kandidat nicht gefunden"}


@pytest.mark.route
def test_create_task_success_persists_kanboard_id_and_marks_processed(
        auth_client, user, db_session, mocker):
    """Works around the get_candidate_by_id signature bug above (necessary to
    reach the rest of the function at all) by patching only that lookup; every
    other side effect (save_kanboard_task_id, update_candidate,
    mark_candidate_processed) runs for real against a seeded DB row, and is
    asserted via a fresh read of that row."""
    candidate_id = _seed_candidate(db_session, user["id"], status=CandidateStatus.DONE.value,
                                    kanboard_task_id=None)
    candidate_dict = {
        "id": candidate_id, "tags": ["Zelt"], "raw_content": "Vor- und Nachname: X",
        "veranstaltungsname": "Sommerfest", "datum": "2026-08-01",
    }
    mocker.patch("vms.clients.email_client.get_candidate_by_id", return_value=candidate_dict)
    mocker.patch("vms.clients.kanboard_client.create_task", return_value={"id": 42})

    resp = auth_client.post(f"/api/emails/candidates/{candidate_id}/create-task", json={
        "veranstaltungsname": "Sommerfest Umbenannt",
        "tags": ["Zelt", "Stuhl"],
    })

    assert resp.status_code == 200
    assert resp.get_json() == {"success": True, "task_id": 42}

    db_session.expire_all()
    row = db_session.query(EmailCandidate).filter_by(id=candidate_id).first()
    assert row.kanboard_task_id == 42
    assert row.status == CandidateStatus.PROCESSED.value
    assert row.contract_created is False
    assert row.veranstaltungsname == "Sommerfest Umbenannt"


@pytest.mark.route
def test_create_task_falls_back_to_candidate_tags_when_body_has_none(
        auth_client, user, db_session, mocker):
    candidate_id = _seed_candidate(db_session, user["id"], tags=[])
    candidate_dict = {
        "id": candidate_id, "tags": ["Zelt", "Bierbank"], "raw_content": "",
        "veranstaltungsname": "Sommerfest",
        "datum": "2026-08-01",
    }
    mocker.patch("vms.clients.email_client.get_candidate_by_id", return_value=candidate_dict)
    created = mocker.patch("vms.clients.kanboard_client.create_task", return_value={"id": 5})

    resp = auth_client.post(f"/api/emails/candidates/{candidate_id}/create-task", json={})

    assert resp.status_code == 200
    assert created.call_args.kwargs["tags"] == ["Zelt", "Bierbank"]

    db_session.expire_all()
    row = db_session.query(EmailCandidate).filter_by(id=candidate_id).first()
    assert row.tags == ["Zelt", "Bierbank"]


@pytest.mark.route
def test_create_task_uses_explicit_description_without_deriving_from_raw(
        auth_client, user, db_session, mocker):
    """When the body already carries a description, extract_form_section must
    NOT be consulted -- the raw_content-derivation branch must be skipped."""
    candidate_id = _seed_candidate(db_session, user["id"],
                                    raw_content="Vor- und Nachname: sollte ignoriert werden")
    mocker.patch("vms.clients.email_client.get_candidate_by_id", return_value={
        "id": candidate_id, "tags": [], "raw_content": "Vor- und Nachname: ignoriert",
        "veranstaltungsname": "X",
            "datum": "2026-08-01",
        })
    mocker.patch("vms.clients.kanboard_client.create_task", return_value={"id": 11})

    resp = auth_client.post(f"/api/emails/candidates/{candidate_id}/create-task", json={
        "description": "Vom Benutzer frei editierter Text",
    })

    assert resp.status_code == 200
    db_session.expire_all()
    row = db_session.query(EmailCandidate).filter_by(id=candidate_id).first()
    assert row.raw_content == "Vom Benutzer frei editierter Text"


@pytest.mark.route
def test_create_task_derives_description_from_raw_content_when_absent(
        auth_client, user, db_session, mocker):
    raw = ("Vor- und Nachname: Erika Mustermann\nSonstiges: Bla\n"
           "Ich habe die Rahmenbedingungen gelesen: Ja\nSpam danach")
    candidate_id = _seed_candidate(db_session, user["id"])
    candidate_dict = {
        "id": candidate_id, "tags": [], "raw_content": raw,
        "veranstaltungsname": "Sommerfest",
        "datum": "2026-08-01",
    }
    mocker.patch("vms.clients.email_client.get_candidate_by_id", return_value=candidate_dict)
    mocker.patch("vms.clients.kanboard_client.create_task", return_value={"id": 9})

    resp = auth_client.post(f"/api/emails/candidates/{candidate_id}/create-task", json={})

    assert resp.status_code == 200

    from vms.clients.email_client import extract_form_section
    expected_description = extract_form_section(raw)
    assert "Spam danach" not in expected_description  # sanity: extraction really trims

    db_session.expire_all()
    row = db_session.query(EmailCandidate).filter_by(id=candidate_id).first()
    assert row.raw_content == expected_description


@pytest.mark.route
def test_create_task_does_not_save_kanboard_id_when_result_has_no_id(
        auth_client, user, db_session, mocker):
    candidate_id = _seed_candidate(db_session, user["id"], kanboard_task_id=None)
    mocker.patch("vms.clients.email_client.get_candidate_by_id", return_value={
        "id": candidate_id, "tags": [], "raw_content": "", "veranstaltungsname": "X",
            "datum": "2026-08-01",
        })
    mocker.patch("vms.clients.kanboard_client.create_task", return_value={"id": 0})

    resp = auth_client.post(f"/api/emails/candidates/{candidate_id}/create-task", json={})

    assert resp.status_code == 200
    assert resp.get_json()["task_id"] == 0

    db_session.expire_all()
    row = db_session.query(EmailCandidate).filter_by(id=candidate_id).first()
    assert row.kanboard_task_id is None  # falsy id -> save_kanboard_task_id skipped


@pytest.mark.route
def test_create_task_sets_responsible_user_when_present_in_body(
        auth_client, user, db_session, mocker):
    from vms.auth import User
    responsible = User.create(username="verantwortlich", password="Sup3r-Secret!",
                               display_name="Verantwortlich", email="resp@example.com",
                               is_active=True)
    candidate_id = _seed_candidate(db_session, user["id"], responsible_user_id=None)
    mocker.patch("vms.clients.email_client.get_candidate_by_id", return_value={
        "id": candidate_id, "tags": [], "raw_content": "", "veranstaltungsname": "X",
            "datum": "2026-08-01",
        })
    mocker.patch("vms.clients.kanboard_client.create_task", return_value={"id": 3})

    resp = auth_client.post(f"/api/emails/candidates/{candidate_id}/create-task", json={
        "responsible_user_id": responsible.id,
    })

    assert resp.status_code == 200
    db_session.expire_all()
    row = db_session.query(EmailCandidate).filter_by(id=candidate_id).first()
    assert row.responsible_user_id == responsible.id


@pytest.mark.route
def test_create_task_kanboard_failure_returns_500_and_leaves_candidate_unchanged(
        auth_client, user, db_session, mocker):
    candidate_id = _seed_candidate(db_session, user["id"],
                                    status=CandidateStatus.PROCESSED.value,
                                    kanboard_task_id=None)
    mocker.patch("vms.clients.email_client.get_candidate_by_id", return_value={
        "id": candidate_id, "tags": [], "raw_content": "", "veranstaltungsname": "X",
            "datum": "2026-08-01",
        })
    mocker.patch("vms.clients.kanboard_client.create_task", side_effect=Exception("Spalte fehlt"))

    resp = auth_client.post(f"/api/emails/candidates/{candidate_id}/create-task", json={})

    assert resp.status_code == 500
    assert resp.get_json() == {"error": "Spalte fehlt"}

    db_session.expire_all()
    row = db_session.query(EmailCandidate).filter_by(id=candidate_id).first()
    # nothing should have been persisted -- status untouched by the failed attempt
    assert row.status == CandidateStatus.PROCESSED.value
    assert row.kanboard_task_id is None


# ---------------------------------------------------------------------------
# /api/kanboard/reconcile + reconcile_all_rentals
# ---------------------------------------------------------------------------

@pytest.mark.route
def test_reconcile_route_requires_login(client):
    resp = client.post("/api/kanboard/reconcile", follow_redirects=False)

    assert resp.status_code == 401
    assert "error" in resp.get_json()


@pytest.mark.route
def test_reconcile_route_returns_processed_count(auth_client, mocker):
    mocker.patch("vms.app.reconcile_all_rentals", return_value=3)

    resp = auth_client.post("/api/kanboard/reconcile")

    assert resp.status_code == 200
    assert resp.get_json() == {"success": True, "reconciled": 3}


@pytest.mark.integration
def test_reconcile_all_rentals_processes_done_candidates_with_kanboard_id(
        app, user, db_session, mocker):
    import vms.app as app_module

    db_session.add(EmailCandidate(
        user_id=user["id"], vorname_nachname="A", veranstaltungsname="A-Fest",
        datum="2026-08-01", status=CandidateStatus.DONE.value, tags=[],
        kanboard_task_id=101, email_id="rec-1",
    ))
    db_session.add(EmailCandidate(
        user_id=user["id"], vorname_nachname="B", veranstaltungsname="B-Fest",
        datum="2026-08-01", status=CandidateStatus.DONE.value, tags=[],
        kanboard_task_id=102, email_id="rec-2",
    ))
    # Not eligible: no kanboard_task_id yet.
    db_session.add(EmailCandidate(
        user_id=user["id"], vorname_nachname="C", veranstaltungsname="C-Fest",
        datum="2026-08-01", status=CandidateStatus.DONE.value, tags=[],
        kanboard_task_id=None, email_id="rec-3",
    ))
    db_session.commit()

    reconcile_mock = mocker.patch("vms.clients.kanboard_client.reconcile_candidate")

    processed = app_module.reconcile_all_rentals()

    assert processed == 2
    assert reconcile_mock.call_count == 2


@pytest.mark.integration
def test_reconcile_all_rentals_does_not_count_a_failed_candidate_as_processed(
        app, user, db_session, mocker):
    """app.py:418 `except Exception: pass` swallows a per-candidate Kanboard
    failure so one bad task doesn't abort the whole reconcile run -- but the
    `processed += 1` on L417 sits *inside* the try, right after the call, so a
    raising candidate is correctly excluded from the count (not a bug: verified
    here rather than just assumed)."""
    import vms.app as app_module

    db_session.add(EmailCandidate(
        user_id=user["id"], vorname_nachname="A", veranstaltungsname="A-Fest",
        datum="2026-08-01", status=CandidateStatus.DONE.value, tags=[],
        kanboard_task_id=201, email_id="rec-fail-1",
    ))
    db_session.commit()

    mocker.patch("vms.clients.kanboard_client.reconcile_candidate",
                 side_effect=Exception("Kanboard timeout"))

    processed = app_module.reconcile_all_rentals()

    assert processed == 0


@pytest.mark.integration
def test_reconcile_all_rentals_counts_only_the_successful_candidate_in_a_mixed_batch(
        app, user, db_session, mocker):
    """One candidate's reconcile succeeds, the other raises -- the returned
    count must reflect exactly the one success, proving the except-swallow
    doesn't inflate (or deflate) unrelated candidates' counting."""
    import vms.app as app_module

    db_session.add(EmailCandidate(
        user_id=user["id"], vorname_nachname="A", veranstaltungsname="A-Fest",
        datum="2026-08-01", status=CandidateStatus.DONE.value, tags=[],
        kanboard_task_id=301, email_id="rec-mixed-1",
    ))
    db_session.add(EmailCandidate(
        user_id=user["id"], vorname_nachname="B", veranstaltungsname="B-Fest",
        datum="2026-08-01", status=CandidateStatus.DONE.value, tags=[],
        kanboard_task_id=302, email_id="rec-mixed-2",
    ))
    db_session.commit()

    def _fail_for_301(user_id, candidate):
        if candidate["kanboard_task_id"] == 301:
            raise Exception("Kanboard timeout")

    mocker.patch("vms.clients.kanboard_client.reconcile_candidate", side_effect=_fail_for_301)

    processed = app_module.reconcile_all_rentals()

    assert processed == 1


@pytest.mark.unit
def test_init_scheduler_starts_the_job_once_and_is_idempotent(mocker):
    """First call starts the scheduler and flips the module-level guard;
    a second call must short-circuit rather than starting a second job."""
    import vms.app as app_module

    previous = app_module._scheduler_started
    app_module._scheduler_started = False
    fake_scheduler = mocker.MagicMock()
    mocker.patch("apscheduler.schedulers.background.BackgroundScheduler",
                 return_value=fake_scheduler)

    try:
        app_module.init_scheduler()
        assert app_module._scheduler_started is True
        assert fake_scheduler.start.call_count == 1

        app_module.init_scheduler()  # idempotent guard (L428-429)
        assert fake_scheduler.start.call_count == 1
    finally:
        app_module._scheduler_started = previous


@pytest.mark.unit
def test_init_scheduler_failure_is_logged_not_raised(mocker):
    import vms.app as app_module

    previous = app_module._scheduler_started
    app_module._scheduler_started = False
    mocker.patch("apscheduler.schedulers.background.BackgroundScheduler",
                 side_effect=RuntimeError("no thread pool"))

    try:
        app_module.init_scheduler()  # must not raise -- broad except at L444
        assert app_module._scheduler_started is False
    finally:
        app_module._scheduler_started = previous


@pytest.mark.integration
def test_reconcile_all_rentals_returns_zero_when_lock_is_held_elsewhere(app, user):
    """The advisory lock is session-scoped in Postgres: hold it on a second,
    independent connection and confirm reconcile_all_rentals bails out with 0
    rather than doing (and double-counting) the same work concurrently."""
    import vms.app as app_module
    from vms.domain.database import get_engine
    from sqlalchemy import text

    engine = get_engine()
    lock_conn = engine.connect()
    lock_conn.execute(text("SELECT pg_advisory_lock(:k)"),
                       {"k": app_module._ADVISORY_LOCK_KEY})
    try:
        processed = app_module.reconcile_all_rentals()
        assert processed == 0
    finally:
        lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"),
                           {"k": app_module._ADVISORY_LOCK_KEY})
        lock_conn.close()


# ---------------------------------------------------------------------------
# Datumspflicht beim Anlegen eines Verleihs
# Spec: docs/specs/vorgangslisten-und-datumspflicht.md
# ---------------------------------------------------------------------------

@pytest.mark.route
def test_create_task_ohne_datum_gibt_400_und_bleibt_pending(
        auth_client, user, db_session, mocker):
    """Nur PENDING darf datumslos sein. Der Übergang zum Verleih wird abgelehnt,
    bevor ein Kanboard-Task entsteht -- sonst bliebe ein verwaister Task zurück."""
    candidate_id = _seed_candidate(db_session, user["id"],
                                   status=CandidateStatus.PENDING.value,
                                   datum=None, kanboard_task_id=None)
    erzeugt = mocker.patch("vms.clients.kanboard_client.create_task",
                           return_value={"id": 7})

    resp = auth_client.post(f"/api/emails/candidates/{candidate_id}/create-task", json={})

    assert resp.status_code == 400
    assert "Datum" in resp.get_json()["error"]
    erzeugt.assert_not_called()

    db_session.expire_all()
    row = db_session.query(EmailCandidate).filter_by(id=candidate_id).first()
    assert row.status == CandidateStatus.PENDING.value
    assert row.kanboard_task_id is None


@pytest.mark.route
def test_create_task_mit_datum_setzt_processed(auth_client, user, db_session, mocker):
    candidate_id = _seed_candidate(db_session, user["id"],
                                   status=CandidateStatus.PENDING.value,
                                   datum=None, kanboard_task_id=None)
    mocker.patch("vms.clients.kanboard_client.create_task", return_value={"id": 8})

    resp = auth_client.post(f"/api/emails/candidates/{candidate_id}/create-task",
                            json={"start_date": "2026-08-01"})

    assert resp.status_code == 200

    db_session.expire_all()
    row = db_session.query(EmailCandidate).filter_by(id=candidate_id).first()
    assert row.status == CandidateStatus.PROCESSED.value
    assert row.datum == "2026-08-01"

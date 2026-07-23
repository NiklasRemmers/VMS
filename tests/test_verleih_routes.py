"""Tests for verleih_routes.py: storage-location CRUD/assignment, the public
code-share flow (send-codes email + the token-gated /codes/<token> page), and
the assignable-loans listing.

Mock boundaries: mail is captured with the `mailbox` fixture (never touches
SMTP); `auth.send_plain_email` is patched directly for the failure branch
because it is imported *inside* the route function (`from auth import
send_plain_email`), so patching `verleih_routes.send_plain_email` would not
intercept the call. `email_client.get_candidate_by_id` / `get_candidates` are
exercised for real against seeded `EmailCandidate` rows rather than mocked,
since they are simple DB reads and mocking them would hide the wiring bugs
this module has (see FINDINGS).
"""
import pytest

from vms.domain.models import EmailCandidate, StorageLocation, CodeShareLink, CandidateStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_candidate(db_session, user_id, **overrides):
    defaults = dict(
        user_id=user_id,
        vorname_nachname="Erika Mustermann",
        veranstaltungsname="Sommerfest",
        email_address="erika@example.com",
        datum="2026-07-22",
        status=CandidateStatus.PROCESSED.value,
        tags=[],
    )
    defaults.update(overrides)
    c = EmailCandidate(**defaults)
    db_session.add(c)
    db_session.flush()
    return c.id


def _make_location(db_session, **overrides):
    defaults = dict(name="Schrank 1", code="1234")
    defaults.update(overrides)
    loc = StorageLocation(**defaults)
    db_session.add(loc)
    db_session.flush()
    return loc.id


# ---------------------------------------------------------------------------
# verleih_page (GET /verleih)
# ---------------------------------------------------------------------------

@pytest.mark.route
def test_verleih_page_renders_for_authenticated_user(auth_client):
    resp = auth_client.get("/verleih")

    assert resp.status_code == 200


@pytest.mark.route
def test_verleih_page_redirects_anonymous_to_login(client):
    resp = client.get("/verleih", follow_redirects=False)

    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


# ---------------------------------------------------------------------------
# get_locations (GET /api/verleih/locations)
# ---------------------------------------------------------------------------

@pytest.mark.route
def test_get_locations_returns_rows_ordered_by_name(auth_client, db_session, user):
    _make_location(db_session, name="Zebra-Schrank")
    _make_location(db_session, name="Anker-Schrank")
    db_session.commit()

    resp = auth_client.get("/api/verleih/locations")

    assert resp.status_code == 200
    names = [row["name"] for row in resp.get_json()]
    assert names == ["Anker-Schrank", "Zebra-Schrank"]


@pytest.mark.route
def test_get_locations_rejects_anonymous(client):
    resp = client.get("/api/verleih/locations")

    assert resp.status_code == 401
    assert "error" in resp.get_json()


# ---------------------------------------------------------------------------
# create_location (POST /api/verleih/locations)
# ---------------------------------------------------------------------------

@pytest.mark.route
def test_create_location_persists_row(auth_client, db_session):
    resp = auth_client.post("/api/verleih/locations", json={"name": "Spind 3", "code": "9999"})

    assert resp.status_code == 201
    body = resp.get_json()
    assert body["name"] == "Spind 3"
    assert body["code"] == "9999"
    assert body["candidate_id"] is None

    row = db_session.query(StorageLocation).filter_by(name="Spind 3").first()
    assert row is not None
    assert row.code == "9999"


@pytest.mark.route
@pytest.mark.parametrize("payload", [{}, {"name": ""}, {"name": "   "}, {"name": None}])
def test_create_location_missing_name_returns_400(auth_client, payload):
    resp = auth_client.post("/api/verleih/locations", json=payload)

    assert resp.status_code == 400
    assert resp.get_json()["error"] == "Name erforderlich"


@pytest.mark.route
def test_create_location_duplicate_name_conflicts(auth_client, db_session):
    _make_location(db_session, name="Schrank 5", code="A")
    db_session.commit()

    resp = auth_client.post("/api/verleih/locations", json={"name": "Schrank 5", "code": "B"})

    assert resp.status_code == 409
    assert "existiert bereits" in resp.get_json()["error"]
    # only the original row survives -- the failed insert rolled back
    rows = db_session.query(StorageLocation).filter_by(name="Schrank 5").all()
    assert len(rows) == 1
    assert rows[0].code == "A"


@pytest.mark.route
def test_create_location_rejects_anonymous(client):
    resp = client.post("/api/verleih/locations", json={"name": "X"})

    assert resp.status_code == 401


@pytest.mark.route
def test_create_location_non_integrity_db_error_returns_500(auth_client, db_session):
    """code is varchar(100); a too-long value trips a DataError (not an
    IntegrityError), exercising the bare `except Exception` at :57-59."""
    resp = auth_client.post(
        "/api/verleih/locations", json={"name": "Überlang", "code": "x" * 200}
    )

    assert resp.status_code == 500
    assert resp.get_json()["error"]
    assert db_session.query(StorageLocation).filter_by(name="Überlang").first() is None


# ---------------------------------------------------------------------------
# update_location (PUT /api/verleih/locations/<id>)
# ---------------------------------------------------------------------------

@pytest.mark.route
def test_update_location_persists_change(auth_client, db_session):
    loc_id = _make_location(db_session, name="Altbezeichnung", code="OLD")
    db_session.commit()

    resp = auth_client.put(f"/api/verleih/locations/{loc_id}", json={"name": "Neubezeichnung", "code": "NEW"})

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["name"] == "Neubezeichnung"
    assert body["code"] == "NEW"

    db_session.expire_all()
    row = db_session.query(StorageLocation).get(loc_id)
    assert row.name == "Neubezeichnung"
    assert row.code == "NEW"


@pytest.mark.route
def test_update_location_updates_code_only_leaves_name(auth_client, db_session):
    """Only 'code' present in the payload -- the 'name' branch (:72) must be
    skipped, not clear the name."""
    loc_id = _make_location(db_session, name="Bleibt gleich", code="OLD")
    db_session.commit()

    resp = auth_client.put(f"/api/verleih/locations/{loc_id}", json={"code": "NEW"})

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["name"] == "Bleibt gleich"
    assert body["code"] == "NEW"


@pytest.mark.route
def test_update_location_not_found_returns_404(auth_client):
    resp = auth_client.put("/api/verleih/locations/999999", json={"name": "X"})

    assert resp.status_code == 404
    assert resp.get_json()["error"] == "Lagerort nicht gefunden"


@pytest.mark.route
def test_update_location_duplicate_name_conflicts(auth_client, db_session):
    _make_location(db_session, name="Schrank A")
    other_id = _make_location(db_session, name="Schrank B")
    db_session.commit()

    resp = auth_client.put(f"/api/verleih/locations/{other_id}", json={"name": "Schrank A"})

    assert resp.status_code == 409
    assert resp.get_json()["error"] == "Name bereits vergeben"

    db_session.expire_all()
    row = db_session.query(StorageLocation).get(other_id)
    assert row.name == "Schrank B"  # unchanged, rolled back


# ---------------------------------------------------------------------------
# delete_location (DELETE /api/verleih/locations/<id>)
# ---------------------------------------------------------------------------

@pytest.mark.route
def test_delete_location_removes_row(auth_client, db_session):
    loc_id = _make_location(db_session, name="Zu löschen")
    db_session.commit()

    resp = auth_client.delete(f"/api/verleih/locations/{loc_id}")

    assert resp.status_code == 200
    assert resp.get_json() == {"success": True}

    db_session.expire_all()
    assert db_session.query(StorageLocation).get(loc_id) is None


@pytest.mark.route
def test_delete_location_not_found_returns_404(auth_client):
    resp = auth_client.delete("/api/verleih/locations/999999")

    assert resp.status_code == 404
    assert resp.get_json()["error"] == "Lagerort nicht gefunden"


# ---------------------------------------------------------------------------
# assign_location (POST /api/verleih/locations/<id>/assign)
# ---------------------------------------------------------------------------

@pytest.mark.route
def test_assign_location_sets_candidate_id(auth_client, db_session, user):
    cid = _make_candidate(db_session, user["id"])
    loc_id = _make_location(db_session, name="Schrank X")
    db_session.commit()

    resp = auth_client.post(f"/api/verleih/locations/{loc_id}/assign", json={"candidate_id": cid})

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["candidate_id"] == cid
    assert body["candidate"]["id"] == cid

    db_session.expire_all()
    row = db_session.query(StorageLocation).get(loc_id)
    assert row.candidate_id == cid


@pytest.mark.route
def test_assign_location_missing_candidate_id_returns_400(auth_client, db_session):
    loc_id = _make_location(db_session)
    db_session.commit()

    resp = auth_client.post(f"/api/verleih/locations/{loc_id}/assign", json={})

    assert resp.status_code == 400
    assert resp.get_json()["error"] == "candidate_id erforderlich"


@pytest.mark.route
def test_assign_location_candidate_not_found_returns_404(auth_client, db_session):
    loc_id = _make_location(db_session)
    db_session.commit()

    resp = auth_client.post(f"/api/verleih/locations/{loc_id}/assign", json={"candidate_id": 999999})

    assert resp.status_code == 404
    assert resp.get_json()["error"] == "Verleih nicht gefunden"


@pytest.mark.route
def test_assign_location_location_not_found_returns_404(auth_client, db_session, user):
    cid = _make_candidate(db_session, user["id"])
    db_session.commit()

    resp = auth_client.post("/api/verleih/locations/999999/assign", json={"candidate_id": cid})

    assert resp.status_code == 404
    assert resp.get_json()["error"] == "Lagerort nicht gefunden"


# ---------------------------------------------------------------------------
# unassign_location (POST /api/verleih/locations/<id>/unassign)
# ---------------------------------------------------------------------------

@pytest.mark.route
def test_unassign_location_clears_candidate_id(auth_client, db_session, user):
    cid = _make_candidate(db_session, user["id"])
    loc_id = _make_location(db_session, candidate_id=cid)
    db_session.commit()

    resp = auth_client.post(f"/api/verleih/locations/{loc_id}/unassign")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["candidate_id"] is None
    assert body["candidate"] is None

    db_session.expire_all()
    row = db_session.query(StorageLocation).get(loc_id)
    assert row.candidate_id is None


@pytest.mark.route
def test_unassign_location_not_found_returns_404(auth_client):
    resp = auth_client.post("/api/verleih/locations/999999/unassign")

    assert resp.status_code == 404
    assert resp.get_json()["error"] == "Lagerort nicht gefunden"


# ---------------------------------------------------------------------------
# _get_or_create_share_token
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_get_or_create_share_token_reuses_existing_token(app, db_session, user):
    from vms.routes.verleih import _get_or_create_share_token

    cid = _make_candidate(db_session, user["id"])
    db_session.commit()

    first = _get_or_create_share_token(cid)
    second = _get_or_create_share_token(cid)

    assert first == second
    links = db_session.query(CodeShareLink).filter_by(candidate_id=cid).all()
    assert len(links) == 1
    assert links[0].token == first


# ---------------------------------------------------------------------------
# send_codes (POST /api/verleih/send-codes)
# ---------------------------------------------------------------------------

@pytest.mark.route
def test_send_codes_missing_candidate_id_returns_400(auth_client):
    resp = auth_client.post("/api/verleih/send-codes", json={"email": "a@b.de"})

    assert resp.status_code == 400
    assert resp.get_json()["error"] == "candidate_id erforderlich"


@pytest.mark.route
def test_send_codes_missing_email_returns_400(auth_client, db_session, user):
    cid = _make_candidate(db_session, user["id"])
    db_session.commit()

    resp = auth_client.post("/api/verleih/send-codes", json={"candidate_id": cid})

    assert resp.status_code == 400
    assert resp.get_json()["error"] == "E-Mail-Adresse erforderlich"


@pytest.mark.route
def test_send_codes_candidate_not_found_returns_404(auth_client):
    resp = auth_client.post(
        "/api/verleih/send-codes", json={"candidate_id": 999999, "email": "a@b.de"}
    )

    assert resp.status_code == 404
    assert resp.get_json()["error"] == "Verleih nicht gefunden"


@pytest.mark.route
def test_send_codes_without_assigned_location_returns_400(auth_client, db_session, user):
    cid = _make_candidate(db_session, user["id"])
    db_session.commit()

    resp = auth_client.post(
        "/api/verleih/send-codes", json={"candidate_id": cid, "email": "a@b.de"}
    )

    assert resp.status_code == 400
    assert resp.get_json()["error"] == "Diesem Verleih ist kein Lagerort zugeordnet"


@pytest.mark.route
def test_send_codes_success_emails_link_without_codes(auth_client, db_session, user, mailbox):
    cid = _make_candidate(db_session, user["id"], veranstaltungsname="Winterfest")
    _make_location(db_session, name="Schrank 7", code="GEHEIM-42", candidate_id=cid)
    db_session.commit()

    with mailbox as outbox:
        resp = auth_client.post(
            "/api/verleih/send-codes", json={"candidate_id": cid, "email": "empfaenger@example.com"}
        )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["success"] is True
    url = body["url"]
    assert url

    assert len(outbox) == 1
    msg = outbox[0]
    assert msg.recipients == ["empfaenger@example.com"]
    assert url in msg.body
    # the whole point of the link-based flow: no codes travel over email
    assert "GEHEIM-42" not in msg.body


@pytest.mark.route
def test_send_codes_reuses_the_same_token_across_calls(auth_client, db_session, user, mailbox):
    cid = _make_candidate(db_session, user["id"])
    _make_location(db_session, name="Schrank 8", code="X", candidate_id=cid)
    db_session.commit()

    with mailbox as outbox:
        first = auth_client.post(
            "/api/verleih/send-codes", json={"candidate_id": cid, "email": "a@b.de"}
        )
        second = auth_client.post(
            "/api/verleih/send-codes", json={"candidate_id": cid, "email": "a@b.de"}
        )

    assert first.get_json()["url"] == second.get_json()["url"]
    assert len(outbox) == 2


@pytest.mark.route
def test_send_codes_mail_failure_returns_500_but_still_includes_url(auth_client, db_session, user, mocker):
    cid = _make_candidate(db_session, user["id"])
    _make_location(db_session, name="Schrank 9", code="Y", candidate_id=cid)
    db_session.commit()

    mocker.patch("vms.auth.send_plain_email", side_effect=Exception("SMTP down"))

    resp = auth_client.post(
        "/api/verleih/send-codes", json={"candidate_id": cid, "email": "a@b.de"}
    )

    assert resp.status_code == 500
    body = resp.get_json()
    assert "SMTP down" in body["error"]
    assert body["url"]


@pytest.mark.route
def test_send_codes_rejects_anonymous(client):
    resp = client.post("/api/verleih/send-codes", json={"candidate_id": 1, "email": "a@b.de"})

    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# get_assignable_loans (GET /api/verleih/loans) -- returns only loans whose
# status is in ACTIVE_STATUSES (processed/done); pending/returned are excluded.
# ---------------------------------------------------------------------------

@pytest.mark.route
def test_get_assignable_loans_returns_only_active_statuses(auth_client, db_session, user):
    active_id = _make_candidate(db_session, user["id"], status=CandidateStatus.PROCESSED.value,
                                 email_id="e-active")
    done_id = _make_candidate(db_session, user["id"], status=CandidateStatus.DONE.value,
                               email_id="e-done", vorname_nachname="Done Kandidat")
    _make_candidate(db_session, user["id"], status=CandidateStatus.PENDING.value,
                     email_id="e-pending")
    _make_candidate(db_session, user["id"], status=CandidateStatus.RETURNED.value,
                     email_id="e-returned")
    db_session.commit()

    resp = auth_client.get("/api/verleih/loans")

    assert resp.status_code == 200
    ids = {row["id"] for row in resp.get_json()}
    assert ids == {active_id, done_id}


@pytest.mark.route
def test_get_assignable_loans_rejects_anonymous(client):
    resp = client.get("/api/verleih/loans")

    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# public_codes (GET /codes/<token>) -- no @login_required, reached via client
# ---------------------------------------------------------------------------

@pytest.mark.route
def test_public_codes_unknown_token_returns_404(client):
    resp = client.get("/codes/does-not-exist")

    assert resp.status_code == 404
    assert "ungültig" in resp.get_data(as_text=True).lower()


@pytest.mark.route
def test_public_codes_upcoming_when_start_date_in_future(client, db_session, user):
    from freezegun import freeze_time
    from vms.routes.verleih import _get_or_create_share_token

    cid = _make_candidate(db_session, user["id"], datum="2026-08-15")
    _make_location(db_session, name="Schrank 1", code="SECRET-1", candidate_id=cid)
    db_session.commit()
    token = _get_or_create_share_token(cid)

    with freeze_time("2026-08-01"):
        resp = client.get(f"/codes/{token}")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "sichtbar" in html
    assert "SECRET-1" not in html


@pytest.mark.route
@pytest.mark.parametrize("datum", [None, "", "kein-datum"])
def test_public_codes_upcoming_when_datum_unparseable_or_missing(client, db_session, user, datum):
    from vms.routes.verleih import _get_or_create_share_token

    cid = _make_candidate(db_session, user["id"], datum=datum)
    _make_location(db_session, name="Schrank 1", code="SECRET-2", candidate_id=cid)
    db_session.commit()
    token = _get_or_create_share_token(cid)

    resp = client.get(f"/codes/{token}")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "sichtbar" in html
    assert "SECRET-2" not in html


@pytest.mark.route
def test_public_codes_visible_during_loan_window_shows_codes(client, db_session, user):
    from freezegun import freeze_time
    from vms.routes.verleih import _get_or_create_share_token

    cid = _make_candidate(db_session, user["id"], datum="2026-08-15")
    _make_location(db_session, name="Schrank 1", code="SECRET-3", candidate_id=cid)
    db_session.commit()
    token = _get_or_create_share_token(cid)

    with freeze_time("2026-08-15"):
        resp = client.get(f"/codes/{token}")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "SECRET-3" in html
    assert "Schrank 1" in html


@pytest.mark.route
def test_public_codes_visible_within_explicit_end_date_range(client, db_session, user):
    from freezegun import freeze_time
    from vms.routes.verleih import _get_or_create_share_token

    cid = _make_candidate(db_session, user["id"], datum="2026-08-10", end_date="2026-08-20")
    _make_location(db_session, name="Schrank 1", code="SECRET-4", candidate_id=cid)
    db_session.commit()
    token = _get_or_create_share_token(cid)

    with freeze_time("2026-08-18"):
        resp = client.get(f"/codes/{token}")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "SECRET-4" in html


@pytest.mark.route
def test_public_codes_ended_after_single_day_loan_hides_codes(client, db_session, user):
    from freezegun import freeze_time
    from vms.routes.verleih import _get_or_create_share_token

    cid = _make_candidate(db_session, user["id"], datum="2026-08-15")
    _make_location(db_session, name="Schrank 1", code="SECRET-5", candidate_id=cid)
    db_session.commit()
    token = _get_or_create_share_token(cid)

    with freeze_time("2026-08-16"):
        resp = client.get(f"/codes/{token}")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "beendet" in html
    assert "SECRET-5" not in html


@pytest.mark.route
def test_public_codes_ended_after_explicit_end_date_hides_codes(client, db_session, user):
    from freezegun import freeze_time
    from vms.routes.verleih import _get_or_create_share_token

    cid = _make_candidate(db_session, user["id"], datum="2026-08-10", end_date="2026-08-20")
    _make_location(db_session, name="Schrank 1", code="SECRET-6", candidate_id=cid)
    db_session.commit()
    token = _get_or_create_share_token(cid)

    with freeze_time("2026-08-21"):
        resp = client.get(f"/codes/{token}")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "beendet" in html
    assert "SECRET-6" not in html


@pytest.mark.route
def test_public_codes_visible_but_no_locations_assigned_shows_empty_state(client, db_session, user):
    from freezegun import freeze_time
    from vms.routes.verleih import _get_or_create_share_token

    cid = _make_candidate(db_session, user["id"], datum="2026-08-15")
    db_session.commit()
    token = _get_or_create_share_token(cid)

    with freeze_time("2026-08-15"):
        resp = client.get(f"/codes/{token}")

    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "Keine Codes hinterlegt" in html

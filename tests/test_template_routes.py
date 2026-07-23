"""Tests for template_routes.py — the /api/templates blueprint.

See tests/test_template_store.py for the two fixture caveats that also apply
here: KMS needs an explicit master-key file (no SECRET_KEY fallback inside
kms.py), and template_store.py's already-imported `convert_to_pdf` name must be
patched directly (patching `odt_processor.convert_to_pdf` via the shared
`no_libreoffice` fixture does not reach it) to avoid a real LibreOffice call
from validate_template()'s probe render during upload.
"""
import hashlib
import io

import pytest

import vms.infra.template_store as template_store
from vms.infra.template_store import BUNDLED_TEMPLATES, TEMPLATE_LABELS


MASTER_KEY = b"k" * 32


@pytest.fixture(autouse=True)
def _kms_master_key(tmp_path_factory, monkeypatch, _kms_isolated):
    import vms.infra.kms as kms

    key_path = tmp_path_factory.mktemp("kms") / "master.key"
    key_path.write_bytes(MASTER_KEY)
    monkeypatch.setenv("KMS_MASTER_KEY_PATH", str(key_path))
    kms.clear_cache()
    yield


@pytest.fixture()
def stub_convert_to_pdf(mocker, tmp_path):
    fake_pdf = tmp_path / "probe.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 fake")
    return mocker.patch("vms.infra.template_store.convert_to_pdf", return_value=str(fake_pdf))


def _bundled_bytes(template_type):
    with open(BUNDLED_TEMPLATES[template_type], "rb") as f:
        return f.read()


def _upload(client, template_type="rechnung", filename="upload.odt",
            content=b"content", note=None):
    data = {
        "template_type": template_type,
        "template": (io.BytesIO(content), filename),
    }
    if note is not None:
        data["note"] = note
    return client.post(
        "/api/templates/upload", data=data, content_type="multipart/form-data",
    )


# ==========================================================================
# GET /api/templates
# ==========================================================================

@pytest.mark.route
def test_list_templates_first_call_seeds_all_three_types(auth_client, db_session):
    from vms.domain.models import DocumentTemplate

    resp = auth_client.get("/api/templates")

    assert resp.status_code == 200
    body = resp.get_json()
    assert {entry["template_type"] for entry in body} == set(TEMPLATE_LABELS)
    for entry in body:
        assert entry["active"]["version"] == 1

    db_session.expire_all()
    rows = db_session.query(DocumentTemplate).all()
    assert sorted(r.template_type for r in rows) == sorted(TEMPLATE_LABELS)
    assert all(r.version == 1 and r.is_active for r in rows)


@pytest.mark.route
def test_list_templates_anonymous_is_unauthorized(client):
    resp = client.get("/api/templates", follow_redirects=False)

    assert resp.status_code in (302, 401)


# ==========================================================================
# POST /api/templates/upload
# ==========================================================================

@pytest.mark.route
def test_upload_invalid_template_type_returns_400(auth_client):
    resp = _upload(auth_client, template_type="voodoo", content=b"x")

    assert resp.status_code == 400
    assert resp.get_json()["error"] == "Ungültiger Template-Typ"


@pytest.mark.route
def test_upload_without_file_returns_400(auth_client):
    resp = auth_client.post(
        "/api/templates/upload",
        data={"template_type": "rechnung"},
        content_type="multipart/form-data",
    )

    assert resp.status_code == 400
    assert resp.get_json()["error"] == "Keine Datei ausgewählt"


@pytest.mark.route
def test_upload_with_empty_filename_returns_400(auth_client):
    resp = auth_client.post(
        "/api/templates/upload",
        data={
            "template_type": "rechnung",
            "template": (io.BytesIO(b"x"), ""),
        },
        content_type="multipart/form-data",
    )

    assert resp.status_code == 400
    assert resp.get_json()["error"] == "Keine Datei ausgewählt"


@pytest.mark.route
def test_upload_non_odt_extension_returns_400(auth_client):
    resp = _upload(auth_client, filename="upload.docx", content=b"x")

    assert resp.status_code == 400
    assert resp.get_json()["error"] == "Nur .odt-Dateien sind erlaubt"


@pytest.mark.route
def test_upload_zero_byte_odt_returns_400(auth_client):
    resp = _upload(auth_client, filename="empty.odt", content=b"")

    assert resp.status_code == 400
    assert "leer" in resp.get_json()["error"]


@pytest.mark.route
def test_upload_failing_validation_returns_422_with_errors(auth_client, db_session):
    """A .odt that is structurally not an ODT at all fails validate_template's
    zip check before any render probe runs."""
    resp = _upload(auth_client, filename="broken.odt", content=b"not a real zip")

    assert resp.status_code == 422
    body = resp.get_json()
    assert body["error"] == "Vorlage abgelehnt"
    assert body["errors"]
    assert "beschädigt" in body["errors"][0]


@pytest.mark.route
def test_upload_success_persists_new_active_version(
        auth_client, user, db_session, stub_convert_to_pdf):
    from vms.domain.models import DocumentTemplate

    content = _bundled_bytes("rechnung")
    resp = _upload(auth_client, template_type="rechnung",
                   filename="template_rechnung.odt", content=content, note="Neu")

    assert resp.status_code == 200
    body = resp.get_json()
    stored = body["template"]
    assert stored["version"] == 2  # version 1 was seeded by ensure_seeded first
    assert stored["is_active"] is True

    db_session.expire_all()
    rows = (db_session.query(DocumentTemplate)
            .filter_by(template_type="rechnung")
            .order_by(DocumentTemplate.version).all())
    assert [r.version for r in rows] == [1, 2]
    v1, v2 = rows
    assert v1.is_active is False
    assert v2.is_active is True
    assert v2.uploaded_by == user["id"]
    assert v2.content_hash == hashlib.sha256(content).hexdigest()
    assert v2.note == "Neu"


@pytest.mark.route
def test_upload_anonymous_is_unauthorized(client):
    resp = client.post("/api/templates/upload", data={}, content_type="multipart/form-data",
                       follow_redirects=False)

    assert resp.status_code in (302, 401)


# ==========================================================================
# POST /api/templates/<id>/activate
# ==========================================================================

@pytest.mark.route
def test_activate_unknown_id_returns_404(auth_client):
    resp = auth_client.post("/api/templates/999999/activate")

    assert resp.status_code == 404
    assert resp.get_json()["error"] == "Version nicht gefunden"


@pytest.mark.route
def test_activate_success_flips_previous_active_off(auth_client, db_session):
    from vms.domain.models import DocumentTemplate

    v2 = template_store.store_new_version("rechnung", "v2.odt", b"v2")
    v3 = template_store.store_new_version("rechnung", "v3.odt", b"v3")  # currently active

    resp = auth_client.post(f"/api/templates/{v2['id']}/activate")

    assert resp.status_code == 200
    assert resp.get_json()["template"]["is_active"] is True

    db_session.expire_all()
    actives = (db_session.query(DocumentTemplate)
               .filter_by(template_type="rechnung", is_active=True).all())
    assert [r.id for r in actives] == [v2["id"]]


@pytest.mark.route
def test_activate_anonymous_is_unauthorized(client):
    resp = client.post("/api/templates/1/activate", follow_redirects=False)

    assert resp.status_code in (302, 401)


# ==========================================================================
# GET /api/templates/<id>/download
# ==========================================================================

@pytest.mark.route
def test_download_unknown_id_returns_404(auth_client):
    resp = auth_client.get("/api/templates/999999/download")

    assert resp.status_code == 404
    assert resp.get_json()["error"] == "Version nicht gefunden"


@pytest.mark.route
def test_download_success_returns_byte_identical_content(auth_client, db_session):
    payload = _bundled_bytes("leihvertrag")
    stored = template_store.store_new_version("leihvertrag", "vertrag.odt", payload)

    resp = auth_client.get(f"/api/templates/{stored['id']}/download")

    assert resp.status_code == 200
    assert resp.mimetype == "application/vnd.oasis.opendocument.text"
    assert "vertrag.odt" in resp.headers["Content-Disposition"]
    assert resp.data == payload
    assert hashlib.sha256(resp.data).hexdigest() == hashlib.sha256(payload).hexdigest()


@pytest.mark.route
def test_download_anonymous_is_unauthorized(client):
    resp = client.get("/api/templates/1/download", follow_redirects=False)

    assert resp.status_code in (302, 401)


# ==========================================================================
# DELETE /api/templates/<id>
# ==========================================================================

@pytest.mark.route
def test_delete_active_version_returns_400(auth_client, db_session):
    stored = template_store.store_new_version("rechnung", "v.odt", b"x")

    resp = auth_client.delete(f"/api/templates/{stored['id']}")

    assert resp.status_code == 400
    assert "aktive Version" in resp.get_json()["error"]


@pytest.mark.route
def test_delete_unknown_id_returns_400(auth_client):
    resp = auth_client.delete("/api/templates/999999")

    assert resp.status_code == 400
    assert resp.get_json()["error"] == "Version nicht gefunden"


@pytest.mark.route
def test_delete_inactive_version_succeeds_and_removes_row(auth_client, db_session):
    from vms.domain.models import DocumentTemplate

    v2 = template_store.store_new_version("rechnung", "v2.odt", b"v2")
    template_store.store_new_version("rechnung", "v3.odt", b"v3")  # v2 now inactive

    resp = auth_client.delete(f"/api/templates/{v2['id']}")

    assert resp.status_code == 200
    assert resp.get_json()["success"] is True
    db_session.expire_all()
    assert db_session.query(DocumentTemplate).filter_by(id=v2["id"]).first() is None


@pytest.mark.route
def test_delete_anonymous_is_unauthorized(client):
    resp = client.delete("/api/templates/1", follow_redirects=False)

    assert resp.status_code in (302, 401)


# ==========================================================================
# GET /templates (page)
# ==========================================================================

@pytest.mark.route
def test_templates_page_renders_for_authenticated_user(auth_client):
    resp = auth_client.get("/templates")

    assert resp.status_code == 200


@pytest.mark.route
def test_templates_page_anonymous_redirects_to_login(client):
    resp = client.get("/templates", follow_redirects=False)

    assert resp.status_code in (301, 302)
    assert "/login" in resp.headers["Location"]

"""
Tests für den Secret-Schreibpfad in settings_routes.py.

Fokus: eine fehlgeschlagene Verschlüsselung darf niemals als Erfolg gemeldet
werden. Vorher gab encrypt_value bei jedem Fehler None zurück — der Create-Pfad
schrieb das None in die Spalte, der Update-Pfad übersprang die Zuweisung, und
beide antworteten mit {'success': True}.
"""
import io

import bcrypt
import pytest

from vms.infra.security import EncryptionError

# Ein winziges, aber gültiges PNG (1x1 Pixel, RGBA) -- reicht als Binärinhalt
# für den Signature-Upload-Roundtrip, ohne dass es tatsächlich dekodiert wird.
PNG_BYTES = (
    b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
    b'\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01'
    b'\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82'
)


def _write_master_key(tmp_path, monkeypatch, content=b"k" * 32):
    """Point KMS at a deterministic key file so encrypt_binary/decrypt_binary work.

    _kms_isolated (conftest) removes KMS_MASTER_KEY_PATH and clears the kms
    cache before each test; signature routes go through kms.encrypt_binary /
    kms.decrypt_binary which need a real key file to load.
    """
    import vms.infra.kms as kms
    path = tmp_path / "master.key"
    path.write_bytes(content)
    monkeypatch.setenv("KMS_MASTER_KEY_PATH", str(path))
    kms.clear_cache()

EMAIL_PAYLOAD = {
    "email_address": "tester@example.com",
    "imap_server": "imap.example.com",
    "imap_port": 993,
    "imap_password": "imap-geheim",
    "smtp_server": "smtp.example.com",
    "smtp_port": 587,
    "smtp_password": "smtp-geheim",
}


def _settings_row(db_session):
    from vms.domain.models import UserSettings
    return db_session.query(UserSettings).first()


@pytest.mark.route
def test_email_settings_create_stores_ciphertext_not_plaintext(app, auth_client, db_session):
    resp = auth_client.post("/api/settings/email/manual", json=EMAIL_PAYLOAD)

    assert resp.status_code == 200
    row = _settings_row(db_session)
    assert row.encrypted_smtp_password not in (None, "smtp-geheim")
    assert row.encrypted_imap_password not in (None, "imap-geheim")

    from vms.infra.security import decrypt_value
    with app.app_context():
        assert decrypt_value(row.encrypted_smtp_password) == "smtp-geheim"
        assert decrypt_value(row.encrypted_imap_password) == "imap-geheim"


@pytest.mark.route
def test_email_settings_not_saved_when_encryption_fails(app, auth_client, db_session, mocker):
    """Kein stiller Erfolg: Fehler melden und gar nichts schreiben."""
    mocker.patch(
        "vms.routes.settings.encrypt_value",
        side_effect=EncryptionError("Master-Key unbrauchbar"),
    )

    resp = auth_client.post("/api/settings/email/manual", json=EMAIL_PAYLOAD)

    assert resp.status_code == 500
    assert "error" in resp.get_json()
    assert _settings_row(db_session) is None


@pytest.mark.route
def test_kanboard_settings_not_saved_when_encryption_fails(app, auth_client, db_session, mocker):
    mocker.patch(
        "vms.routes.settings.encrypt_value",
        side_effect=EncryptionError("Master-Key unbrauchbar"),
    )

    resp = auth_client.post(
        "/api/settings/kanboard",
        json={"kanboard_url": "https://kb.example.com/jsonrpc.php",
              "kanboard_user": "tester",
              "kanboard_token": "tok-geheim"},
    )

    assert resp.status_code == 500
    assert _settings_row(db_session) is None

# --------------------------------------------------------------------------
# Kanboard-Projekt-ID: eine unlesbare Eingabe wurde früher still durch den
# hartcodierten Default 25 ersetzt -- der Nutzer landete unbemerkt in einem
# fremden Projekt (Block F der Graph-Analyse).
# --------------------------------------------------------------------------

@pytest.mark.route
@pytest.mark.parametrize("bad_id", ["abc", "12x", "3.5", "-1", "0"])
def test_kanboard_rejects_unusable_project_id(app, auth_client, db_session, bad_id):
    resp = auth_client.post(
        "/api/settings/kanboard",
        json={"kanboard_url": "https://kb.example.com/jsonrpc.php",
              "kanboard_user": "tester",
              "kanboard_token": "tok-geheim",
              "kanboard_project_id": bad_id},
    )

    assert resp.status_code == 400
    assert "Projekt-ID" in resp.get_json()["error"]
    assert _settings_row(db_session) is None


@pytest.mark.route
def test_kanboard_accepts_a_numeric_project_id(app, auth_client, db_session):
    resp = auth_client.post(
        "/api/settings/kanboard",
        json={"kanboard_url": "https://kb.example.com/jsonrpc.php",
              "kanboard_user": "tester",
              "kanboard_token": "tok-geheim",
              "kanboard_project_id": "42"},
    )

    assert resp.status_code == 200
    assert _settings_row(db_session).kanboard_project_id == 42


@pytest.mark.route
def test_kanboard_falls_back_to_default_project_id_when_omitted(app, auth_client, db_session):
    """Weggelassen ist weiterhin erlaubt und nutzt den Default -- bewusst nicht
    mitgeändert, um bestehende Installationen nicht zu brechen."""
    from vms.routes.settings import DEFAULT_KANBOARD_PROJECT_ID

    resp = auth_client.post(
        "/api/settings/kanboard",
        json={"kanboard_url": "https://kb.example.com/jsonrpc.php",
              "kanboard_user": "tester",
              "kanboard_token": "tok-geheim"},
    )

    assert resp.status_code == 200
    assert _settings_row(db_session).kanboard_project_id == DEFAULT_KANBOARD_PROJECT_ID


# --------------------------------------------------------------------------
# GET /api/settings: unconfigured-Default vs. bestehende Zeile.
# --------------------------------------------------------------------------

@pytest.mark.route
def test_get_settings_returns_unconfigured_default_when_no_row(app, auth_client, db_session):
    resp = auth_client.get("/api/settings")

    assert resp.status_code == 200
    assert resp.get_json() == {"email_provider": "unconfigured", "kanboard_configured": False}


@pytest.mark.route
def test_get_settings_returns_row_dict_when_configured(app, auth_client, db_session):
    auth_client.post(
        "/api/settings/kanboard",
        json={"kanboard_url": "https://kb.example.com/jsonrpc.php",
              "kanboard_user": "tester",
              "kanboard_token": "tok-geheim"},
    )

    resp = auth_client.get("/api/settings")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["kanboard_configured"] is True
    assert body["kanboard_url"] == "https://kb.example.com/jsonrpc.php"
    assert "encrypted_kanboard_token" not in body  # to_dict() darf kein Chiffrat leaken


# --------------------------------------------------------------------------
# Kanboard: Verschlüsselungs-Roundtrip und Validierung der übrigen Felder
# --------------------------------------------------------------------------

@pytest.mark.route
def test_kanboard_settings_create_stores_ciphertext_and_roundtrips(app, auth_client, db_session):
    resp = auth_client.post(
        "/api/settings/kanboard",
        json={"kanboard_url": "https://kb.example.com/jsonrpc.php",
              "kanboard_user": "tester",
              "kanboard_token": "tok-geheim",
              "kanboard_project_id": "7"},
    )

    assert resp.status_code == 200
    row = _settings_row(db_session)
    assert row.kanboard_url == "https://kb.example.com/jsonrpc.php"
    assert row.kanboard_user == "tester"
    assert row.kanboard_project_id == 7
    assert row.encrypted_kanboard_token not in (None, "tok-geheim")

    from vms.infra.security import decrypt_value
    with app.app_context():
        assert decrypt_value(row.encrypted_kanboard_token) == "tok-geheim"


@pytest.mark.route
def test_kanboard_settings_create_missing_url_returns_400(app, auth_client, db_session):
    resp = auth_client.post(
        "/api/settings/kanboard",
        json={"kanboard_user": "tester", "kanboard_token": "tok-geheim"},
    )

    assert resp.status_code == 400
    assert "URL" in resp.get_json()["error"]
    assert _settings_row(db_session) is None


@pytest.mark.route
def test_kanboard_settings_create_missing_user_returns_400(app, auth_client, db_session):
    resp = auth_client.post(
        "/api/settings/kanboard",
        json={"kanboard_url": "https://kb.example.com/jsonrpc.php",
              "kanboard_token": "tok-geheim"},
    )

    assert resp.status_code == 400
    assert _settings_row(db_session) is None


@pytest.mark.route
def test_kanboard_settings_create_missing_token_returns_400(app, auth_client, db_session):
    resp = auth_client.post(
        "/api/settings/kanboard",
        json={"kanboard_url": "https://kb.example.com/jsonrpc.php",
              "kanboard_user": "tester"},
    )

    assert resp.status_code == 400
    assert "Token" in resp.get_json()["error"]
    assert _settings_row(db_session) is None


@pytest.mark.route
def test_kanboard_settings_update_keeps_old_token_when_omitted(app, auth_client, db_session):
    """Update-Pfad: kein neues Token in der Anfrage -> altes Chiffrat bleibt stehen."""
    from vms.infra.security import decrypt_value

    auth_client.post(
        "/api/settings/kanboard",
        json={"kanboard_url": "https://kb.example.com/jsonrpc.php",
              "kanboard_user": "tester",
              "kanboard_token": "erstes-token"},
    )
    old_ciphertext = _settings_row(db_session).encrypted_kanboard_token

    resp = auth_client.post(
        "/api/settings/kanboard",
        json={"kanboard_url": "https://kb2.example.com/jsonrpc.php",
              "kanboard_user": "tester2"},
    )

    assert resp.status_code == 200
    row = _settings_row(db_session)
    assert row.kanboard_url == "https://kb2.example.com/jsonrpc.php"
    assert row.kanboard_user == "tester2"
    assert row.encrypted_kanboard_token == old_ciphertext
    with app.app_context():
        assert decrypt_value(row.encrypted_kanboard_token) == "erstes-token"


@pytest.mark.route
def test_kanboard_settings_update_replaces_token_when_given(app, auth_client, db_session):
    from vms.infra.security import decrypt_value

    auth_client.post(
        "/api/settings/kanboard",
        json={"kanboard_url": "https://kb.example.com/jsonrpc.php",
              "kanboard_user": "tester",
              "kanboard_token": "erstes-token"},
    )
    old_ciphertext = _settings_row(db_session).encrypted_kanboard_token

    resp = auth_client.post(
        "/api/settings/kanboard",
        json={"kanboard_url": "https://kb.example.com/jsonrpc.php",
              "kanboard_user": "tester",
              "kanboard_token": "zweites-token"},
    )

    assert resp.status_code == 200
    row = _settings_row(db_session)
    assert row.encrypted_kanboard_token != old_ciphertext
    with app.app_context():
        assert decrypt_value(row.encrypted_kanboard_token) == "zweites-token"


# --------------------------------------------------------------------------
# E-Mail: Update-Pfad (leeres Passwort behält Chiffrat, neues ersetzt es) und
# fehlende Pflichtfelder.
# --------------------------------------------------------------------------

@pytest.mark.route
def test_email_settings_update_keeps_old_ciphertext_when_password_blank(app, auth_client, db_session):
    from vms.infra.security import decrypt_value

    auth_client.post("/api/settings/email/manual", json=EMAIL_PAYLOAD)
    old_smtp_cipher = _settings_row(db_session).encrypted_smtp_password
    old_imap_cipher = _settings_row(db_session).encrypted_imap_password

    update_payload = dict(EMAIL_PAYLOAD)
    update_payload["imap_password"] = ""
    update_payload["smtp_password"] = ""
    update_payload["email_address"] = "changed@example.com"

    resp = auth_client.post("/api/settings/email/manual", json=update_payload)

    assert resp.status_code == 200
    row = _settings_row(db_session)
    assert row.email_address == "changed@example.com"
    assert row.encrypted_smtp_password == old_smtp_cipher
    assert row.encrypted_imap_password == old_imap_cipher
    with app.app_context():
        assert decrypt_value(row.encrypted_smtp_password) == "smtp-geheim"
        assert decrypt_value(row.encrypted_imap_password) == "imap-geheim"


@pytest.mark.route
def test_email_settings_update_replaces_ciphertext_when_password_given(app, auth_client, db_session):
    from vms.infra.security import decrypt_value

    auth_client.post("/api/settings/email/manual", json=EMAIL_PAYLOAD)
    old_smtp_cipher = _settings_row(db_session).encrypted_smtp_password

    update_payload = dict(EMAIL_PAYLOAD)
    update_payload["smtp_password"] = "neues-smtp-geheimnis"

    resp = auth_client.post("/api/settings/email/manual", json=update_payload)

    assert resp.status_code == 200
    row = _settings_row(db_session)
    assert row.encrypted_smtp_password != old_smtp_cipher
    with app.app_context():
        assert decrypt_value(row.encrypted_smtp_password) == "neues-smtp-geheimnis"


@pytest.mark.route
def test_email_settings_missing_required_field_returns_400(app, auth_client, db_session):
    payload = dict(EMAIL_PAYLOAD)
    del payload["smtp_server"]

    resp = auth_client.post("/api/settings/email/manual", json=payload)

    assert resp.status_code == 400
    assert "error" in resp.get_json()
    assert _settings_row(db_session) is None


@pytest.mark.route
def test_email_settings_create_missing_password_returns_400(app, auth_client, db_session):
    payload = dict(EMAIL_PAYLOAD)
    payload["smtp_password"] = ""

    resp = auth_client.post("/api/settings/email/manual", json=payload)

    assert resp.status_code == 400
    assert _settings_row(db_session) is None


# --------------------------------------------------------------------------
# settings_page: Profil- und Passwort-Formulare
# --------------------------------------------------------------------------

@pytest.mark.route
def test_settings_page_get_renders_with_existing_row(app, auth_client, db_session):
    from vms.domain.models import UserSettings
    row = UserSettings(user_id=1, kanboard_url="https://kb.example.com", kanboard_user="tester",
                        encrypted_kanboard_token=b"irrelevant-for-get".hex())
    db_session.add(row)
    db_session.commit()

    resp = auth_client.get("/settings")

    assert resp.status_code == 200
    assert b"kb.example.com" in resp.data
    assert b"Gespeichert" in resp.data  # has_token -> placeholder statt leerem Feld


@pytest.mark.route
def test_settings_page_get_renders_with_no_row(app, auth_client, db_session):
    resp = auth_client.get("/settings")

    assert resp.status_code == 200
    assert _settings_row(db_session) is None
    assert b"<form" in resp.data


@pytest.mark.route
def test_settings_page_profile_duplicate_email_shows_flash_without_redirect(app, auth_client, db_session, user):
    from vms.auth import User

    other = User.create(username="other", password="Sup3r-Secret!",
                         display_name="Other User", email="other@example.com", is_active=True)
    assert other is not None

    resp = auth_client.post("/settings", data={
        "update_profile": "1",
        "display_name": "Test User",
        "email": "other@example.com",
    })

    assert resp.status_code == 200
    assert "E-Mail bereits verwendet".encode("utf-8") in resp.data

    from vms.domain.models import User as UserModel
    current = db_session.query(UserModel).filter_by(id=user["id"]).first()
    assert current.email == "tester@example.com"


@pytest.mark.route
def test_settings_page_profile_new_unique_email_redirects_and_updates(app, auth_client, db_session, user):
    """E-Mail geändert, kein Konflikt: der else-Zweig (Zeilen 192-196) muss die
    Zeile aktualisieren und redirecten, nicht nur den Dup-Check überspringen."""
    resp = auth_client.post("/settings", data={
        "update_profile": "1",
        "display_name": "Test User",
        "email": "new-unique@example.com",
    })

    assert resp.status_code == 302

    from vms.domain.models import User as UserModel
    current = db_session.query(UserModel).filter_by(id=user["id"]).first()
    assert current.email == "new-unique@example.com"


@pytest.mark.route
def test_settings_page_profile_name_only_change_redirects(app, auth_client, db_session, user):
    resp = auth_client.post("/settings", data={
        "update_profile": "1",
        "display_name": "Neuer Name",
        "email": "tester@example.com",
    })

    assert resp.status_code == 302

    from vms.domain.models import User as UserModel
    current = db_session.query(UserModel).filter_by(id=user["id"]).first()
    assert current.display_name == "Neuer Name"


@pytest.mark.route
def test_settings_page_password_wrong_current_password_no_redirect(app, auth_client, db_session, user):
    resp = auth_client.post("/settings", data={
        "change_password": "1",
        "current_password": "totally-wrong",
        "new_password": "NeuesPassw0rt",
        "confirm_password": "NeuesPassw0rt",
    })

    assert resp.status_code == 200
    assert "Aktuelles Passwort falsch".encode("utf-8") in resp.data

    from vms.domain.models import User as UserModel
    current = db_session.query(UserModel).filter_by(id=user["id"]).first()
    assert bcrypt.checkpw(user["password"].encode("utf-8"), current.password_hash.encode("utf-8"))


@pytest.mark.route
def test_settings_page_password_correct_changes_hash_and_redirects(app, auth_client, db_session, user):
    resp = auth_client.post("/settings", data={
        "change_password": "1",
        "current_password": user["password"],
        "new_password": "NeuesPassw0rt",
        "confirm_password": "NeuesPassw0rt",
    })

    assert resp.status_code == 302

    from vms.domain.models import User as UserModel
    current = db_session.query(UserModel).filter_by(id=user["id"]).first()
    assert bcrypt.checkpw(b"NeuesPassw0rt", current.password_hash.encode("utf-8"))
    assert not bcrypt.checkpw(user["password"].encode("utf-8"), current.password_hash.encode("utf-8"))


# --------------------------------------------------------------------------
# Signatur: Upload-Roundtrip, Validierung, Delete, Preview, Base64-API.
# --------------------------------------------------------------------------

@pytest.mark.route
def test_upload_signature_roundtrips_through_kms_decrypt_binary(app, auth_client, db_session, tmp_path, monkeypatch):
    import vms.infra.kms as kms
    _write_master_key(tmp_path, monkeypatch)

    resp = auth_client.post(
        "/settings/signature",
        data={"signature": (io.BytesIO(PNG_BYTES), "signature.png")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert resp.status_code == 200
    assert "hochgeladen".encode("utf-8") in resp.data

    row = _settings_row(db_session)
    assert row.encrypted_signature not in (None, PNG_BYTES)
    assert kms.decrypt_binary(row.encrypted_signature) == PNG_BYTES


@pytest.mark.route
def test_upload_signature_updates_existing_row_without_creating_new(app, auth_client, db_session, tmp_path, monkeypatch):
    """Zeile existiert bereits (z. B. mit Kanboard-Config) -> `if not settings`
    ist False, es wird kein zweiter UserSettings-Datensatz angelegt."""
    import vms.infra.kms as kms
    _write_master_key(tmp_path, monkeypatch)

    from vms.domain.models import UserSettings
    row = UserSettings(user_id=1, kanboard_url="https://kb.example.com", kanboard_user="tester")
    db_session.add(row)
    db_session.commit()

    resp = auth_client.post(
        "/settings/signature",
        data={"signature": (io.BytesIO(PNG_BYTES), "signature.png")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert resp.status_code == 200
    rows = db_session.query(UserSettings).all()
    assert len(rows) == 1
    assert rows[0].kanboard_url == "https://kb.example.com"  # unangetastet
    assert kms.decrypt_binary(rows[0].encrypted_signature) == PNG_BYTES


@pytest.mark.route
def test_upload_signature_encryption_failure_flashes_error_and_writes_nothing(app, auth_client, db_session):
    """Kein Master-Key konfiguriert (Standardzustand von _kms_isolated) ->
    encrypt_binary wirft FileNotFoundError -> except-Zweig, kein stiller
    Erfolg, keine Zeile geschrieben."""
    resp = auth_client.post(
        "/settings/signature",
        data={"signature": (io.BytesIO(PNG_BYTES), "signature.png")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert resp.status_code == 200
    assert "Fehler beim Speichern".encode("utf-8") in resp.data
    assert _settings_row(db_session) is None


@pytest.mark.route
def test_upload_signature_rejects_non_png_and_writes_nothing(app, auth_client, db_session, tmp_path, monkeypatch):
    _write_master_key(tmp_path, monkeypatch)

    resp = auth_client.post(
        "/settings/signature",
        data={"signature": (io.BytesIO(b"not a png"), "signature.txt")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert resp.status_code == 200
    assert "Nur PNG-Dateien erlaubt".encode("utf-8") in resp.data
    assert _settings_row(db_session) is None


@pytest.mark.route
def test_upload_signature_no_file_part_flashes_and_writes_nothing(app, auth_client, db_session):
    resp = auth_client.post(
        "/settings/signature",
        data={},
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert resp.status_code == 200
    assert "Keine Datei ausgewählt".encode("utf-8") in resp.data
    assert _settings_row(db_session) is None


@pytest.mark.route
def test_upload_signature_empty_filename_flashes_and_writes_nothing(app, auth_client, db_session):
    resp = auth_client.post(
        "/settings/signature",
        data={"signature": (io.BytesIO(PNG_BYTES), "")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )

    assert resp.status_code == 200
    assert "Keine Datei ausgewählt".encode("utf-8") in resp.data
    assert _settings_row(db_session) is None


@pytest.mark.route
def test_delete_signature_found_nulls_column(app, auth_client, db_session, tmp_path, monkeypatch):
    import vms.infra.kms as kms
    _write_master_key(tmp_path, monkeypatch)

    from vms.domain.models import UserSettings
    row = UserSettings(user_id=1, encrypted_signature=kms.encrypt_binary(PNG_BYTES))
    db_session.add(row)
    db_session.commit()

    resp = auth_client.delete("/api/signature")

    assert resp.status_code == 200
    assert resp.get_json() == {"success": True}
    assert _settings_row(db_session).encrypted_signature is None


@pytest.mark.route
def test_delete_signature_not_found_returns_404(app, auth_client, db_session):
    resp = auth_client.delete("/api/signature")

    assert resp.status_code == 404
    assert "error" in resp.get_json()


@pytest.mark.route
def test_preview_signature_returns_png_bytes(app, auth_client, db_session, tmp_path, monkeypatch):
    import vms.infra.kms as kms
    _write_master_key(tmp_path, monkeypatch)

    from vms.domain.models import UserSettings
    row = UserSettings(user_id=1, encrypted_signature=kms.encrypt_binary(PNG_BYTES))
    db_session.add(row)
    db_session.commit()

    resp = auth_client.get("/api/signature/preview")

    assert resp.status_code == 200
    assert resp.data == PNG_BYTES
    assert resp.mimetype == "image/png"


@pytest.mark.route
def test_preview_signature_decrypt_error_returns_500(app, auth_client, db_session, tmp_path, monkeypatch):
    """Chiffrat, das mit einem anderen Key erzeugt wurde, kann nicht entschlüsselt
    werden -- muss als 500 mit Fehlermeldung beantwortet werden, nicht als
    stiller Erfolg."""
    import vms.infra.kms as kms
    _write_master_key(tmp_path, monkeypatch, content=b"a" * 32)
    garbage_ciphertext = kms.encrypt_binary(PNG_BYTES)

    # Key rotieren, ohne das gespeicherte Chiffrat neu zu verschlüsseln.
    _write_master_key(tmp_path, monkeypatch, content=b"b" * 32)

    from vms.domain.models import UserSettings
    row = UserSettings(user_id=1, encrypted_signature=garbage_ciphertext)
    db_session.add(row)
    db_session.commit()

    resp = auth_client.get("/api/signature/preview")

    assert resp.status_code == 500
    body = resp.get_json()
    assert "error" in body
    # Die Entschlüsselungs-Exception (hier Fernets InvalidToken) wird
    # serverseitig geloggt; der Client bekommt nur eine generische Meldung
    # (kein roher Exception-String) -- siehe S1-Fix in docs/FINDINGS.md.


@pytest.mark.route
def test_preview_signature_does_not_leak_internal_file_path_on_config_error(app, auth_client, db_session, monkeypatch):
    import vms.infra.kms as kms
    monkeypatch.delenv("KMS_MASTER_KEY_PATH", raising=False)
    kms.clear_cache()

    from vms.domain.models import UserSettings
    row = UserSettings(user_id=1, encrypted_signature=b"not-a-real-fernet-token")
    db_session.add(row)
    db_session.commit()

    resp = auth_client.get("/api/signature/preview")

    assert resp.status_code == 500
    error_message = resp.get_json().get("error", "")
    assert "/etc/vms/master.key" not in error_message, (
        "internal server file path must not be exposed to the client"
    )


@pytest.mark.route
def test_preview_signature_404_when_none(app, auth_client, db_session):
    resp = auth_client.get("/api/signature/preview")

    assert resp.status_code == 404
    assert "error" in resp.get_json()


@pytest.mark.route
def test_get_signature_api_returns_base64_data_uri(app, auth_client, db_session, tmp_path, monkeypatch):
    import base64
    import vms.infra.kms as kms
    _write_master_key(tmp_path, monkeypatch)

    from vms.domain.models import UserSettings
    row = UserSettings(user_id=1, encrypted_signature=kms.encrypt_binary(PNG_BYTES))
    db_session.add(row)
    db_session.commit()

    resp = auth_client.get("/api/signature")

    assert resp.status_code == 200
    body = resp.get_json()
    expected_prefix = "data:image/png;base64,"
    assert body["signature"].startswith(expected_prefix)
    decoded = base64.b64decode(body["signature"][len(expected_prefix):])
    assert decoded == PNG_BYTES


@pytest.mark.route
def test_get_signature_api_decrypt_error_returns_500(app, auth_client, db_session, tmp_path, monkeypatch):
    import vms.infra.kms as kms
    _write_master_key(tmp_path, monkeypatch, content=b"a" * 32)
    garbage_ciphertext = kms.encrypt_binary(PNG_BYTES)
    _write_master_key(tmp_path, monkeypatch, content=b"b" * 32)

    from vms.domain.models import UserSettings
    row = UserSettings(user_id=1, encrypted_signature=garbage_ciphertext)
    db_session.add(row)
    db_session.commit()

    resp = auth_client.get("/api/signature")

    assert resp.status_code == 500
    assert "error" in resp.get_json()


@pytest.mark.route
def test_get_signature_api_returns_none_when_no_row(app, auth_client, db_session):
    resp = auth_client.get("/api/signature")

    assert resp.status_code == 200
    assert resp.get_json() == {"signature": None}


# --------------------------------------------------------------------------
# Auth-Grenze: alle acht Routen ohne Session -> Redirect zum Login, keine
# Seiteneffekte.
# --------------------------------------------------------------------------

@pytest.mark.route
@pytest.mark.parametrize("method,path", [
    ("get", "/api/settings"),
    ("post", "/api/settings/email/manual"),
    ("post", "/api/settings/kanboard"),
    ("get", "/settings"),
    ("post", "/settings/signature"),
    ("delete", "/api/signature"),
    ("get", "/api/signature/preview"),
    ("get", "/api/signature"),
])
def test_anonymous_client_is_redirected_for_every_settings_route(app, client, db_session, method, path):
    resp = getattr(client, method)(path)

    assert resp.status_code in (302, 401)
    assert _settings_row(db_session) is None

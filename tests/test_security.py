"""
Tests für security.py — encrypt_value/decrypt_value und die Schlüsselableitung.

security.py hat zwei Ableitungszweige:
  * KMS-Zweig      — Salt b'vms_kms_v1',      200_000 Iterationen
  * SECRET_KEY-Zweig — Salt b'vms_static_salt', 100_000 Iterationen

Sie erzeugen *verschiedene* Fernet-Keys. Damit ein Kippen von is_kms_available()
bestehende Chiffrate nicht unlesbar macht, trägt jedes neue Chiffrat ein Versionstag
(k1:/s1:), das den erzeugenden Zweig markiert; decrypt_value wählt danach den
richtigen Zweig. Der Standardzustand in Tests ist "KMS nicht verfügbar"
(autouse-Fixture _kms_isolated in conftest.py) -> SECRET_KEY. Wer den KMS-Zweig
braucht, ruft _enable_kms().

Alle Aufrufe brauchen app_ctx: die Ableitung liest current_app.config['SECRET_KEY'].
"""
import pytest

from vms.infra.security import DecryptionError, EncryptionError, decrypt_value, encrypt_value

KEY = b"k" * 32


def _enable_kms(monkeypatch, tmp_path, key=KEY):
    """Schaltet _get_fernet auf den KMS-Zweig.

    is_kms_available() prüft nur die *Existenz* beider Dateien, der Inhalt von
    secrets.enc ist dafür egal — deshalb reicht hier ein Platzhalter.
    """
    import vms.infra.kms as kms

    key_path = tmp_path / "master.key"
    key_path.write_bytes(key)
    secrets_path = tmp_path / "secrets.enc"
    secrets_path.write_text("platzhalter")

    monkeypatch.setenv("KMS_MASTER_KEY_PATH", str(key_path))
    monkeypatch.setattr(kms, "DEFAULT_SECRETS_PATH", str(secrets_path))
    kms.clear_cache()


# --- Round-Trips über beide Zweige --------------------------------------------

@pytest.mark.unit
def test_roundtrip_via_secret_key_branch(app_ctx):
    token = encrypt_value("smtp-passwort")

    assert "smtp-passwort" not in token
    assert decrypt_value(token) == "smtp-passwort"


@pytest.mark.unit
def test_roundtrip_via_kms_branch(app_ctx, tmp_path, monkeypatch):
    _enable_kms(monkeypatch, tmp_path)

    token = encrypt_value("smtp-passwort")

    assert "smtp-passwort" not in token
    assert decrypt_value(token) == "smtp-passwort"


@pytest.mark.unit
def test_encrypt_value_is_non_deterministic(app_ctx):
    """Fernet nutzt pro Aufruf eine neue IV — gleicher Klartext, andere Tokens."""
    a = encrypt_value("smtp-passwort")
    b = encrypt_value("smtp-passwort")

    assert a != b
    assert decrypt_value(a) == decrypt_value(b) == "smtp-passwort"


@pytest.mark.unit
def test_tagged_ciphertext_survives_kms_becoming_available(app_ctx, tmp_path, monkeypatch):
    """Das Versionstag löst die frühere KDF-Divergenz (s. docs/FINDINGS.md #1).

    Ein im SECRET_KEY-Zweig verschlüsselter Wert bleibt lesbar, auch wenn KMS
    später verfügbar wird — das Tag verweist decrypt_value auf den SECRET_KEY-Zweig,
    statt blind den nun aktiven KMS-Key zu probieren. Genau der Betriebsfall, der
    vorher alle Chiffrate unlesbar machte.
    """
    token = encrypt_value("smtp-passwort")          # SECRET_KEY-Zweig, Tag s1:
    assert token.startswith("s1:")

    _enable_kms(monkeypatch, tmp_path)              # KMS ab hier verfügbar

    assert decrypt_value(token) == "smtp-passwort"


@pytest.mark.unit
def test_kms_tagged_ciphertext_is_unreadable_once_master_key_is_gone(app_ctx, tmp_path, monkeypatch):
    """Echter Datenverlust-Fall bleibt ein sichtbarer Fehler.

    Ein KMS-getaggtes Chiffrat kann ohne den Master-Key nicht gelesen werden — und
    darf NICHT still auf den SECRET_KEY-Zweig ausweichen (das würde Müll liefern).
    Das Tag zwingt den KMS-Zweig; fehlt er, gibt es einen DecryptionError.
    """
    _enable_kms(monkeypatch, tmp_path)
    token = encrypt_value("smtp-passwort")          # KMS-Zweig, Tag k1:
    assert token.startswith("k1:")

    monkeypatch.setenv("KMS_MASTER_KEY_PATH", str(tmp_path / "weg.key"))  # Key verschwindet
    import vms.infra.kms as kms
    kms.clear_cache()

    with pytest.raises(DecryptionError):
        decrypt_value(token)


# --- Leerwerte ----------------------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize("value", ["", None])
def test_encrypt_value_falsy_input_returns_none(app_ctx, value):
    assert encrypt_value(value) is None


@pytest.mark.unit
@pytest.mark.parametrize("token", ["", None])
def test_decrypt_value_falsy_input_returns_none(app_ctx, token):
    """None heißt 'kein Wert gespeichert' — das bleibt bewusst kein Fehler."""
    assert decrypt_value(token) is None


# --- Fehlerpfade: laut statt still --------------------------------------------

@pytest.mark.unit
def test_encrypt_value_raises_when_master_key_too_short(app_ctx, tmp_path, monkeypatch):
    """Zu kurzer Master-Key -> ValueError aus load_master_key.

    _get_fernet fängt nur (ImportError, FileNotFoundError), der ValueError
    propagiert also. Vor dem Fix wurde er zu None und die aufrufende Route hat
    ein leeres Passwort gespeichert und Erfolg gemeldet.
    """
    _enable_kms(monkeypatch, tmp_path, key=b"zu-kurz")

    with pytest.raises(EncryptionError):
        encrypt_value("smtp-passwort")


@pytest.mark.unit
def test_decrypt_value_raises_on_garbage_token(app_ctx):
    with pytest.raises(DecryptionError):
        decrypt_value("kein-gueltiges-fernet-token")


@pytest.mark.unit
def test_decrypt_value_raises_when_encrypted_with_other_secret_key(app_ctx, monkeypatch):
    """SECRET_KEY-Rotation macht bestehende Chiffrate unlesbar — sichtbar, nicht still."""
    token = encrypt_value("smtp-passwort")

    # setitem statt direkter Zuweisung: die app-Fixture ist session-scoped, eine
    # rohe Zuweisung würde in alle folgenden Tests lecken.
    monkeypatch.setitem(app_ctx.config, "SECRET_KEY", "ein-voellig-anderer-secret-key-000000")

    with pytest.raises(DecryptionError):
        decrypt_value(token)


@pytest.mark.unit
def test_encrypt_value_raises_without_secret_key(app_ctx, monkeypatch):
    """Ohne SECRET_KEY und ohne KMS gibt es keinen Schlüssel — das muss scheitern."""
    monkeypatch.setitem(app_ctx.config, "SECRET_KEY", None)

    with pytest.raises(EncryptionError):
        encrypt_value("smtp-passwort")


@pytest.mark.unit
def test_master_key_vanishing_after_availability_check_falls_back(app_ctx, tmp_path, monkeypatch, mocker):
    """TOCTOU: is_kms_available() sagt ja, load_master_key findet die Datei nicht mehr.

    Deckt den FileNotFoundError-Zweig in _get_fernet ab. Real möglich, wenn das
    kms_data-Volume während des Betriebs verschwindet.
    """
    import vms.infra.kms as kms
    _enable_kms(monkeypatch, tmp_path)
    mocker.patch.object(kms, "load_master_key", side_effect=FileNotFoundError("weg"))

    token = encrypt_value("smtp-passwort")

    assert decrypt_value(token) == "smtp-passwort"    # SECRET_KEY-Zweig übernimmt


@pytest.mark.unit
def test_missing_master_key_file_falls_back_to_secret_key(app_ctx, tmp_path, monkeypatch):
    """Master-Key-Pfad gesetzt, Datei fehlt -> is_kms_available() False -> Fallback.

    Der Fallback ist legitim (KMS schlicht nicht eingerichtet) und muss weiter
    funktionieren, statt zu werfen.
    """
    monkeypatch.setenv("KMS_MASTER_KEY_PATH", str(tmp_path / "nicht-da.key"))

    token = encrypt_value("smtp-passwort")

    assert decrypt_value(token) == "smtp-passwort"

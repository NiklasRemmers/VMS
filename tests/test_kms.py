"""
Tests für kms.py — reine Krypto- und Dateilogik, kein App-Kontext, keine DB.

Alle Tests übergeben explizite Pfade (tmp_path) und einen expliziten master_key.
Niemals ohne Pfad arbeiten: DEFAULT_SECRETS_PATH zeigt ins Repo-Verzeichnis,
ein Test ohne Pfad würde die echte secrets.enc überschreiben.

Die autouse-Fixture _kms_isolated (conftest.py) leert die Prozess-Caches
_master_key/_secrets vor und nach jedem Test und entfernt KMS_MASTER_KEY_PATH.
"""
import json
import os
import stat

import pytest
from cryptography.fernet import InvalidToken

KEY = b"k" * 32          # gültiger Master-Key: exakt die Mindestlänge
OTHER_KEY = b"x" * 32


def _write_key(tmp_path, content=KEY, name="master.key"):
    p = tmp_path / name
    p.write_bytes(content)
    return str(p)


# --- load_master_key ----------------------------------------------------------

@pytest.mark.unit
def test_load_master_key_reads_file_and_strips_whitespace(tmp_path):
    path = _write_key(tmp_path, KEY + b"\n  ")

    key = __import__("vms.infra.kms", fromlist=["load_master_key"]).load_master_key(path)

    assert key == KEY


@pytest.mark.unit
def test_load_master_key_missing_file_raises_with_path_in_message(tmp_path):
    import vms.infra.kms as kms
    missing = str(tmp_path / "nicht-da.key")

    with pytest.raises(FileNotFoundError) as exc:
        kms.load_master_key(missing)

    assert missing in str(exc.value)


@pytest.mark.unit
def test_load_master_key_too_short_raises(tmp_path):
    import vms.infra.kms as kms
    path = _write_key(tmp_path, b"zu-kurz")

    with pytest.raises(ValueError, match="too short"):
        kms.load_master_key(path)


@pytest.mark.unit
def test_load_master_key_too_short_does_not_poison_cache(tmp_path):
    """Regression zu FINDINGS #1: ein ungültiger Key darf den Cache nie erreichen.

    Vor dem Fix wurde _master_key *vor* der Längenprüfung gesetzt. Der erste
    Aufruf warf zwar, der zweite traf den Cache-Zweig und lieferte den zu kurzen
    Key kommentarlos zurück.
    """
    import vms.infra.kms as kms
    path = _write_key(tmp_path, b"zu-kurz")

    with pytest.raises(ValueError):
        kms.load_master_key(path)

    assert kms._master_key is None
    with pytest.raises(ValueError):
        kms.load_master_key(path)


@pytest.mark.unit
def test_load_master_key_caches_across_calls(tmp_path):
    import vms.infra.kms as kms
    path = _write_key(tmp_path)
    kms.load_master_key(path)

    os.remove(path)                      # Cache-Treffer darf die Datei nicht mehr brauchen

    assert kms.load_master_key(path) == KEY


@pytest.mark.unit
def test_load_master_key_reads_path_from_environment(tmp_path, monkeypatch):
    import vms.infra.kms as kms
    path = _write_key(tmp_path)
    monkeypatch.setenv("KMS_MASTER_KEY_PATH", path)

    assert kms.load_master_key() == KEY


@pytest.mark.unit
def test_load_master_key_explicit_path_wins_over_environment(tmp_path, monkeypatch):
    import vms.infra.kms as kms
    monkeypatch.setenv("KMS_MASTER_KEY_PATH", _write_key(tmp_path, OTHER_KEY, "env.key"))
    explicit = _write_key(tmp_path, KEY, "explicit.key")

    assert kms.load_master_key(explicit) == KEY


# --- encrypt/decrypt_secret ---------------------------------------------------

@pytest.mark.unit
def test_encrypt_secret_roundtrips_and_hides_plaintext():
    from vms.infra.kms import decrypt_secret, encrypt_secret

    token = encrypt_secret("hunter2", master_key=KEY)

    assert "hunter2" not in token
    assert decrypt_secret(token, master_key=KEY) == "hunter2"


@pytest.mark.unit
def test_encrypt_secret_is_non_deterministic():
    """Fernet nutzt pro Aufruf eine neue IV — gleicher Klartext, andere Tokens."""
    from vms.infra.kms import decrypt_secret, encrypt_secret

    a = encrypt_secret("gleich", master_key=KEY)
    b = encrypt_secret("gleich", master_key=KEY)

    assert a != b
    assert decrypt_secret(a, master_key=KEY) == decrypt_secret(b, master_key=KEY) == "gleich"


@pytest.mark.unit
def test_decrypt_secret_with_wrong_master_key_raises():
    from vms.infra.kms import decrypt_secret, encrypt_secret
    token = encrypt_secret("geheim", master_key=KEY)

    with pytest.raises(InvalidToken):
        decrypt_secret(token, master_key=OTHER_KEY)


@pytest.mark.unit
def test_encrypt_decrypt_secret_uses_default_master_key_when_none_given(tmp_path, monkeypatch):
    """`mk = master_key or load_master_key()`: ohne explizites Argument muss der
    per KMS_MASTER_KEY_PATH konfigurierte Default-Key genutzt werden."""
    import vms.infra.kms as kms
    monkeypatch.setenv("KMS_MASTER_KEY_PATH", _write_key(tmp_path))
    kms.clear_cache()

    token = kms.encrypt_secret("via-default-key")

    kms.clear_cache()
    assert kms.decrypt_secret(token) == "via-default-key"


@pytest.mark.unit
@pytest.mark.parametrize("value", ["", None])
def test_encrypt_secret_falsy_input_returns_empty_string(value):
    """Kein Master-Key nötig: der Kurzschluss greift vor load_master_key()."""
    from vms.infra.kms import encrypt_secret

    assert encrypt_secret(value) == ""


@pytest.mark.unit
@pytest.mark.parametrize("value", ["", None])
def test_decrypt_secret_falsy_input_returns_empty_string(value):
    from vms.infra.kms import decrypt_secret

    assert decrypt_secret(value) == ""


# --- encrypt/decrypt_binary ---------------------------------------------------

@pytest.mark.unit
def test_binary_roundtrips_including_null_bytes():
    from vms.infra.kms import decrypt_binary, encrypt_binary
    payload = b"\x00\x01\xfe\xff ODT-Bytes"

    token = encrypt_binary(payload, master_key=KEY)

    assert token != payload
    assert decrypt_binary(token, master_key=KEY) == payload


@pytest.mark.unit
def test_encrypt_binary_empty_returns_empty_bytes():
    from vms.infra.kms import encrypt_binary

    assert encrypt_binary(b"") == b""


@pytest.mark.unit
def test_decrypt_binary_empty_returns_empty_bytes():
    from vms.infra.kms import decrypt_binary

    assert decrypt_binary(b"") == b""


@pytest.mark.unit
def test_encrypt_binary_uses_default_master_key_when_none_given(tmp_path, monkeypatch):
    """Analog zum String-Pfad: `mk = master_key or load_master_key()` in
    encrypt_binary/decrypt_binary ist ohne explizites Argument nie exerciert."""
    import vms.infra.kms as kms
    monkeypatch.setenv("KMS_MASTER_KEY_PATH", _write_key(tmp_path))
    kms.clear_cache()
    payload = b"\x00\x01\xfe\xff default-key-bytes"

    token = kms.encrypt_binary(payload)

    kms.clear_cache()
    assert kms.decrypt_binary(token) == payload


@pytest.mark.unit
def test_decrypt_binary_with_wrong_master_key_raises():
    from vms.infra.kms import decrypt_binary, encrypt_binary
    payload = b"geheime-bytes"
    token = encrypt_binary(payload, master_key=KEY)

    with pytest.raises(InvalidToken):
        decrypt_binary(token, master_key=OTHER_KEY)


# --- save_secrets / load_secrets ----------------------------------------------

@pytest.mark.unit
def test_save_secrets_writes_ciphertext_that_load_secrets_reads_back(tmp_path):
    import vms.infra.kms as kms
    path = str(tmp_path / "secrets.enc")
    secrets = {"SMTP_PASSWORD": "pw", "KANBOARD_TOKEN": "tok"}

    kms.save_secrets(secrets, path=path, master_key=KEY)
    # Cache leeren, damit wirklich von Platte gelesen und entschlüsselt wird —
    # das ist der Produktivpfad (schreibender Prozess != lesender Prozess).
    kms.clear_cache()

    on_disk = (tmp_path / "secrets.enc").read_text()
    assert "SMTP_PASSWORD" not in on_disk
    assert kms.load_secrets(path=path, master_key=KEY) == secrets


@pytest.mark.unit
def test_save_secrets_restricts_file_permissions_to_owner_only(tmp_path):
    import vms.infra.kms as kms
    path = str(tmp_path / "secrets.enc")

    kms.save_secrets({"a": "b"}, path=path, master_key=KEY)

    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


@pytest.mark.unit
def test_load_secrets_missing_file_returns_empty_dict(tmp_path):
    import vms.infra.kms as kms

    assert kms.load_secrets(path=str(tmp_path / "weg.enc"), master_key=KEY) == {}


@pytest.mark.unit
def test_load_secrets_empty_file_returns_empty_dict(tmp_path):
    import vms.infra.kms as kms
    path = tmp_path / "leer.enc"
    path.write_text("   \n")

    assert kms.load_secrets(path=str(path), master_key=KEY) == {}


@pytest.mark.unit
def test_load_secrets_caches_across_calls(tmp_path):
    import vms.infra.kms as kms
    path = str(tmp_path / "secrets.enc")
    kms.save_secrets({"a": "1"}, path=path, master_key=KEY)
    kms.load_secrets(path=path, master_key=KEY)

    os.remove(path)

    assert kms.load_secrets(path=path, master_key=KEY) == {"a": "1"}


@pytest.mark.unit
def test_load_secrets_with_wrong_master_key_raises(tmp_path):
    import vms.infra.kms as kms
    path = str(tmp_path / "secrets.enc")
    kms.save_secrets({"a": "1"}, path=path, master_key=KEY)
    # save_secrets füllt den Cache; ohne clear_cache() würde load_secrets den
    # Cache-Zweig nehmen und gar nicht erst entschlüsseln.
    kms.clear_cache()

    with pytest.raises(InvalidToken):
        kms.load_secrets(path=path, master_key=OTHER_KEY)


@pytest.mark.unit
def test_save_secrets_invalidates_stale_cache(tmp_path):
    import vms.infra.kms as kms
    path = str(tmp_path / "secrets.enc")
    kms.save_secrets({"SMTP_PASSWORD": "alt"}, path=path, master_key=KEY)
    kms.load_secrets(path=path, master_key=KEY)          # füllt den Cache

    kms.save_secrets({"SMTP_PASSWORD": "neu"}, path=path, master_key=KEY)

    assert kms.load_secrets(path=path, master_key=KEY) == {"SMTP_PASSWORD": "neu"}


# --- get_secret ---------------------------------------------------------------

@pytest.mark.unit
def test_get_secret_returns_stored_value(tmp_path, monkeypatch):
    import vms.infra.kms as kms
    path = str(tmp_path / "secrets.enc")
    monkeypatch.setenv("KMS_MASTER_KEY_PATH", _write_key(tmp_path))
    kms.save_secrets({"SMTP_PASSWORD": "pw"}, path=path, master_key=KEY)
    monkeypatch.setattr(kms, "DEFAULT_SECRETS_PATH", path)

    assert kms.get_secret("SMTP_PASSWORD") == "pw"


@pytest.mark.unit
def test_get_secret_unknown_key_returns_default(tmp_path, monkeypatch):
    import vms.infra.kms as kms
    path = str(tmp_path / "secrets.enc")
    monkeypatch.setenv("KMS_MASTER_KEY_PATH", _write_key(tmp_path))
    kms.save_secrets({"a": "1"}, path=path, master_key=KEY)
    monkeypatch.setattr(kms, "DEFAULT_SECRETS_PATH", path)

    assert kms.get_secret("GIBTS_NICHT", default="fallback") == "fallback"


@pytest.mark.unit
def test_get_secret_without_master_key_returns_default(tmp_path, monkeypatch):
    """KMS gar nicht eingerichtet -> kein Crash, sondern der Default."""
    import vms.infra.kms as kms
    monkeypatch.setenv("KMS_MASTER_KEY_PATH", str(tmp_path / "nicht-da.key"))

    assert kms.get_secret("SMTP_PASSWORD", default="fallback") == "fallback"


@pytest.mark.unit
def test_get_secret_propagates_undecryptable_secrets_file(tmp_path, monkeypatch):
    """Falscher Master-Key zur secrets.enc ist ein Konfigurationsfehler, kein 'nicht gesetzt'.

    Still den Default zu liefern lässt die App mit unkonfigurierten Secrets
    weiterlaufen, statt beim Start hart zu scheitern.
    """
    import vms.infra.kms as kms
    path = str(tmp_path / "secrets.enc")
    kms.save_secrets({"SMTP_PASSWORD": "pw"}, path=path, master_key=OTHER_KEY)
    kms.clear_cache()
    monkeypatch.setenv("KMS_MASTER_KEY_PATH", _write_key(tmp_path))
    monkeypatch.setattr(kms, "DEFAULT_SECRETS_PATH", path)

    with pytest.raises(InvalidToken):
        kms.get_secret("SMTP_PASSWORD")


# --- is_kms_available ---------------------------------------------------------

@pytest.mark.unit
def test_is_kms_available_true_when_key_and_secrets_exist(tmp_path, monkeypatch):
    import vms.infra.kms as kms
    secrets = tmp_path / "secrets.enc"
    secrets.write_text("x")
    monkeypatch.setenv("KMS_MASTER_KEY_PATH", _write_key(tmp_path))
    monkeypatch.setattr(kms, "DEFAULT_SECRETS_PATH", str(secrets))

    assert kms.is_kms_available() is True


@pytest.mark.unit
def test_is_kms_available_false_without_master_key(tmp_path, monkeypatch):
    import vms.infra.kms as kms
    secrets = tmp_path / "secrets.enc"
    secrets.write_text("x")
    monkeypatch.setenv("KMS_MASTER_KEY_PATH", str(tmp_path / "nicht-da.key"))
    monkeypatch.setattr(kms, "DEFAULT_SECRETS_PATH", str(secrets))

    assert kms.is_kms_available() is False


@pytest.mark.unit
def test_is_kms_available_false_without_secrets_file(tmp_path, monkeypatch):
    import vms.infra.kms as kms
    monkeypatch.setenv("KMS_MASTER_KEY_PATH", _write_key(tmp_path))
    monkeypatch.setattr(kms, "DEFAULT_SECRETS_PATH", str(tmp_path / "keine-secrets.enc"))

    assert kms.is_kms_available() is False


# --- clear_cache --------------------------------------------------------------

@pytest.mark.unit
def test_clear_cache_forces_reload_from_disk(tmp_path):
    import vms.infra.kms as kms
    path = _write_key(tmp_path)
    kms.load_master_key(path)

    kms.clear_cache()
    os.remove(path)

    with pytest.raises(FileNotFoundError):
        kms.load_master_key(path)

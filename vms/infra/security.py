"""
Security utilities for encrypting/decrypting sensitive values.
Uses KMS master key in production, falls back to SECRET_KEY in development.
"""
import base64
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from flask import current_app


class EncryptionError(RuntimeError):
    """Ein Wert konnte nicht verschlüsselt werden (KMS/SECRET_KEY fehlkonfiguriert)."""


class DecryptionError(RuntimeError):
    """Ein gespeichertes Chiffrat konnte nicht entschlüsselt werden.

    Ursache ist meist ein fehlender/geänderter Schlüssel: der zum Verschlüsseln
    genutzte Zweig (KMS oder SECRET_KEY) ist nicht mehr verfügbar, oder der
    SECRET_KEY wurde rotiert. Dieser Fehler macht den Fall unterscheidbar von
    "kein Wert gespeichert" (dafür steht weiterhin None).
    """


# Chiffrat-Versionstags. Das Tag hält fest, *welcher* Ableitungszweig das Chiffrat
# erzeugt hat, damit die Entschlüsselung korrekt bleibt, selbst wenn
# is_kms_available() später kippt (Volume nicht gemountet, anderer CWD → relativer
# KMS-Pfad). Frühere, ungetaggte Chiffrate ("legacy") werden weiterhin über die
# gerade verfügbaren Zweige probiert. Es wird bewusst NICHT migriert; neue Werte
# tragen das Tag, alte bleiben lesbar.
_TAG_KMS = 'k1:'
_TAG_SECRET = 's1:'


def _fernet_kms():
    """Fernet aus dem KMS-Master-Key, oder None wenn KMS nicht verfügbar ist.

    Ein zu kurzer Master-Key lässt load_master_key einen ValueError werfen — der
    propagiert bewusst (kein stiller Fallback auf SECRET_KEY für einen kaputten Key).
    """
    try:
        from vms.infra.kms import is_kms_available, load_master_key
        if not is_kms_available():
            return None
        master_key = load_master_key()
    except (ImportError, FileNotFoundError):
        return None
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b'vms_kms_v1',
        iterations=200_000,
    )
    return Fernet(base64.urlsafe_b64encode(kdf.derive(master_key)))


def _fernet_secret():
    """Fernet aus dem Flask-SECRET_KEY, oder None wenn keiner gesetzt ist."""
    secret = current_app.config.get('SECRET_KEY')
    if not secret:
        return None
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b'vms_static_salt',
        iterations=100_000,
    )
    return Fernet(base64.urlsafe_b64encode(kdf.derive(secret.encode())))


def _active_fernet():
    """(tag, Fernet) für den Zweig, der neue Werte verschlüsselt: KMS wenn verfügbar,
    sonst SECRET_KEY."""
    f = _fernet_kms()
    if f is not None:
        return _TAG_KMS, f
    f = _fernet_secret()
    if f is not None:
        return _TAG_SECRET, f
    raise ValueError("SECRET_KEY must be set in Flask config")


def encrypt_value(value: str) -> str:
    """Encrypt a string value.

    Leerer Input -> None. Jeder andere Fehlschlag wirft EncryptionError: ein
    stilles None würde vom Aufrufer als Chiffrat gespeichert (oder übersprungen)
    und der Nutzer bekäme eine Erfolgsmeldung für ein nie gespeichertes Secret.

    Das Chiffrat trägt ein Versionstag (s. _TAG_*), damit die Entschlüsselung den
    erzeugenden Zweig kennt und ein Kippen von is_kms_available() übersteht.
    """
    if not value:
        return None
    try:
        tag, f = _active_fernet()
        return tag + f.encrypt(value.encode()).decode()
    except Exception as e:
        current_app.logger.error(f"Encryption error: {e}")
        raise EncryptionError(f"Wert konnte nicht verschlüsselt werden: {e}") from e


def decrypt_value(token: str) -> str:
    """Decrypt an encrypted string value.

    Leerer Input -> None ("kein Wert gespeichert"). Ein nicht entschlüsselbares
    Chiffrat wirft DecryptionError, damit es nicht mit "nicht gesetzt"
    verwechselt wird.

    Getaggte Chiffrate werden gezielt mit dem erzeugenden Zweig entschlüsselt (ein
    Fehlschlag ist dann ein echter Fehler, z. B. rotierter SECRET_KEY). Ungetaggte
    Legacy-Chiffrate werden über alle verfügbaren Zweige probiert.
    """
    if not token:
        return None
    try:
        if token.startswith(_TAG_KMS):
            fernets = [_fernet_kms()]
            body = token[len(_TAG_KMS):]
        elif token.startswith(_TAG_SECRET):
            fernets = [_fernet_secret()]
            body = token[len(_TAG_SECRET):]
        else:
            # Legacy (ungetaggt): beide gerade verfügbaren Zweige durchprobieren.
            fernets = [_fernet_kms(), _fernet_secret()]
            body = token

        raw = body.encode()
        last_error = None
        for f in fernets:
            if f is None:
                continue
            try:
                return f.decrypt(raw).decode()
            except Exception as e:  # falscher Key für dieses Chiffrat
                last_error = e
        raise last_error or ValueError("kein passender Schlüssel verfügbar")
    except Exception as e:
        current_app.logger.error(f"Decryption error: {e}")
        raise DecryptionError(f"Wert konnte nicht entschlüsselt werden: {e}") from e

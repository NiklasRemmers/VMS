"""
Security utilities for encrypting/decrypting sensitive values.
Uses KMS master key in production, falls back to SECRET_KEY in development.
"""
import base64
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from flask import current_app


def _get_fernet():
    """
    Derive encryption key.
    In production: uses KMS master key (stored outside project).
    In development: derives from Flask SECRET_KEY.
    """
    # Try KMS first
    try:
        from kms import is_kms_available, load_master_key
        if is_kms_available():
            master_key = load_master_key()
            kdf = PBKDF2HMAC(
                algorithm=hashes.SHA256(),
                length=32,
                salt=b'contract_maker_kms_v1',
                iterations=200_000,
            )
            key = base64.urlsafe_b64encode(kdf.derive(master_key))
            return Fernet(key)
    except (ImportError, FileNotFoundError):
        pass  # Fall through to SECRET_KEY method

    # Fallback: derive from SECRET_KEY (development mode)
    if not current_app.config.get('SECRET_KEY'):
        raise ValueError("SECRET_KEY must be set in Flask config")

    password = current_app.config['SECRET_KEY'].encode()
    salt = b'contract_maker_static_salt'

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100_000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(password))
    return Fernet(key)


def encrypt_value(value: str) -> str:
    """Encrypt a string value."""
    if not value:
        return None
    try:
        f = _get_fernet()
        return f.encrypt(value.encode()).decode()
    except Exception as e:
        current_app.logger.error(f"Encryption error: {e}")
        return None


def decrypt_value(token: str) -> str:
    """Decrypt an encrypted string value."""
    if not token:
        return None
    try:
        f = _get_fernet()
        return f.decrypt(token.encode()).decode()
    except Exception as e:
        current_app.logger.error(f"Decryption error: {e}")
        return None

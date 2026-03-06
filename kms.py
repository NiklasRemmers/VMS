"""
Local Key Management Service (KMS) for VMS.
Manages master key loading and secret encryption/decryption.
"""
import base64
import json
import os
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

# Default paths
DEFAULT_MASTER_KEY_PATH = '/etc/vms/master.key'
DEFAULT_SECRETS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'secrets.enc')

# Cached state
_master_key = None
_secrets = None


def _derive_fernet_key(master_key: bytes, salt: bytes = b'vms_kms_v1') -> Fernet:
    """Derive a Fernet encryption key from the master key."""
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=200_000,
    )
    key = base64.urlsafe_b64encode(kdf.derive(master_key))
    return Fernet(key)


def load_master_key(path: str = None) -> bytes:
    """Load master key from file. Caches the key in memory."""
    global _master_key
    if _master_key is not None:
        return _master_key

    key_path = path or os.environ.get('KMS_MASTER_KEY_PATH', DEFAULT_MASTER_KEY_PATH)

    if not os.path.exists(key_path):
        raise FileNotFoundError(
            f"Master key not found at {key_path}. "
            f"Run 'python kms_setup.py generate' to create one."
        )

    with open(key_path, 'rb') as f:
        _master_key = f.read().strip()

    if len(_master_key) < 32:
        raise ValueError("Master key is too short (minimum 32 bytes)")

    return _master_key


def encrypt_secret(plaintext: str, master_key: bytes = None) -> str:
    """Encrypt a plaintext secret using the master key."""
    if not plaintext:
        return ''
    mk = master_key or load_master_key()
    f = _derive_fernet_key(mk)
    return f.encrypt(plaintext.encode('utf-8')).decode('utf-8')


def decrypt_secret(ciphertext: str, master_key: bytes = None) -> str:
    """Decrypt a ciphertext secret using the master key."""
    if not ciphertext:
        return ''
    mk = master_key or load_master_key()
    f = _derive_fernet_key(mk)
    return f.decrypt(ciphertext.encode('utf-8')).decode('utf-8')


def encrypt_binary(data: bytes, master_key: bytes = None) -> bytes:
    """Encrypt binary data using the master key."""
    if not data:
        return b''
    mk = master_key or load_master_key()
    f = _derive_fernet_key(mk)
    return f.encrypt(data)


def decrypt_binary(data: bytes, master_key: bytes = None) -> bytes:
    """Decrypt binary data using the master key."""
    if not data:
        return b''
    mk = master_key or load_master_key()
    f = _derive_fernet_key(mk)
    return f.decrypt(data)


def save_secrets(secrets_dict: dict, path: str = None, master_key: bytes = None):
    """Encrypt and save a dictionary of secrets to file."""
    mk = master_key or load_master_key()
    f = _derive_fernet_key(mk)
    plaintext = json.dumps(secrets_dict).encode('utf-8')
    encrypted = f.encrypt(plaintext).decode('utf-8')

    secrets_path = path or DEFAULT_SECRETS_PATH
    with open(secrets_path, 'w') as sf:
        sf.write(encrypted)

    # Restrict file permissions
    os.chmod(secrets_path, 0o600)


def load_secrets(path: str = None, master_key: bytes = None) -> dict:
    """Load and decrypt secrets from file. Caches the result."""
    global _secrets
    if _secrets is not None:
        return _secrets

    secrets_path = path or DEFAULT_SECRETS_PATH
    mk = master_key or load_master_key()

    if not os.path.exists(secrets_path):
        return {}

    with open(secrets_path, 'r') as sf:
        encrypted = sf.read().strip()

    if not encrypted:
        return {}

    f = _derive_fernet_key(mk)
    decrypted = f.decrypt(encrypted.encode('utf-8')).decode('utf-8')
    _secrets = json.loads(decrypted)
    return _secrets


def get_secret(key: str, default: str = None) -> str:
    """Get a single secret by key, with optional default."""
    try:
        secrets = load_secrets()
        return secrets.get(key, default)
    except (FileNotFoundError, Exception):
        return default


def is_kms_available() -> bool:
    """Check if KMS is configured and available."""
    key_path = os.environ.get('KMS_MASTER_KEY_PATH', DEFAULT_MASTER_KEY_PATH)
    return os.path.exists(key_path) and os.path.exists(DEFAULT_SECRETS_PATH)


def clear_cache():
    """Clear cached master key and secrets (useful for testing)."""
    global _master_key, _secrets
    _master_key = None
    _secrets = None

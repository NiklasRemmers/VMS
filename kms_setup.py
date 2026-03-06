#!/usr/bin/env python3
"""
KMS Setup CLI Tool for VMS.
Generates master keys and encrypts application secrets.

Usage:
    python kms_setup.py generate [--path /etc/vms/master.key]
    python kms_setup.py encrypt [--env .env] [--output secrets.enc]
    python kms_setup.py verify
    python kms_setup.py show-keys
"""
import argparse
import json
import os
import secrets
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def generate_master_key(path: str):
    """Generate a cryptographically secure master key."""
    key_dir = os.path.dirname(path)

    if key_dir and not os.path.exists(key_dir):
        try:
            os.makedirs(key_dir, mode=0o700, exist_ok=True)
            print(f"✓ Created directory: {key_dir}")
        except PermissionError:
            print(f"✗ Permission denied creating {key_dir}")
            print(f"  Run: sudo mkdir -p {key_dir} && sudo chown $(whoami) {key_dir}")
            sys.exit(1)

    if os.path.exists(path):
        confirm = input(f"⚠ Master key already exists at {path}. Overwrite? (y/N): ")
        if confirm.lower() != 'y':
            print("Aborted.")
            sys.exit(0)

    # Generate 64 bytes of cryptographically secure random data
    key = secrets.token_bytes(64)

    try:
        with open(path, 'wb') as f:
            f.write(key)
        os.chmod(path, 0o600)
        print(f"✓ Master key generated: {path}")
        print(f"  Permissions: 600 (owner read/write only)")
        print(f"  Key length: {len(key)} bytes")
        print()
        print("⚠ IMPORTANT: Back up this key securely!")
        print("  If lost, all encrypted secrets become unrecoverable.")
    except PermissionError:
        print(f"✗ Permission denied writing to {path}")
        print(f"  Run: sudo chown $(whoami) {key_dir}")
        sys.exit(1)


def encrypt_secrets(env_path: str, output_path: str, master_key_path: str):
    """Read sensitive values from .env and encrypt them."""
    from kms import load_master_key, save_secrets

    # Sensitive keys to extract from .env
    SENSITIVE_KEYS = [
        'SECRET_KEY',
        'KANBOARD_TOKEN',
        'IMAP_PASSWORD',
        'MAIL_PASSWORD',
    ]

    if not os.path.exists(env_path):
        print(f"✗ .env file not found: {env_path}")
        sys.exit(1)

    # Parse .env file
    env_vars = {}
    with open(env_path, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' in line:
                key, _, value = line.partition('=')
                key = key.strip()
                value = value.strip()
                if key in SENSITIVE_KEYS:
                    env_vars[key] = value

    if not env_vars:
        print("✗ No sensitive keys found in .env")
        sys.exit(1)

    print(f"Found {len(env_vars)} secrets to encrypt:")
    for key in env_vars:
        print(f"  • {key} ({len(env_vars[key])} chars)")

    # Load master key and encrypt
    try:
        mk = load_master_key(master_key_path)
    except FileNotFoundError as e:
        print(f"\n✗ {e}")
        sys.exit(1)

    save_secrets(env_vars, output_path, mk)
    print(f"\n✓ Secrets encrypted and saved to: {output_path}")
    print(f"  Permissions: 600 (owner read/write only)")

    # Suggest cleaning .env
    print()
    print("━" * 50)
    print("NEXT STEP: Remove sensitive values from .env")
    print("━" * 50)
    print()
    print("Replace these lines in .env with empty values:")
    for key in env_vars:
        print(f"  {key}=   # Now managed by KMS")
    print()
    print("Or run with --clean to do this automatically.")


def verify_secrets(master_key_path: str, secrets_path: str):
    """Verify that encrypted secrets can be decrypted."""
    from kms import load_master_key, load_secrets, clear_cache

    clear_cache()

    try:
        mk = load_master_key(master_key_path)
        print(f"✓ Master key loaded ({len(mk)} bytes)")
    except FileNotFoundError as e:
        print(f"✗ {e}")
        sys.exit(1)

    try:
        secrets_dict = load_secrets(secrets_path, mk)
        print(f"✓ Secrets file decrypted successfully")
        print(f"  Contains {len(secrets_dict)} secret(s):")
        for key in secrets_dict:
            val = secrets_dict[key]
            masked = val[:3] + '*' * (len(val) - 3) if len(val) > 3 else '***'
            print(f"  • {key}: {masked}")
    except FileNotFoundError:
        print(f"✗ Secrets file not found: {secrets_path}")
        sys.exit(1)
    except Exception as e:
        print(f"✗ Decryption failed: {e}")
        print("  Master key may have changed since secrets were encrypted.")
        sys.exit(1)


def show_keys():
    """Show which secret keys are expected by the application."""
    keys = {
        'SECRET_KEY': 'Flask session encryption key',
        'KANBOARD_TOKEN': 'Kanboard API authentication token',
        'IMAP_PASSWORD': 'IMAP email server password',
        'MAIL_PASSWORD': 'SMTP email server password',
    }
    print("Secret keys managed by KMS:")
    print()
    for key, desc in keys.items():
        print(f"  {key:30s} {desc}")


def main():
    parser = argparse.ArgumentParser(
        description='VMS KMS Setup Tool',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python kms_setup.py generate
  python kms_setup.py encrypt
  python kms_setup.py verify
  python kms_setup.py show-keys
        """
    )
    subparsers = parser.add_subparsers(dest='command', help='Command to run')

    # Generate
    gen_parser = subparsers.add_parser('generate', help='Generate a new master key')
    gen_parser.add_argument('--path', default='/etc/vms/master.key',
                           help='Path to save the master key')

    # Encrypt
    enc_parser = subparsers.add_parser('encrypt', help='Encrypt secrets from .env')
    enc_parser.add_argument('--env', default='.env', help='Path to .env file')
    enc_parser.add_argument('--output', default='secrets.enc', help='Output file for encrypted secrets')
    enc_parser.add_argument('--key-path', default='/etc/vms/master.key',
                           help='Path to master key')

    # Verify
    ver_parser = subparsers.add_parser('verify', help='Verify encrypted secrets')
    ver_parser.add_argument('--key-path', default='/etc/vms/master.key',
                           help='Path to master key')
    ver_parser.add_argument('--secrets', default='secrets.enc', help='Path to encrypted secrets')

    # Show keys
    subparsers.add_parser('show-keys', help='Show managed secret keys')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    if args.command == 'generate':
        generate_master_key(args.path)
    elif args.command == 'encrypt':
        encrypt_secrets(args.env, args.output, args.key_path)
    elif args.command == 'verify':
        verify_secrets(args.key_path, args.secrets)
    elif args.command == 'show-keys':
        show_keys()


if __name__ == '__main__':
    main()

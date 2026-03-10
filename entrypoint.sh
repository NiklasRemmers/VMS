#!/bin/bash
set -e

echo "═══════════════════════════════════════════"
echo "  VMS — Bootstrap"
echo "═══════════════════════════════════════════"

KMS_DIR="/etc/vms"
KMS_KEY="$KMS_DIR/master.key"
SECRETS_FILE="/app/secrets.enc"

# ─── 1. KMS Master Key ───
if [ ! -f "$KMS_KEY" ]; then
    echo "⚙ Generating KMS master key..."
    mkdir -p "$KMS_DIR"
    python -c "
import secrets, os
key = secrets.token_bytes(64)
with open('$KMS_KEY', 'wb') as f:
    f.write(key)
os.chmod('$KMS_KEY', 0o600)
"
    echo "✓ Master key created: $KMS_KEY"
else
    echo "✓ Master key exists"
fi

# ─── 1b. Persistent Flask SECRET_KEY ───
FLASK_SECRET="$KMS_DIR/flask_secret_key"
if [ ! -f "$FLASK_SECRET" ]; then
    echo "⚙ Generating persistent Flask SECRET_KEY..."
    python -c "import secrets; print(secrets.token_hex(32))" > "$FLASK_SECRET"
    chmod 600 "$FLASK_SECRET"
    echo "✓ SECRET_KEY created: $FLASK_SECRET"
else
    echo "✓ SECRET_KEY exists"
fi
export SECRET_KEY=$(cat "$FLASK_SECRET")

# ─── 2. Encrypt Secrets ───
if [ ! -f "$SECRETS_FILE" ]; then
    echo "⚙ Encrypting secrets from environment..."
    python -c "
import os, json, sys
sys.path.insert(0, '/app')
os.environ['KMS_MASTER_KEY_PATH'] = '$KMS_KEY'

from kms import load_master_key, save_secrets, clear_cache
clear_cache()

secrets_dict = {}
for key in ['SECRET_KEY', 'DATABASE_URL', 'KANBOARD_TOKEN', 'IMAP_PASSWORD', 'MAIL_PASSWORD']:
    val = os.environ.get(key)
    if val:
        secrets_dict[key] = val

if secrets_dict:
    mk = load_master_key('$KMS_KEY')
    save_secrets(secrets_dict, '$SECRETS_FILE', mk)
    print(f'✓ {len(secrets_dict)} secret(s) encrypted')
else:
    print('⚠ No secrets found in environment to encrypt')
"
else
    echo "✓ Secrets file exists"
fi

# ─── 3. Wait for Database ───
echo "⚙ Waiting for database..."
python -c "
import time, os
from sqlalchemy import create_engine, text

url = os.environ.get('DATABASE_URL', 'postgresql://admin:admin@db:5432/vms')
for i in range(30):
    try:
        engine = create_engine(url)
        with engine.connect() as conn:
            conn.execute(text('SELECT 1'))
        print('✓ Database ready')
        break
    except Exception:
        time.sleep(1)
else:
    print('✗ Database not available after 30s')
    exit(1)
"

# ─── 4. Initialize Database Tables ───
echo "⚙ Initializing database tables..."
python -c "
import os
os.environ['KMS_MASTER_KEY_PATH'] = '$KMS_KEY'
from database import init_db
init_db()
print('✓ Tables ready')
"

echo ""
echo "═══════════════════════════════════════════"
echo "  Starting Gunicorn..."
echo "═══════════════════════════════════════════"
echo ""

exec gunicorn --config gunicorn.conf.py app:app

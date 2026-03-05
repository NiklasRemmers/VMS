# Contract Maker — Production Deployment Guide

## Prerequisites
- Ubuntu/Debian server with root access
- Domain name pointing to your server
- Python 3.10+

---

## 1. System Setup

```bash
# Create dedicated user
sudo useradd -m -s /bin/bash contract_maker

# Create directories
sudo mkdir -p /etc/contract_maker
sudo mkdir -p /var/log/contract_maker
sudo chown contract_maker:contract_maker /var/log/contract_maker

# Install system dependencies
sudo apt update && sudo apt install -y python3-venv python3-pip nginx certbot python3-certbot-nginx
```

## 2. Application Setup

```bash
# Switch to app user
sudo su - contract_maker

# Clone/copy application
# (copy your project to /home/contract_maker/contract_maker/)

# Create virtual environment
cd /home/contract_maker/contract_maker
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Set production mode in .env
# Edit .env: FLASK_ENV=production
```

## 3. KMS Setup

```bash
# Generate master key (as root or with sudo)
sudo python3 kms_setup.py generate --path /etc/contract_maker/master.key
sudo chown contract_maker:contract_maker /etc/contract_maker/master.key
sudo chmod 600 /etc/contract_maker/master.key

# Encrypt secrets from .env
source venv/bin/activate
python3 kms_setup.py encrypt --env .env --output secrets.enc

# Verify
python3 kms_setup.py verify

# Remove sensitive values from .env (keep only non-secret config)
# SECRET_KEY, KANBOARD_TOKEN, IMAP_PASSWORD, MAIL_PASSWORD → delete or blank
```

After KMS setup, your `.env` should only contain **non-sensitive** config:
```env
KANBOARD_URL=https://todo.stuve.de/jsonrpc.php
KANBOARD_USERNAME=jan-niklas.remmers
KANBOARD_PROJECT_ID=25
FLASK_ENV=production
IMAP_SERVER=imap.uni-ulm.de
IMAP_PORT=993
IMAP_USERNAME=qdv43
MAIL_SERVER=mail.uni-ulm.de
MAIL_PORT=587
MAIL_USE_TLS=true
MAIL_USE_SSL=false
MAIL_DEFAULT_SENDER=jan-niklas.remmers@uni-ulm.de
```

## 4. Database Permissions

```bash
chmod 600 /home/contract_maker/contract_maker/users.db
chown contract_maker:contract_maker /home/contract_maker/contract_maker/users.db
```

## 5. TLS/HTTPS with Let's Encrypt

```bash
# Get certificate (Nginx must be running on port 80)
sudo certbot --nginx -d YOUR_DOMAIN.de

# Auto-renewal is configured automatically by certbot
# Test: sudo certbot renew --dry-run
```

## 6. Nginx Setup

```bash
# Copy config
sudo cp deploy/nginx.conf /etc/nginx/sites-available/contract_maker

# Edit: replace YOUR_DOMAIN.de with your actual domain
sudo nano /etc/nginx/sites-available/contract_maker

# Edit: update the static files path
# alias /home/contract_maker/contract_maker/static/;

# Enable
sudo ln -s /etc/nginx/sites-available/contract_maker /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default

# Test & reload
sudo nginx -t
sudo systemctl reload nginx
```

## 7. Systemd Service

```bash
# Copy service file
sudo cp deploy/contract_maker.service /etc/systemd/system/

# Edit paths if needed
sudo nano /etc/systemd/system/contract_maker.service

# Enable & start
sudo systemctl daemon-reload
sudo systemctl enable contract_maker
sudo systemctl start contract_maker

# Check status
sudo systemctl status contract_maker
sudo journalctl -u contract_maker -f
```

## 8. Verify Deployment

```bash
# Check the service is running
curl -I https://YOUR_DOMAIN.de

# Expected: HTTP/2 200, with security headers
```

---

## Maintenance

### View Logs
```bash
sudo journalctl -u contract_maker -f          # Application logs
sudo tail -f /var/log/contract_maker/access.log  # Access logs
sudo tail -f /var/log/contract_maker/error.log   # Error logs
```

### Restart Application
```bash
sudo systemctl restart contract_maker
```

### Database Backup
```bash
# Add to crontab: crontab -e
0 2 * * * cp /home/contract_maker/contract_maker/users.db /home/contract_maker/backups/users_$(date +\%Y\%m\%d).db
```

### Update Application
```bash
sudo su - contract_maker
cd contract_maker
git pull  # or copy new files
source venv/bin/activate
pip install -r requirements.txt
exit
sudo systemctl restart contract_maker
```

---

## Architecture

```
Internet
   │
   ▼
┌────────────────────────┐
│  Nginx (HTTPS/TLS)     │  Port 443
│  Let's Encrypt certs   │
│  Security headers      │
└──────────┬─────────────┘
           │ proxy_pass
           ▼
┌────────────────────────┐
│  Gunicorn (WSGI)       │  127.0.0.1:8000
│  Multi-worker          │
│  Auto-restart          │
└──────────┬─────────────┘
           │
           ▼
┌────────────────────────┐
│  Flask App             │
│  ├── KMS (master.key)  │  /etc/contract_maker/
│  ├── secrets.enc       │  Encrypted secrets
│  ├── users.db          │  bcrypt hashes + Fernet-encrypted creds
│  └── .env              │  Non-sensitive config only
└────────────────────────┘
```

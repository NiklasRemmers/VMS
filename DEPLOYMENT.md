# VMS — Production Deployment Guide

## Prerequisites
- Ubuntu/Debian server with root access
- Domain name pointing to your server
- Python 3.10+

---

## 1. System Setup

```bash
# Create dedicated user
sudo useradd -m -s /bin/bash vms

# Create directories
sudo mkdir -p /etc/vms
sudo mkdir -p /var/log/vms
sudo chown vms:vms /var/log/vms

# Install system dependencies
sudo apt update && sudo apt install -y python3-venv python3-pip nginx certbot python3-certbot-nginx
```

## 2. Application Setup

```bash
# Switch to app user
sudo su - vms

# Clone/copy application
# (copy your project to /home/vms/vms/)

# Create virtual environment
cd /home/vms/vms
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Set production mode in .env
# Edit .env: FLASK_ENV=production
```

## 3. KMS Setup

```bash
# Generate master key (as root or with sudo)
sudo python3 kms_setup.py generate --path /etc/vms/master.key
sudo chown vms:vms /etc/vms/master.key
sudo chmod 600 /etc/vms/master.key

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
chmod 600 /home/vms/vms/users.db
chown vms:vms /home/vms/vms/users.db
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
sudo cp deploy/nginx.conf /etc/nginx/sites-available/vms

# Edit: replace YOUR_DOMAIN.de with your actual domain
sudo nano /etc/nginx/sites-available/vms

# Edit: update the static files path
# alias /home/vms/vms/static/;

# Enable
sudo ln -s /etc/nginx/sites-available/vms /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default

# Test & reload
sudo nginx -t
sudo systemctl reload nginx
```

## 7. Systemd Service

```bash
# Copy service file
sudo cp deploy/vms.service /etc/systemd/system/

# Edit paths if needed
sudo nano /etc/systemd/system/vms.service

# Enable & start
sudo systemctl daemon-reload
sudo systemctl enable vms
sudo systemctl start vms

# Check status
sudo systemctl status vms
sudo journalctl -u vms -f
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
sudo journalctl -u vms -f          # Application logs
sudo tail -f /var/log/vms/access.log  # Access logs
sudo tail -f /var/log/vms/error.log   # Error logs
```

### Restart Application
```bash
sudo systemctl restart vms
```

### Database Backup
```bash
# Add to crontab: crontab -e
0 2 * * * cp /home/vms/vms/users.db /home/vms/backups/users_$(date +\%Y\%m\%d).db
```

### Update Application
```bash
sudo su - vms
cd vms
git pull  # or copy new files
source venv/bin/activate
pip install -r requirements.txt
exit
sudo systemctl restart vms
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
│  ├── KMS (master.key)  │  /etc/vms/
│  ├── secrets.enc       │  Encrypted secrets
│  ├── users.db          │  bcrypt hashes + Fernet-encrypted creds
│  └── .env              │  Non-sensitive config only
└────────────────────────┘
```

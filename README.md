# VMS — Verleih-Management-System

Flask-Anwendung zur Verwaltung von Materialverleih: erzeugt **Leihverträge,
Rechnungen und Umbuchungen** aus versionierten ODT-Vorlagen (Export als PDF),
verwaltet **Inventar**, importiert **Leihanfragen per E-Mail** und hält den
Status mit einem **Kanboard**-Board synchron. Mit Nutzer-Authentifizierung,
verschlüsselter Secret-Verwaltung (KMS) und PostgreSQL-Backend.

---

## Inhalt

- [Funktionsumfang](#funktionsumfang)
- [Technologie-Stack](#technologie-stack)
- [Projektstruktur](#projektstruktur)
- [Architektur-Prinzipien](#architektur-prinzipien)
- [Lokale Entwicklung](#lokale-entwicklung)
- [Konfiguration (Umgebungsvariablen)](#konfiguration-umgebungsvariablen)
- [Datenbank & Migrationen](#datenbank--migrationen)
- [Tests](#tests)
- [Deployment](#deployment)
- [Sicherheit](#sicherheit)
- [Dokumentation & Wissensgraph](#dokumentation--wissensgraph)

---

## Funktionsumfang

| Bereich | Beschreibung |
|---|---|
| **Dokumente** | Leihvertrag, Rechnung und Umbuchung aus ODT-Vorlagen füllen und als PDF exportieren (LibreOffice im Headless-Modus). Vorlagen werden versioniert im `template_store` gehalten. |
| **Inventar** | CRUD für Artikel, Bundles und Lagerorte; Tags für die Materialauswahl. |
| **E-Mail-Import** | Leihanfragen werden per IMAP abgeholt, das Formular aus dem Mail-Text extrahiert und als `EmailCandidate` gespeichert. |
| **Kanboard-Abgleich** | Kandidaten werden mit Karten auf einem Kanboard-Board abgeglichen (Spaltenwechsel je nach Verleih-Status); ein täglicher Scheduler stößt den Abgleich automatisch an. |
| **Rechnungen** | Fortlaufende, eindeutige Rechnungs-/Umbuchungsnummern (DB-seitig über einen Unique-Index abgesichert). |
| **Nutzerverwaltung** | Login (bcrypt-Hashes), Einladungs-Flow, Rate-Limiting, CSRF-Schutz. |

---

## Technologie-Stack

- **Backend:** Python 3.12, Flask 3, SQLAlchemy 2, Flask-Login, Flask-WTF, Flask-Mail, Flask-Limiter
- **Datenbank:** PostgreSQL (psycopg2)
- **Dokumente:** ODT-Vorlagen + LibreOffice (`libreoffice-writer`) für die PDF-Konvertierung
- **Scheduling:** APScheduler (täglicher Kanboard-Abgleich, per Postgres-Advisory-Lock gegen Mehrfachausführung geschützt)
- **Secrets:** eigenes KMS-Modul (Fernet-verschlüsselte `secrets.enc`, Master-Key auf der Platte)
- **Server:** Gunicorn hinter Nginx
- **Tests:** pytest, pytest-cov, pytest-mock, Testcontainers (ephemeres PostgreSQL), responses, freezegun

---

## Projektstruktur

```
vms/                     Anwendungspaket (importierbar als vms.*)
  app.py                 Flask-Wiring, Blueprint-Registrierung, Top-Level-Routes
                         (/, /health, /api/generate, Kanboard-Proxy, Scheduler-Setup)
  auth.py                Nutzer, Login, Einladungen, Rate-Limiter (querschnittlich)
  routes/                je ein Flask-Blueprint pro Datei
    inventory.py           Inventar-, Bundle- und Lagerort-CRUD
    invoice.py             Rechnung / Umbuchung erzeugen & versenden
    verleih.py             Leihvorgänge / Vertragserstellung
    email.py               E-Mail-Kandidaten, Sync-Endpunkte
    settings.py            Nutzer- & Systemeinstellungen
    templates.py           Upload/Verwaltung der ODT-Dokumentvorlagen
  domain/                Kern-Domäne, ohne Web-Abhängigkeit
    models.py              SQLAlchemy-Modelle, Datums-Helfer, laufende Nummern
    database.py            Engine-/Session-Verwaltung (lazy, per Worker)
  clients/               Anbindung externer Systeme
    email_client.py        IMAP-Import & Parsing der Leihanfragen
    kanboard_client.py     Kanboard-JSON-RPC, Status-Reconciliation
  infra/                 Infrastruktur / Querschnitt
    kms.py                 Laden/Entschlüsseln der Secrets (Fernet)
    kms_setup.py           CLI zum Provisionieren des Secret-Stores
    security.py            Fernet-Ver-/Entschlüsselung einzelner Werte
    odt_processor.py       Platzhalter-Ersetzung in ODT + ODT→PDF-Konvertierung
    template_store.py      versionierter Store der ODT-Vorlagen

migrations/              einmalige Migrations-Skripte (migrate_*.py)
assets/                  gebündelte ODT-Vorlagen (template*.odt) — Fallback/Seed für den Store
templates/               Jinja-HTML-Templates (Frontend)
static/                  JS/CSS/Assets fürs Frontend
tests/                   pytest-Suite + Fixtures (tests/conftest.py)
docs/                    Specs, FINDINGS.md, TEST_PLAN.md
ops/                     Deployment
  Dockerfile               Multi-Stage-Image (Build-Kontext = Repo-Root)
  docker-compose.yml       db + app + nginx + certbot
  gunicorn.conf.py         Gunicorn-Config inkl. post_fork-Scheduler
  entrypoint.sh            Container-Bootstrap (KMS, DB-Init, Migrationen, Start)
  deploy/                  systemd-Unit (vms.service), nginx-Configs
```

---

## Architektur-Prinzipien

- **Dünne Routes, reine Domäne:** Blueprints in `vms/routes/` orchestrieren nur;
  die Fachlogik liegt in `vms/domain/` und `vms/infra/`.
- **Absolute Paket-Imports:** durchgängig `from vms.<subpkg>.<mod> import …`
  (z. B. `from vms.domain.models import EmailCandidate`,
  `from vms.infra.template_store import load_template`). Keine flachen Imports.
- **Blueprints + CSRF:** API-Blueprints sind vom Formular-CSRF ausgenommen und
  nutzen stattdessen den `X-CSRFToken`-Header (siehe `vms/app.py`).
- **Lazy DB pro Worker:** `vms/domain/database.py` legt Engine/Session verzögert
  an; Gunicorn verwirft den Pool nach dem Fork (`post_fork` in `ops/gunicorn.conf.py`).
- **Datums-Konvention:** Anzeige `DD.MM.YYYY`, intern ISO — über `format_de_date`
  bzw. den Jinja-Filter `de_date`.

---

## Lokale Entwicklung

**Voraussetzungen:** Python 3.12, ein laufender Docker-Daemon (für die Tests),
sowie `libreoffice-writer`, falls die PDF-Konvertierung lokal laufen soll.

```bash
# 1. Virtuelle Umgebung
python -m venv venv && source venv/bin/activate

# 2. Abhängigkeiten
pip install -r requirements.txt -r requirements-dev.txt

# 3. Konfiguration
cp .env.example .env   # falls vorhanden; sonst .env manuell anlegen (siehe unten)

# 4. PostgreSQL bereitstellen (z. B. per Docker)
docker run --rm -e POSTGRES_USER=admin -e POSTGRES_PASSWORD=admin \
  -e POSTGRES_DB=vms -p 5432:5432 postgres:16-alpine

# 5. App starten (WSGI-Objekt ist vms.app:app)
gunicorn --config ops/gunicorn.conf.py vms.app:app
# oder für die Entwicklung:
FLASK_APP=vms.app flask run
```

Die App ist danach unter `http://localhost:8000` (Gunicorn) bzw. dem Flask-Dev-Port
erreichbar; `/health` liefert `{"status": "ok"}`.

---

## Konfiguration (Umgebungsvariablen)

Secrets werden in Produktion aus dem verschlüsselten **KMS-Store** (`secrets.enc`)
geladen; ist kein KMS konfiguriert, greift die App auf die `.env` zurück.

| Variable | Zweck | Default |
|---|---|---|
| `DATABASE_URL` | PostgreSQL-Verbindung (`postgresql://…`) | lokal, siehe `database.py` |
| `SECRET_KEY` | Flask-Session-Schlüssel | im Container automatisch generiert & persistiert |
| `FLASK_ENV` | `production` schaltet Secure-Cookies etc. scharf | – |
| `MAIL_SERVER` / `MAIL_PORT` | SMTP-Versand | `localhost` / `587` |
| `MAIL_USE_TLS` / `MAIL_USE_SSL` | SMTP-Transportsicherheit | `true` / `false` |
| `MAIL_USERNAME` / `MAIL_PASSWORD` | SMTP-Zugang (Secret) | – |
| `MAIL_DEFAULT_SENDER` | Absenderadresse | `MAIL_USERNAME` |
| `IMAP_SERVER` / `IMAP_PORT` | Abholen der Leihanfragen | – |
| `KANBOARD_URL` / `KANBOARD_PROJECT_ID` | Kanboard-Anbindung | – |
| `KMS_MASTER_KEY_PATH` | Pfad zum KMS-Master-Key | `/etc/vms/master.key` |
| `KMS_SECRETS_PATH` | Pfad zur verschlüsselten `secrets.enc` | Repo-Root |
| `SQL_ECHO` | SQLAlchemy-SQL-Logging (`true`) | `false` |
| `GUNICORN_BIND` / `GUNICORN_WORKERS` | Gunicorn-Bind/Worker | `127.0.0.1:8000` / `CPU*2+1` |
| `GUNICORN_ACCESS_LOG` / `GUNICORN_ERROR_LOG` / `GUNICORN_LOG_LEVEL` | Gunicorn-Logging | `/var/log/vms/*` / `info` |

---

## Datenbank & Migrationen

- Das Schema wird beim Bootstrap über `init_db()` (`vms/domain/database.py`)
  angelegt; der Container-`entrypoint.sh` ruft das automatisch auf und führt
  zusätzlich inline-Spaltenmigrationen aus.
- Einmalige, in sich abgeschlossene Migrationen liegen in `migrations/` und werden
  als Modul ausgeführt, z. B.:

  ```bash
  python -m migrations.migrate_unique_laufende_nummer
  ```

  (importieren `from vms.domain… import` und benötigen daher das Repo-Root auf dem Pfad).

---

## Tests

Die Suite nutzt einen **PostgreSQL-Testcontainer** — ein laufender Docker-Daemon
ist Voraussetzung. Die Fixtures leben in `tests/conftest.py`.

```bash
pytest                     # volle Suite mit Coverage (term + xml + html)
pytest -m unit             # nur reine Logik, ohne DB/App-Context
pytest -m "not slow"       # schnelle Feedback-Schleife
pytest tests/test_app.py   # einzelne Datei
```

**Marker** (siehe `pyproject.toml`): `unit`, `integration`, `route`, `slow`.
Coverage-Reports landen in `coverage.xml` (maschinell) und `htmlcov/` (Review).
`pythonpath = ["."]` macht das `vms`-Paket im Testlauf importierbar.

> Konventionen für neue Tests fasst der `testing`-Skill zusammen; für neue Features
> gibt es die Skills `tdd`/`feature`, für Bugfixes `bugfix`.

---

## Deployment

`Dockerfile` und `docker-compose.yml` liegen unter `ops/`; der **Build-Kontext ist
das Repo-Root**, weil das Image die Ordner `vms/`, `assets/`, `templates/`,
`static/` und `migrations/` kopiert.

```bash
# Image bauen
docker build -f ops/Dockerfile -t vms .

# Kompletter Stack: db + app + nginx + certbot
docker compose -f ops/docker-compose.yml up -d
```

Der Container-`entrypoint.sh` durchläuft beim Start:

1. **KMS-Master-Key** erzeugen (falls nicht vorhanden)
2. **Flask `SECRET_KEY`** erzeugen und persistieren
3. **Secrets** aus den Umgebungsvariablen in `secrets.enc` verschlüsseln
4. auf die **Datenbank warten**, dann **Tabellen initialisieren** und Spaltenmigrationen fahren
5. `gunicorn --config ops/gunicorn.conf.py vms.app:app` starten

**Bare-Metal (systemd):** `ops/deploy/vms.service` nach `/etc/systemd/system/`
kopieren, aktivieren und starten. Der Dienst startet aus dem Checkout-Verzeichnis
`gunicorn --config ops/gunicorn.conf.py vms.app:app`.

> `.dockerignore` bleibt bewusst im **Repo-Root** — Docker wertet die Datei relativ
> zum Build-Kontext aus, nicht relativ zum Dockerfile.

---

## Sicherheit

- **Secret-Management:** produktive Secrets liegen Fernet-verschlüsselt in
  `secrets.enc`; der Master-Key liegt getrennt (`KMS_MASTER_KEY_PATH`). Ohne KMS
  fällt die App für die Entwicklung auf `.env` zurück.
- **Passwörter:** bcrypt-Hashes (`vms/auth.py`).
- **Web-Härtung:** CSRF-Schutz (Flask-WTF), Rate-Limiting (Flask-Limiter),
  Secure-/HttpOnly-/SameSite-Cookies in Produktion, `ProxyFix` hinter Nginx.
- **Nie eingecheckt:** `.env`, `secrets.enc`, `*.key`, `kms_local/` (siehe `.gitignore`).

---

## Dokumentation & Wissensgraph

- `docs/` enthält Feature-Specs, `FINDINGS.md` (bekannte Probleme) und `TEST_PLAN.md`.
- Das Repo pflegt einen **graphify**-Wissensgraph in `graphify-out/`. Für
  Codebase-Fragen: `graphify query "<frage>"`; nach Code-Änderungen `graphify update .`.

"""
Shared test fixtures for VMS.

Design constraints handled here (see docs/TEST_PLAN.md for rationale):
  * `app` is a module-level singleton created at import time -> env must be set
    BEFORE importing app/database, and the lazy DB globals reset per session.
  * Models use PostgreSQL-only types (JSONB, tz-aware DateTime) -> we test against
    a real ephemeral PostgreSQL via Testcontainers, never SQLite.
  * CSRF, rate limiting and outbound mail/HTTP/LibreOffice are neutralised so tests
    exercise application logic, not infrastructure.
"""
import os
import pytest

# --- 1. Environment must be configured before app/database import -------------
os.environ.setdefault("FLASK_ENV", "testing")
os.environ.setdefault("SECRET_KEY", "test-secret-key-deterministic-000000000000")
os.environ.setdefault("RATELIMIT_ENABLED", "false")
os.environ.setdefault("SQL_ECHO", "false")
# Ryuk (der Testcontainers-Reaper) räumt per Container-kill auf und scheitert
# damit am selben Docker-/runc-Problem wie pg.stop(). Aus.
os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")


@pytest.fixture(autouse=True)
def _kms_isolated(monkeypatch):
    """Make KMS state deterministic and independent of .env / cwd / test order.

    Two hazards this closes:
      * kms._master_key / kms._secrets are process-wide caches. Once any test
        loads a key, later loads short-circuit on the cache and the file-missing
        and too-short-key branches become unreachable.
      * .env sets KMS_MASTER_KEY_PATH to a *relative* path that exists in the
        repo, so is_kms_available() would be True and security._get_fernet would
        always take the KMS branch, never the SECRET_KEY fallback.

    Default here is "KMS unavailable" -> SECRET_KEY path. A test that wants the
    KMS branch sets KMS_MASTER_KEY_PATH itself (to a tmp_path key file).
    """
    import vms.infra.kms as kms

    monkeypatch.delenv("KMS_MASTER_KEY_PATH", raising=False)
    kms.clear_cache()
    yield
    kms.clear_cache()


@pytest.fixture(scope="session")
def _pg_container():
    """Start one PostgreSQL container for the whole test session.

    Teardown ist bewusst fehlertolerant: ein Docker-Daemon, der Container nicht
    stoppen kann (siehe docs/FINDINGS.md), darf eine grüne Suite nicht rot
    färben. Der Container leakt dann pro Lauf und wird am Host aufgeräumt.
    """
    from testcontainers.postgres import PostgresContainer

    pg = PostgresContainer("postgres:16-alpine")
    pg.start()
    try:
        # SQLAlchemy 2.x + psycopg2 driver URL
        url = pg.get_connection_url().replace("postgresql+psycopg2", "postgresql")
        os.environ["DATABASE_URL"] = url
        yield url
    finally:
        try:
            pg.stop()
        except Exception as e:
            import warnings
            warnings.warn(f"Testcontainer-Teardown fehlgeschlagen (Container geleakt): {e}")


@pytest.fixture(scope="session")
def _app(_pg_container):
    """Import the Flask app once, wired to the test database, and create schema."""
    import vms.domain.database as database

    # Reset lazy globals so the engine binds to the container, not a stale URL.
    database._database_url = None
    database._engine = None
    database._SessionFactory = None

    import vms.app as app_module

    flask_app = app_module.app
    flask_app.config.update(
        TESTING=True,
        WTF_CSRF_ENABLED=False,       # forms are tested via their handlers, not tokens
        MAIL_SUPPRESS_SEND=True,      # capture mail with mail.record_messages() instead
        MAIL_DEFAULT_SENDER="vms-test@example.com",  # prod sets this (app.py:79); mirror it so the Flask-Mail fallback works
        SERVER_NAME="localhost",
    )

    # Flask-Mail caches config (default_sender, suppress) at init time, which ran on
    # import before the overrides above. Re-init so it picks up MAIL_DEFAULT_SENDER.
    app_module.mail.init_app(flask_app)

    # Disable flask-limiter regardless of env handling.
    try:
        from vms.auth import limiter
        limiter.enabled = False
    except Exception:
        pass

    database.init_db()  # CREATE TABLE ... from models.Base
    return flask_app


@pytest.fixture()
def app(_app):
    """Function-scoped app with a clean database (truncate between tests)."""
    from sqlalchemy import text
    import vms.domain.database as database

    yield _app

    # Wipe all rows + reset identities so tests are order-independent.
    engine = database.get_engine()
    from vms.domain.models import Base
    tables = ", ".join(f'"{t.name}"' for t in reversed(Base.metadata.sorted_tables))
    if tables:
        with engine.begin() as conn:
            conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture()
def app_ctx(app):
    """Push an application context (needed for security.py / current_app helpers)."""
    with app.app_context():
        yield app


@pytest.fixture()
def client(app):
    """Anonymous Flask test client."""
    return app.test_client()


@pytest.fixture()
def db_session(app):
    """A raw SQLAlchemy session for arranging fixtures / asserting persisted state."""
    import vms.domain.database as database
    with database.get_session() as s:
        yield s


@pytest.fixture()
def user(app):
    """Create an active user and return its (id, username, password)."""
    from vms.auth import User
    u = User.create(
        username="tester",
        password="Sup3r-Secret!",
        display_name="Test User",
        email="tester@example.com",
        is_active=True,
    )
    assert u is not None, "User.create failed"
    return {"id": u.id, "username": "tester", "password": "Sup3r-Secret!"}


@pytest.fixture()
def auth_client(app, user):
    """Test client with an authenticated flask-login session (bypasses the login form)."""
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["_user_id"] = str(user["id"])
        sess["_fresh"] = True
    return c


# --- Mocks for external boundaries -------------------------------------------

@pytest.fixture()
def no_libreoffice(tmp_path, mocker):
    """Stub the ODT->PDF subprocess so tests never shell out to LibreOffice.

    convert_to_pdf(odt_path, output_dir) -> path to the produced PDF (str).
    """
    fake_pdf = tmp_path / "out.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 fake")
    return mocker.patch("vms.infra.odt_processor.convert_to_pdf", return_value=str(fake_pdf))


@pytest.fixture()
def mock_kanboard(mocker):
    """Neutralise Kanboard at its single HTTP chokepoint (_make_request).

    Every public kanboard_client.* function funnels through _make_request, so
    patching it here isolates all outbound Kanboard traffic. Set .return_value
    in the test to shape the API response.
    """
    return mocker.patch("vms.clients.kanboard_client._make_request", autospec=True)


@pytest.fixture()
def mailbox(app):
    """Capture outbound emails without sending. Usage: with mailbox as outbox: ..."""
    from vms.app import mail
    return mail.record_messages()

"""
Seed tests that prove the harness is wired correctly.
Run first:  pytest tests/test_smoke.py -q
These double as canonical examples for the patterns in .claude/skills/testing/SKILL.md.
"""
import pytest


@pytest.mark.unit
def test_kms_secret_roundtrip():
    """Pure crypto, no app context, no DB."""
    from vms.infra.kms import encrypt_secret, decrypt_secret
    key = b"0" * 32
    token = encrypt_secret("hunter2", master_key=key)
    assert token != "hunter2"
    assert decrypt_secret(token, master_key=key) == "hunter2"


@pytest.mark.unit
def test_security_value_roundtrip(app_ctx):
    """security.py needs current_app (SECRET_KEY-derived Fernet in test mode)."""
    from vms.infra.security import encrypt_value, decrypt_value
    token = encrypt_value("smtp-password")
    assert token is not None
    assert decrypt_value(token) == "smtp-password"


@pytest.mark.route
def test_login_redirects_to_setup_when_no_users(client):
    """Ohne angelegten User leitet /login in den First-Run-Setup-Flow (auth.py:320)."""
    resp = client.get("/login", follow_redirects=False)
    assert resp.status_code == 302
    assert "/setup" in resp.headers["Location"]


@pytest.mark.route
def test_login_page_renders_with_users(client, user):
    resp = client.get("/login")
    assert resp.status_code == 200


@pytest.mark.route
def test_protected_route_redirects_anonymous(client):
    """A login_required route must bounce anonymous users to /login."""
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code in (301, 302)
    assert "/login" in resp.headers["Location"]


@pytest.mark.integration
def test_user_create_persists(app, db_session):
    from vms.auth import User
    from vms.domain.models import User as UserModel

    u = User.create("alice", "pw-Str0ng!", email="alice@example.com", is_active=True)
    assert u is not None

    row = db_session.query(UserModel).filter_by(username="alice").first()
    assert row is not None
    assert row.password_hash != "pw-Str0ng!"          # stored as bcrypt hash
    assert User.verify_password(row.password_hash, "pw-Str0ng!") is True


@pytest.mark.route
def test_authenticated_dashboard_access(auth_client):
    resp = auth_client.get("/")
    assert resp.status_code == 200


@pytest.mark.integration
def test_user_create_logs_the_reason_when_it_fails(app, db_session, caplog):
    """User.create schluckt jede Exception und gibt None zurück. Ohne Log sind
    doppelter Name, Constraint-Verstoß und DB-Ausfall nicht unterscheidbar."""
    import logging
    from vms.auth import User

    User.create("alice", "pw-Str0ng!", email="alice@example.com", is_active=True)

    with caplog.at_level(logging.ERROR, logger="auth"):
        doppelt = User.create("alice", "pw-Str0ng!", email="andere@example.com",
                              is_active=True)

    assert doppelt is None
    assert any("User.create" in r.message and r.exc_info for r in caplog.records), \
        "Fehlschlag von User.create wurde nicht mit Traceback protokolliert"

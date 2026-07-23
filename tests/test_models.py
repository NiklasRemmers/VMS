"""Tests für das models.py-Zustandsmodell rund um EmailCandidate.

Zwei Stellen im Fokus:

  * ``set_candidate_status`` (models.py:247-272) -- die einzige Stelle, die
    einen Statuswechsel mit der Pflege von ``contract_created`` koppelt.
  * ``EmailCandidate.to_dict`` (models.py:327-335) -- löst ``responsible`` auf
    einen Anzeigenamen fürs Frontend auf.

Nummernkreis-Logik (claim_sequential_number / release_sequential_number) ist
bereits vollständig in tests/test_sequential_number.py abgedeckt und wird hier
nicht dupliziert.
"""
import pytest


def _make_candidate(db_session, user_id, **overrides):
    from vms.domain.models import EmailCandidate
    defaults = dict(user_id=user_id, vorname_nachname="Erika Mustermann",
                    veranstaltungsname="Sommerfest", datum="2024-05-01",
                    status="processed", tags=[])
    defaults.update(overrides)
    c = EmailCandidate(**defaults)
    db_session.add(c)
    db_session.flush()
    return c.id


def _make_user(db_session, **overrides):
    from vms.domain.models import User
    defaults = dict(username="responsible", password_hash="x",
                    display_name="Verantwortliche Person", email="resp@example.com",
                    is_active=True)
    defaults.update(overrides)
    u = User(**defaults)
    db_session.add(u)
    db_session.flush()
    return u.id


# --------------------------------------------------------------------------
# set_candidate_status
# --------------------------------------------------------------------------

@pytest.mark.integration
def test_set_candidate_status_to_done_marks_contract_created(db_session, user):
    from vms.domain.models import set_candidate_status, EmailCandidate, CandidateStatus

    cid = _make_candidate(db_session, user["id"], status="processed")
    db_session.flush()

    row = set_candidate_status(db_session, cid, "done")

    assert row.status == CandidateStatus.DONE.value
    assert row.contract_created is True
    persisted = db_session.query(EmailCandidate).filter_by(id=cid).one()
    assert persisted.contract_created is True


@pytest.mark.integration
def test_set_candidate_status_to_processed_unmarks_contract_created(db_session, user):
    """Der 'Vertrag entfernen'-Pfad: von DONE zurück nach PROCESSED löscht das
    Flag wieder."""
    from vms.domain.models import set_candidate_status, CandidateStatus

    cid = _make_candidate(db_session, user["id"], status="done", contract_created=True)
    db_session.flush()

    row = set_candidate_status(db_session, cid, "processed")

    assert row.status == CandidateStatus.PROCESSED.value
    assert row.contract_created is False


@pytest.mark.integration
def test_set_candidate_status_to_other_transition_leaves_contract_created_untouched(db_session, user):
    """Ein Vertrag, der einmal erzeugt wurde, bleibt eine historische Tatsache
    -- eine Rückgabe darf das Flag nicht zurücksetzen."""
    from vms.domain.models import set_candidate_status, CandidateStatus

    cid = _make_candidate(db_session, user["id"], status="done", contract_created=True)
    db_session.flush()

    row = set_candidate_status(db_session, cid, "returned")

    assert row.status == CandidateStatus.RETURNED.value
    assert row.contract_created is True  # unberührt, nicht zurückgesetzt


@pytest.mark.integration
def test_set_candidate_status_invalid_status_raises_value_error(db_session, user):
    from vms.domain.models import set_candidate_status

    cid = _make_candidate(db_session, user["id"], status="processed")
    db_session.flush()

    with pytest.raises(ValueError):
        set_candidate_status(db_session, cid, "no-such-status")


@pytest.mark.integration
def test_set_candidate_status_missing_candidate_id_returns_none(db_session):
    from vms.domain.models import set_candidate_status

    result = set_candidate_status(db_session, 999999, "done")

    assert result is None


# --------------------------------------------------------------------------
# EmailCandidate.to_dict -- responsible_name
# --------------------------------------------------------------------------

@pytest.mark.integration
def test_to_dict_surfaces_responsible_display_name(db_session, user):
    uid = _make_user(db_session, username="verantwortlich",
                      display_name="Verantwortliche Person")
    cid = _make_candidate(db_session, user["id"], responsible_user_id=uid)
    db_session.flush()

    from vms.domain.models import EmailCandidate
    row = db_session.query(EmailCandidate).filter_by(id=cid).one()

    assert row.to_dict()["responsible_name"] == "Verantwortliche Person"


@pytest.mark.integration
def test_to_dict_falls_back_to_username_without_a_display_name(db_session, user):
    uid = _make_user(db_session, username="ohne_anzeigename", display_name=None,
                     email="ohne@example.com")
    cid = _make_candidate(db_session, user["id"], responsible_user_id=uid)
    db_session.flush()

    from vms.domain.models import EmailCandidate
    row = db_session.query(EmailCandidate).filter_by(id=cid).one()

    assert row.to_dict()["responsible_name"] == "ohne_anzeigename"


@pytest.mark.integration
def test_to_dict_responsible_name_is_none_when_unset(db_session, user):
    cid = _make_candidate(db_session, user["id"])
    db_session.flush()

    from vms.domain.models import EmailCandidate
    row = db_session.query(EmailCandidate).filter_by(id=cid).one()

    assert row.responsible_user_id is None
    assert row.to_dict()["responsible_name"] is None

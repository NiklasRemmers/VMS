"""Die Abbildung Kandidatenstatus -> Kanboard-Aktion.

reconcile_candidate ist die einzige Stelle, die den vollständigen Lebenszyklus
eines Vorgangs ausdrückt. Diese Tabelle wird hier gepinnt, damit ein neu
hinzugefügter Status auffällt, statt still gar keine Kanboard-Aktion auszulösen.
"""
import pytest

from vms.domain.models import CandidateStatus
import vms.clients.kanboard_client as kb


def _candidate(status, **overrides):
    c = dict(kanboard_task_id=4711, status=status, datum='01.05.2024')
    c.update(overrides)
    return c


@pytest.mark.unit
@pytest.mark.parametrize("status,erwartete_spalte", [
    (CandidateStatus.PROCESSED, kb.COLUMN_PROCESSED),
    (CandidateStatus.INVOICE_PENDING, kb.COLUMN_INVOICE),
])
def test_status_moves_task_to_its_column(mocker, status, erwartete_spalte):
    move = mocker.patch("vms.clients.kanboard_client.move_task")
    close = mocker.patch("vms.clients.kanboard_client.close_task")

    kb.reconcile_candidate(1, _candidate(status.value))

    move.assert_called_once_with(1, 4711, erwartete_spalte)
    assert close.call_count == 0


@pytest.mark.unit
@pytest.mark.parametrize("status", [CandidateStatus.RETURNED, CandidateStatus.INVOICED])
def test_terminal_status_closes_the_task(mocker, status):
    move = mocker.patch("vms.clients.kanboard_client.move_task")
    close = mocker.patch("vms.clients.kanboard_client.close_task")

    kb.reconcile_candidate(1, _candidate(status.value))

    close.assert_called_once_with(1, 4711)
    assert move.call_count == 0


@pytest.mark.unit
def test_done_before_the_loan_starts_stays_in_the_preparation_column(mocker):
    move = mocker.patch("vms.clients.kanboard_client.move_task")
    mocker.patch("vms.clients.kanboard_client._today", return_value=__import__('datetime').date(2024, 4, 1))

    kb.reconcile_candidate(1, _candidate(CandidateStatus.DONE.value, datum='01.05.2024'))

    move.assert_called_once_with(1, 4711, kb.COLUMN_DONE)


@pytest.mark.unit
def test_done_on_or_after_the_start_date_moves_to_verliehen(mocker):
    move = mocker.patch("vms.clients.kanboard_client.move_task")
    mocker.patch("vms.clients.kanboard_client._today", return_value=__import__('datetime').date(2024, 5, 1))

    kb.reconcile_candidate(1, _candidate(CandidateStatus.DONE.value, datum='01.05.2024'))

    move.assert_called_once_with(1, 4711, kb.COLUMN_VERLIEHEN)


@pytest.mark.unit
def test_pending_triggers_no_kanboard_action(mocker):
    move = mocker.patch("vms.clients.kanboard_client.move_task")
    close = mocker.patch("vms.clients.kanboard_client.close_task")

    kb.reconcile_candidate(1, _candidate(CandidateStatus.PENDING.value))

    assert move.call_count == 0 and close.call_count == 0


@pytest.mark.unit
def test_candidate_without_task_id_is_ignored(mocker):
    move = mocker.patch("vms.clients.kanboard_client.move_task")

    kb.reconcile_candidate(1, _candidate(CandidateStatus.PROCESSED.value, kanboard_task_id=None))

    assert move.call_count == 0


@pytest.mark.unit
def test_every_status_is_covered_by_the_reconcile_table(mocker):
    """Wachhund: ein neuer CandidateStatus muss hier bewusst eingeordnet werden.
    'pending' ist der einzige Wert, der absichtlich nichts auslöst."""
    move = mocker.patch("vms.clients.kanboard_client.move_task")
    close = mocker.patch("vms.clients.kanboard_client.close_task")

    ohne_aktion = []
    for status in CandidateStatus:
        move.reset_mock(); close.reset_mock()
        kb.reconcile_candidate(1, _candidate(status.value))
        if move.call_count == 0 and close.call_count == 0:
            ohne_aktion.append(status.value)

    assert ohne_aktion == [CandidateStatus.PENDING.value], \
        f"Status ohne Kanboard-Abgleich: {ohne_aktion}"

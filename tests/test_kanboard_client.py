"""Tests for kanboard_client.py.

Covers, roughly highest-risk first:
  1. parse_description (pure parsing of Kanboard task descriptions)
  2. _format_date_with_time (pure date normalization)
  3. _make_request (the single HTTP chokepoint; auth/config/error branches)
  4. get_column_id_by_name (name resolution incl. case/missing-title)
  5. multi-call flows (get_leihanfragen_tasks, get_tasks_by_column, get_task_tags,
     create_task, update_task, get_all_tags) via mock_kanboard side_effect lists
  6. lifecycle (move_task, close_task, reconcile_candidate/_by_id) incl. the
     freeze_time boundary for the daily reconcile job

All Kanboard-facing tests go through the `mock_kanboard` fixture (patches
kanboard_client._make_request), never through requests/network. `_make_request`
itself is the one exception: to exercise it we patch its own dependencies
(get_user_settings, decrypt_value, requests.post) instead.
"""
import logging

import pytest
import requests
from freezegun import freeze_time

import vms.clients.kanboard_client as kb
from vms.domain.models import CandidateStatus


# ---------------------------------------------------------------------------
# 1. parse_description (pure)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_parse_description_empty_string_returns_empty_dict():
    assert kb.parse_description("") == {}


@pytest.mark.unit
def test_parse_description_none_returns_empty_dict():
    assert kb.parse_description(None) == {}


@pytest.mark.unit
def test_parse_description_line_without_colon_is_skipped():
    assert kb.parse_description("Formularanfrage ohne Doppelpunkt") == {}


@pytest.mark.unit
def test_parse_description_single_field_is_mapped():
    result = kb.parse_description("Anschrift: Musterstr. 1, 12345 Musterstadt")

    assert result == {"rechnungsanschrift": "Musterstr. 1, 12345 Musterstadt"}


@pytest.mark.unit
def test_parse_description_label_matching_is_case_insensitive():
    result = kb.parse_description("DATUM: 2026-01-01")

    assert result == {"veranstaltungsdatum": "2026-01-01"}


@pytest.mark.unit
def test_parse_description_empty_value_is_retained_not_dropped():
    result = kb.parse_description("Telefon:")

    assert result == {"telefon": ""}


@pytest.mark.unit
def test_parse_description_value_with_extra_colon_kept_via_split_once():
    result = kb.parse_description("Telefon: 030:12345")

    assert result == {"telefon": "030:12345"}


@pytest.mark.unit
def test_parse_description_unmatched_label_is_absent_from_result():
    result = kb.parse_description("Irgendein Feld: Wert")

    assert result == {}


@pytest.mark.unit
def test_parse_description_multiline_mixed_heading_and_fields():
    description = (
        "Formularanfrage ueber die Webseite\n"
        "Vor- und Nachname: Max Mustermann\n"
        "eine Zeile ohne Doppelpunkt\n"
        "E-Mail: max@example.com\n"
        "Erwartete Personenzahl: 42\n"
    )

    result = kb.parse_description(description)

    assert result == {
        "vorname_nachname": "Max Mustermann",
        "email_address": "max@example.com",
        "personenzahl": "42",
    }


# NOTE: kanboard_client.py:136 `if len(parts) == 2:` is an unreachable false
# branch: `line.split(':', 1)` on a string that (per the outer `if ':' in
# line:` guard) is known to contain ':' always yields exactly 2 parts, never
# fewer. No test exercises the false side; a contrived input can't produce it.


# ---------------------------------------------------------------------------
# 1b. build_task_description (pure) -- inverse of parse_description; used to push
#     the VMS truth back into the Kanboard task's free-text description.
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_build_task_description_roundtrips_through_parse_description():
    fields = {
        'vorname_nachname': 'Max Mustermann',
        'anschrift': 'Musterstr. 1, 12345 Musterstadt',
        'email_address': 'max@example.com',
        'telefon': '030 12345',
        'veranstaltungsname': 'Sommerfest',
        'veranstaltungsart': 'Fest',
        'veranstaltungsort': 'Marktplatz',
        'veranstaltungsbereich': 'Aussen',
        'personenzahl': '42',
        'material': 'Zelt, Bänke',
        'sonstiges': 'Bitte früh liefern',
        'rahmenbedingungen': 'Kein Aufbau vor 8 Uhr',
    }

    parsed = kb.parse_description(kb.build_task_description(fields))

    assert parsed == {
        'vorname_nachname': 'Max Mustermann',
        'rechnungsanschrift': 'Musterstr. 1, 12345 Musterstadt',
        'email_address': 'max@example.com',
        'telefon': '030 12345',
        'veranstaltungsname': 'Sommerfest',
        'veranstaltungsart': 'Fest',
        'veranstaltungsort': 'Marktplatz',
        'veranstaltungsbereich': 'Aussen',
        'personenzahl': '42',
        'material': 'Zelt, Bänke',
        'sonstiges': 'Bitte früh liefern',
        'rahmenbedingungen': 'Kein Aufbau vor 8 Uhr',
    }


@pytest.mark.unit
def test_build_task_description_omits_empty_fields():
    fields = {
        'vorname_nachname': 'Nur Name',
        'email_address': '',
        'telefon': None,
        'veranstaltungsname': 'Fest',
    }

    text = kb.build_task_description(fields)

    assert 'Vor- und Nachname: Nur Name' in text
    assert 'Name der Veranstaltung: Fest' in text
    assert 'E-Mail' not in text
    assert 'Telefon' not in text


# ---------------------------------------------------------------------------
# 1c. kanboard_date_due_to_iso (pure) -- Kanboard Unix-timestamp -> ISO date,
#     the conversion the sync uses on the create path and for diff detection.
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_kanboard_date_due_to_iso_converts_timestamp():
    from datetime import datetime, timezone
    ts = int(datetime(2024, 6, 15, 12, 0, tzinfo=timezone.utc).timestamp())

    assert kb.kanboard_date_due_to_iso(str(ts)) == '2024-06-15'


@pytest.mark.unit
@pytest.mark.parametrize("empty_value", ['', '0', None])
def test_kanboard_date_due_to_iso_empty_returns_blank(empty_value):
    assert kb.kanboard_date_due_to_iso(empty_value) == ''


@pytest.mark.unit
def test_kanboard_date_due_to_iso_unparseable_falls_back_to_iso_normalizer():
    # Non-numeric, non-date garbage: to_iso_date returns it verbatim.
    assert kb.kanboard_date_due_to_iso('nicht-numerisch') == 'nicht-numerisch'


# ---------------------------------------------------------------------------
# 1d. plan_task_push (pure) -- compares the VMS truth against the current
#     Kanboard task; returns the update_task kwargs to push, or None if in sync.
# ---------------------------------------------------------------------------

def _synced_pair():
    """A candidate dict and a task dict that already agree on every field."""
    candidate = {
        'veranstaltungsname': 'Sommerfest', 'subject': 'Sommerfest',
        'vorname_nachname': 'Max Mustermann', 'email_address': 'max@example.com',
        'anschrift': None, 'telefon': None, 'veranstaltungsart': None,
        'veranstaltungsort': None, 'veranstaltungsbereich': None,
        'personenzahl': None, 'material': None, 'sonstiges': None,
        'rahmenbedingungen': None, 'datum': '2024-06-15', 'tags': ['vip'],
    }
    task = {
        'id': '1', 'title': 'Sommerfest',
        'description': kb.build_task_description(candidate),
        'date_due': '2024-06-15', 'tags': ['vip'], 'parsed_data': {},
    }
    return candidate, task


@pytest.mark.unit
def test_plan_task_push_returns_none_when_in_sync():
    candidate, task = _synced_pair()

    assert kb.plan_task_push(candidate, task) is None


@pytest.mark.unit
def test_plan_task_push_detects_divergent_description_field():
    candidate, task = _synced_pair()
    candidate['email_address'] = 'neu@example.com'  # changed only in VMS

    plan = kb.plan_task_push(candidate, task)

    assert plan is not None
    assert 'E-Mail: neu@example.com' in plan['description']
    assert plan['title'] == 'Sommerfest'
    assert plan['tags'] == ['vip']


@pytest.mark.unit
def test_plan_task_push_ignores_tag_order():
    candidate, task = _synced_pair()
    candidate['tags'] = ['vip', 'gross']
    task['tags'] = ['gross', 'vip']

    assert kb.plan_task_push(candidate, task) is None


@pytest.mark.unit
def test_plan_task_push_empty_vms_datum_does_not_trigger_date_push():
    candidate, task = _synced_pair()
    candidate['datum'] = ''            # VMS has no date
    task['date_due'] = '2024-06-15'    # Kanboard still has one -> must NOT clear

    assert kb.plan_task_push(candidate, task) is None


# ---------------------------------------------------------------------------
# 2. _format_date_with_time (pure)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_format_date_with_time_iso_date():
    assert kb._format_date_with_time("2026-07-22") == "2026-07-22 00:00"


@pytest.mark.unit
def test_format_date_with_time_german_date():
    assert kb._format_date_with_time("22.07.2026") == "2026-07-22 00:00"


@pytest.mark.unit
def test_format_date_with_time_short_german_date():
    assert kb._format_date_with_time("22.07.26") == "2026-07-22 00:00"


@pytest.mark.unit
def test_format_date_with_time_range_shorthand_uses_start_day():
    assert kb._format_date_with_time("13.-15.11.26") == "2026-11-13 00:00"


@pytest.mark.unit
def test_format_date_with_time_collapses_existing_time_component_to_midnight():
    assert kb._format_date_with_time("18.07.2026, 14:00 Uhr") == "2026-07-18 00:00"


@pytest.mark.unit
def test_format_date_with_time_unparseable_truthy_input_returns_none_and_warns(caplog):
    with caplog.at_level(logging.WARNING, logger="kanboard_client"):
        result = kb._format_date_with_time("not a date")

    assert result is None
    assert "nicht interpretierbar" in caplog.text


@pytest.mark.unit
@pytest.mark.parametrize("empty_value", ["", None])
def test_format_date_with_time_falsy_input_returns_none_without_warning(caplog, empty_value):
    with caplog.at_level(logging.WARNING, logger="kanboard_client"):
        result = kb._format_date_with_time(empty_value)

    assert result is None
    assert caplog.records == []


@pytest.mark.unit
def test_format_date_with_time_invalid_calendar_day_returns_none():
    assert kb._format_date_with_time("32.13.2026") is None


# ---------------------------------------------------------------------------
# 3. _make_request (patches its own dependencies, not mock_kanboard)
# ---------------------------------------------------------------------------

def _valid_settings():
    return {
        "kanboard_url": "https://kb.example.com/jsonrpc.php",
        "kanboard_user": "svc-account",
        "encrypted_kanboard_token": "enc-token",
        "kanboard_project_id": None,
    }


@pytest.mark.unit
def test_make_request_no_settings_raises_value_error(mocker):
    mocker.patch("vms.clients.kanboard_client.get_user_settings", return_value=None)

    with pytest.raises(ValueError, match="Keine Kanboard-Einstellungen"):
        kb._make_request(1, "getColumns")


@pytest.mark.unit
@pytest.mark.parametrize(
    "missing_field", ["kanboard_url", "kanboard_user", "encrypted_kanboard_token"]
)
def test_make_request_incomplete_config_raises_value_error(mocker, missing_field):
    settings = _valid_settings()
    settings[missing_field] = None
    mocker.patch("vms.clients.kanboard_client.get_user_settings", return_value=settings)
    # identity passthrough, so a None encrypted_kanboard_token surfaces as a falsy token
    mocker.patch("vms.clients.kanboard_client.decrypt_value", side_effect=lambda v: v)

    with pytest.raises(ValueError, match="unvollständig"):
        kb._make_request(1, "getColumns")


@pytest.mark.unit
def test_make_request_success_returns_result_field_and_posts_expected_payload(mocker):
    mocker.patch("vms.clients.kanboard_client.get_user_settings", return_value=_valid_settings())
    mocker.patch("vms.clients.kanboard_client.decrypt_value", return_value="plain-token")
    response = mocker.Mock()
    response.json.return_value = {"jsonrpc": "2.0", "id": 1, "result": [{"id": 1}]}
    post = mocker.patch("vms.clients.kanboard_client.requests.post", return_value=response)

    result = kb._make_request(7, "getColumns", {"project_id": 25})

    assert result == [{"id": 1}]
    response.raise_for_status.assert_called_once()
    _, kwargs = post.call_args
    assert kwargs["json"]["method"] == "getColumns"
    assert kwargs["json"]["params"] == {"project_id": 25}
    assert kwargs["auth"] == ("svc-account", "plain-token")


@pytest.mark.unit
def test_make_request_none_params_posted_as_empty_dict(mocker):
    mocker.patch("vms.clients.kanboard_client.get_user_settings", return_value=_valid_settings())
    mocker.patch("vms.clients.kanboard_client.decrypt_value", return_value="plain-token")
    response = mocker.Mock()
    response.json.return_value = {"result": None}
    post = mocker.patch("vms.clients.kanboard_client.requests.post", return_value=response)

    result = kb._make_request(7, "getAllTasks", None)

    assert result is None
    assert post.call_args.kwargs["json"]["params"] == {}


@pytest.mark.unit
def test_make_request_api_error_with_message_raises_with_that_message(mocker):
    mocker.patch("vms.clients.kanboard_client.get_user_settings", return_value=_valid_settings())
    mocker.patch("vms.clients.kanboard_client.decrypt_value", return_value="plain-token")
    response = mocker.Mock()
    response.json.return_value = {"error": {"message": "boom"}}
    mocker.patch("vms.clients.kanboard_client.requests.post", return_value=response)

    with pytest.raises(Exception, match="boom"):
        kb._make_request(7, "getColumns")


@pytest.mark.unit
def test_make_request_api_error_without_message_uses_default_text(mocker):
    mocker.patch("vms.clients.kanboard_client.get_user_settings", return_value=_valid_settings())
    mocker.patch("vms.clients.kanboard_client.decrypt_value", return_value="plain-token")
    response = mocker.Mock()
    response.json.return_value = {"error": {}}
    mocker.patch("vms.clients.kanboard_client.requests.post", return_value=response)

    with pytest.raises(Exception, match="Unknown API error"):
        kb._make_request(7, "getColumns")


@pytest.mark.unit
def test_make_request_connection_error_is_wrapped(mocker):
    mocker.patch("vms.clients.kanboard_client.get_user_settings", return_value=_valid_settings())
    mocker.patch("vms.clients.kanboard_client.decrypt_value", return_value="plain-token")
    mocker.patch(
        "vms.clients.kanboard_client.requests.post",
        side_effect=requests.ConnectionError("refused"),
    )

    with pytest.raises(Exception, match="Kanboard Connection Error"):
        kb._make_request(7, "getColumns")


@pytest.mark.unit
def test_make_request_non_json_body_is_also_wrapped_as_connection_error(mocker):
    """response.json() on a real requests.Response raises requests.exceptions.
    JSONDecodeError for a non-JSON body, and that class *is* a RequestException
    subclass -- so it is caught by the existing `except requests.RequestException`
    handler, not an uncaught crash. This test pins that (correct) behaviour."""
    mocker.patch("vms.clients.kanboard_client.get_user_settings", return_value=_valid_settings())
    mocker.patch("vms.clients.kanboard_client.decrypt_value", return_value="plain-token")
    response = mocker.Mock()
    response.json.side_effect = requests.exceptions.JSONDecodeError("Expecting value", "not json", 0)
    mocker.patch("vms.clients.kanboard_client.requests.post", return_value=response)

    with pytest.raises(Exception, match="Kanboard Connection Error"):
        kb._make_request(7, "getColumns")


# ---------------------------------------------------------------------------
# 3b. get_project_id
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_get_project_id_returns_default_when_no_settings(mocker):
    mocker.patch("vms.clients.kanboard_client.get_user_settings", return_value=None)

    assert kb.get_project_id(1) == kb.DEFAULT_PROJECT_ID


@pytest.mark.unit
def test_get_project_id_returns_default_when_settings_have_no_project_id(mocker):
    mocker.patch(
        "vms.clients.kanboard_client.get_user_settings",
        return_value={"kanboard_project_id": None},
    )

    assert kb.get_project_id(1) == kb.DEFAULT_PROJECT_ID


@pytest.mark.unit
def test_get_project_id_returns_configured_project_id(mocker):
    mocker.patch(
        "vms.clients.kanboard_client.get_user_settings",
        return_value={"kanboard_project_id": 42},
    )

    assert kb.get_project_id(1) == 42


# ---------------------------------------------------------------------------
# 4. get_column_id_by_name (mock_kanboard, single call)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_get_column_id_by_name_matches_case_insensitively_and_returns_int(mock_kanboard):
    mock_kanboard.return_value = [
        {"id": "3", "title": "Leihanfrage"},
        {"id": "5", "title": "Verliehen"},
    ]

    result = kb.get_column_id_by_name(1, "LEIHANFRAGE", project_id=25)

    assert result == 3
    assert isinstance(result, int)


@pytest.mark.unit
def test_get_column_id_by_name_no_match_returns_none(mock_kanboard):
    mock_kanboard.return_value = [{"id": "3", "title": "Leihanfrage"}]

    assert kb.get_column_id_by_name(1, "Nichtvorhanden", project_id=25) is None


@pytest.mark.unit
def test_get_column_id_by_name_column_missing_title_is_not_a_false_match(mock_kanboard):
    mock_kanboard.return_value = [{"id": "3"}]  # no 'title' key at all

    assert kb.get_column_id_by_name(1, "Leihanfrage", project_id=25) is None


# ---------------------------------------------------------------------------
# 5a. get_tasks_by_column / get_task_tags
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_get_tasks_by_column_filters_to_the_requested_column(mock_kanboard):
    mock_kanboard.return_value = [
        {"id": 1, "column_id": "9"},
        {"id": 2, "column_id": "10"},
        {"id": 3, "column_id": 9},
    ]

    result = kb.get_tasks_by_column(1, 9, project_id=25)

    assert [t["id"] for t in result] == [1, 3]


@pytest.mark.unit
def test_get_task_tags_normalizes_dict_response_to_list(mock_kanboard):
    mock_kanboard.return_value = {"3": "dringend", "7": "musik"}

    result = kb.get_task_tags(1, 55)

    assert sorted(result) == ["dringend", "musik"]


@pytest.mark.unit
def test_get_task_tags_falsy_response_returns_empty_list(mock_kanboard):
    mock_kanboard.return_value = None

    assert kb.get_task_tags(1, 55) == []


# ---------------------------------------------------------------------------
# 5b. get_leihanfragen_tasks (getColumns -> getAllTasks -> getTaskTags)
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_get_leihanfragen_tasks_returns_enriched_shape_with_parsed_data_and_tags(mock_kanboard):
    columns = [{"id": "9", "title": "Leihanfrage"}]
    tasks = [
        {
            "id": 101,
            "title": "Anfrage A",
            "description": "Telefon: 0123456",
            "date_due": "2026-01-01 00:00",
            "column_id": "9",
        }
    ]
    tags_response = {"1": "dringend", "2": "musik"}
    mock_kanboard.side_effect = [columns, tasks, tags_response]

    result = kb.get_leihanfragen_tasks(1, project_id=25)

    assert len(result) == 1
    task = result[0]
    assert task["id"] == 101
    assert task["title"] == "Anfrage A"
    assert task["parsed_data"] == {"telefon": "0123456"}
    assert sorted(task["tags"]) == ["dringend", "musik"]


@pytest.mark.unit
def test_get_leihanfragen_tasks_returns_task_without_tags_when_tag_fetch_fails(mock_kanboard):
    columns = [{"id": "9", "title": "Leihanfrage"}]
    tasks = [{"id": 101, "title": "A", "description": "", "date_due": "", "column_id": "9"}]
    mock_kanboard.side_effect = [columns, tasks, Exception("Kanboard nicht erreichbar")]

    result = kb.get_leihanfragen_tasks(1, project_id=25)

    assert len(result) == 1
    assert result[0]["tags"] == []


@pytest.mark.unit
def test_get_leihanfragen_tasks_no_matching_column_returns_empty_list(mock_kanboard):
    mock_kanboard.return_value = []  # getColumns: nothing named 'Leihanfrage'

    assert kb.get_leihanfragen_tasks(1, project_id=25) == []


# ---------------------------------------------------------------------------
# 5c. create_task / update_task / get_all_tags
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_create_task_without_due_date_or_tags(mock_kanboard):
    mock_kanboard.side_effect = [
        [{"id": "9", "title": "Leihanfrage"}],  # getColumns
        4242,  # createTask -> new task id
    ]

    result = kb.create_task(1, "Titel", "Beschreibung", project_id=25)

    assert result == {"id": 4242, "title": "Titel"}
    assert mock_kanboard.call_count == 2
    _, method, params = mock_kanboard.call_args_list[1].args
    assert method == "createTask"
    assert "date_due" not in params
    assert "date_started" not in params


@pytest.mark.unit
def test_create_task_with_due_date_sets_due_and_started_fields(mock_kanboard):
    mock_kanboard.side_effect = [
        [{"id": "9", "title": "Leihanfrage"}],
        4242,
    ]

    kb.create_task(1, "Titel", "Beschreibung", due_date="22.07.2026", project_id=25)

    _, _, params = mock_kanboard.call_args_list[1].args
    assert params["date_due"] == "2026-07-22 00:00"
    assert params["date_started"] == "2026-07-22 00:00"


@pytest.mark.unit
def test_create_task_unresolvable_column_raises(mock_kanboard):
    mock_kanboard.return_value = []  # no columns at all

    with pytest.raises(Exception, match="nicht gefunden"):
        kb.create_task(1, "Titel", "Beschreibung", column_name="Nichtvorhanden", project_id=25)


@pytest.mark.unit
def test_create_task_no_task_id_from_api_raises(mock_kanboard):
    mock_kanboard.side_effect = [
        [{"id": "9", "title": "Leihanfrage"}],
        None,  # createTask returned nothing
    ]

    with pytest.raises(Exception, match="konnte nicht erstellt werden"):
        kb.create_task(1, "Titel", "Beschreibung", project_id=25)


@pytest.mark.unit
def test_create_task_with_tags_calls_set_task_tags(mock_kanboard):
    mock_kanboard.side_effect = [
        [{"id": "9", "title": "Leihanfrage"}],
        4242,
        None,  # setTaskTags result
    ]

    result = kb.create_task(1, "Titel", "Beschreibung", tags=["a", "b"], project_id=25)

    assert result == {"id": 4242, "title": "Titel"}
    _, method, params = mock_kanboard.call_args_list[2].args
    assert method == "setTaskTags"
    assert params["tags"] == ["a", "b"]


@pytest.mark.unit
def test_create_task_set_task_tags_failure_is_swallowed_task_still_returned(mock_kanboard):
    mock_kanboard.side_effect = [
        [{"id": "9", "title": "Leihanfrage"}],
        4242,
        Exception("tag service down"),
    ]

    result = kb.create_task(1, "Titel", "Beschreibung", tags=["a"], project_id=25)

    assert result == {"id": 4242, "title": "Titel"}


@pytest.mark.unit
def test_update_task_updates_title_and_description(mock_kanboard):
    mock_kanboard.return_value = True

    result = kb.update_task(1, 55, title="Neuer Titel", description="Neu", project_id=25)

    assert result is True
    _, method, params = mock_kanboard.call_args.args
    assert method == "updateTask"
    assert params == {"id": 55, "title": "Neuer Titel", "description": "Neu"}


@pytest.mark.unit
def test_update_task_with_no_field_changes_still_sends_id_only_update(mock_kanboard):
    # NOTE: `params` always contains 'id' because task_id is a required, always
    # non-None argument -- so kanboard_client.py:260 `if params:` is always true
    # in practice; the empty-params skip is unreachable via the public API.
    mock_kanboard.return_value = True

    result = kb.update_task(1, 55, project_id=25)

    assert result is True
    assert mock_kanboard.call_count == 1
    _, method, params = mock_kanboard.call_args.args
    assert method == "updateTask"
    assert params == {"id": 55}


@pytest.mark.unit
def test_update_task_returns_false_when_update_call_fails(mock_kanboard):
    mock_kanboard.return_value = False

    result = kb.update_task(1, 55, title="X", project_id=25)

    assert result is False


@pytest.mark.unit
def test_update_task_invalid_due_date_is_ignored(mock_kanboard):
    mock_kanboard.return_value = True

    kb.update_task(1, 55, title="X", due_date="not a date", project_id=25)

    _, _, params = mock_kanboard.call_args.args
    assert "date_due" not in params


@pytest.mark.unit
def test_update_task_valid_due_date_is_included(mock_kanboard):
    mock_kanboard.return_value = True

    kb.update_task(1, 55, title="X", due_date="22.07.2026", project_id=25)

    _, _, params = mock_kanboard.call_args.args
    assert params["date_due"] == "2026-07-22 00:00"


@pytest.mark.unit
def test_update_task_sets_tags_alongside_the_forced_id_only_update(mock_kanboard):
    mock_kanboard.side_effect = [True, None]  # updateTask, then setTaskTags

    result = kb.update_task(1, 55, tags=["x"], project_id=25)

    assert result is True
    assert mock_kanboard.call_count == 2
    _, method, params = mock_kanboard.call_args_list[1].args
    assert method == "setTaskTags"
    assert params["tags"] == ["x"]


@pytest.mark.unit
def test_update_task_set_task_tags_failure_is_swallowed(mock_kanboard):
    mock_kanboard.side_effect = [True, Exception("boom")]

    result = kb.update_task(1, 55, title="X", tags=["x"], project_id=25)

    assert result is True


@pytest.mark.unit
def test_get_all_tags_returns_names_only_skipping_untitled_entries(mock_kanboard):
    mock_kanboard.return_value = [{"name": "dringend"}, {"name": ""}, {"id": 3}]

    result = kb.get_all_tags(1, project_id=25)

    assert result == ["dringend"]


@pytest.mark.unit
def test_get_all_tags_non_list_response_returns_empty_list(mock_kanboard):
    mock_kanboard.return_value = {"unexpected": "shape"}

    assert kb.get_all_tags(1, project_id=25) == []


@pytest.mark.unit
def test_get_all_tags_swallows_api_error_and_returns_empty_list(mock_kanboard):
    mock_kanboard.side_effect = Exception("Kanboard nicht erreichbar")

    assert kb.get_all_tags(1, project_id=25) == []


@pytest.mark.unit
def test_get_task_details_returns_parsed_data_and_tags(mock_kanboard):
    task = {"id": 5, "title": "Anfrage", "description": "Telefon: 555"}
    mock_kanboard.side_effect = [task, {"1": "dringend"}]

    result = kb.get_task_details(1, 5)

    assert result["id"] == 5
    assert result["parsed_data"] == {"telefon": "555"}
    assert result["tags"] == ["dringend"]


@pytest.mark.unit
def test_get_task_details_missing_task_raises(mock_kanboard):
    mock_kanboard.return_value = None

    with pytest.raises(Exception, match="nicht gefunden"):
        kb.get_task_details(1, 5)


# ---------------------------------------------------------------------------
# 6a. _today / move_task / close_task
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_today_falls_back_to_naive_now_when_zoneinfo_unavailable(mocker):
    """Covers the fallback branch (kanboard_client.py:304) that only runs when
    zoneinfo/tzdata is unavailable at import time -- simulated here since a real
    tzdata-less environment can't be produced from inside the test."""
    mocker.patch("vms.clients.kanboard_client.LOCAL_TZ", None)
    with freeze_time("2026-07-22 10:00:00"):
        result = kb._today()
        expected = kb.datetime.now().date()

    assert result == expected
    assert result == kb.datetime(2026, 7, 22).date()




@pytest.mark.unit
def test_move_task_moves_when_column_differs(mock_kanboard):
    mock_kanboard.side_effect = [
        {"id": 55, "column_id": "3", "swimlane_id": "2"},  # getTask
        [{"id": "9", "title": "Verliehen"}],                # getColumns
        None,                                                # moveTaskPosition
    ]

    result = kb.move_task(1, 55, "Verliehen", project_id=25)

    assert result is True
    assert mock_kanboard.call_count == 3
    _, method, params = mock_kanboard.call_args_list[2].args
    assert method == "moveTaskPosition"
    assert params["column_id"] == 9
    assert params["swimlane_id"] == 2


@pytest.mark.unit
def test_move_task_is_a_noop_when_already_in_target_column(mock_kanboard):
    mock_kanboard.side_effect = [
        {"id": 55, "column_id": "9"},          # getTask
        [{"id": "9", "title": "Verliehen"}],    # getColumns
    ]

    result = kb.move_task(1, 55, "Verliehen", project_id=25)

    assert result is True
    assert mock_kanboard.call_count == 2  # no moveTaskPosition call made


@pytest.mark.unit
def test_move_task_returns_false_when_task_not_found(mock_kanboard):
    mock_kanboard.return_value = None

    assert kb.move_task(1, 55, "Verliehen", project_id=25) is False


@pytest.mark.unit
def test_move_task_returns_false_when_target_column_cannot_be_resolved(mock_kanboard):
    mock_kanboard.side_effect = [
        {"id": 55, "column_id": "3"},
        [],  # no column named "Nichtvorhanden"
    ]

    assert kb.move_task(1, 55, "Nichtvorhanden", project_id=25) is False


@pytest.mark.unit
def test_close_task_closes_when_open(mock_kanboard):
    mock_kanboard.side_effect = [
        {"id": 55, "is_active": 1},
        True,
    ]

    assert kb.close_task(1, 55) is True
    assert mock_kanboard.call_count == 2


@pytest.mark.unit
def test_close_task_is_a_noop_when_already_closed(mock_kanboard):
    mock_kanboard.return_value = {"id": 55, "is_active": 0}

    assert kb.close_task(1, 55) is True
    assert mock_kanboard.call_count == 1  # only getTask, no closeTask call


@pytest.mark.unit
def test_close_task_returns_false_when_task_not_found(mock_kanboard):
    mock_kanboard.return_value = None

    assert kb.close_task(1, 55) is False


# ---------------------------------------------------------------------------
# 6b. reconcile_candidate / reconcile_candidate_by_id
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_reconcile_candidate_boundary_is_inclusive_start_date_is_today_moves_to_verliehen(
    mocker, mock_kanboard
):
    mocker.patch("vms.clients.kanboard_client.get_project_id", return_value=25)
    candidate = {"kanboard_task_id": 77, "status": CandidateStatus.DONE.value, "datum": "22.07.2026"}
    mock_kanboard.side_effect = [
        {"id": 77, "column_id": "3"},                     # getTask
        [{"id": "9", "title": kb.COLUMN_VERLIEHEN}],       # getColumns
        None,                                               # moveTaskPosition
    ]

    with freeze_time("2026-07-22 10:00:00"):
        kb.reconcile_candidate(1, candidate)

    _, method, params = mock_kanboard.call_args_list[2].args
    assert method == "moveTaskPosition"
    assert params["column_id"] == 9


@pytest.mark.unit
def test_reconcile_candidate_stays_in_done_column_before_the_start_date(mocker, mock_kanboard):
    mocker.patch("vms.clients.kanboard_client.get_project_id", return_value=25)
    candidate = {"kanboard_task_id": 77, "status": CandidateStatus.DONE.value, "datum": "23.07.2026"}
    mock_kanboard.side_effect = [
        {"id": 77, "column_id": "3"},
        [{"id": "9", "title": kb.COLUMN_DONE}],
        None,
    ]

    with freeze_time("2026-07-22 10:00:00"):
        kb.reconcile_candidate(1, candidate)

    _, method, params = mock_kanboard.call_args_list[2].args
    assert method == "moveTaskPosition"
    assert params["column_id"] == 9  # resolved via COLUMN_DONE, not COLUMN_VERLIEHEN


@pytest.mark.unit
def test_reconcile_candidate_swallows_kanboard_errors_best_effort(mocker, mock_kanboard):
    mocker.patch("vms.clients.kanboard_client.get_project_id", return_value=25)
    mock_kanboard.side_effect = Exception("Kanboard nicht erreichbar")
    candidate = {"kanboard_task_id": 77, "status": CandidateStatus.PROCESSED.value}

    kb.reconcile_candidate(1, candidate)  # must not raise


@pytest.mark.unit
def test_reconcile_candidate_pending_status_triggers_no_kanboard_action(mocker, mock_kanboard):
    mocker.patch("vms.clients.kanboard_client.get_project_id", return_value=25)
    candidate = {"kanboard_task_id": 77, "status": CandidateStatus.PENDING.value}

    kb.reconcile_candidate(1, candidate)

    assert mock_kanboard.call_count == 0


@pytest.mark.unit
def test_reconcile_candidate_without_task_id_is_ignored_no_kanboard_call(mock_kanboard):
    candidate = {"kanboard_task_id": None, "status": CandidateStatus.PROCESSED.value}

    kb.reconcile_candidate(1, candidate)

    assert mock_kanboard.call_count == 0


@pytest.mark.unit
@pytest.mark.parametrize(
    "status", [CandidateStatus.RETURNED.value, CandidateStatus.INVOICED.value]
)
def test_reconcile_candidate_terminal_status_closes_the_task(mocker, mock_kanboard, status):
    mocker.patch("vms.clients.kanboard_client.get_project_id", return_value=25)
    mock_kanboard.side_effect = [
        {"id": 77, "is_active": 1},  # getTask (close_task)
        True,                        # closeTask
    ]
    candidate = {"kanboard_task_id": 77, "status": status}

    kb.reconcile_candidate(1, candidate)

    _, method, _ = mock_kanboard.call_args_list[1].args
    assert method == "closeTask"


@pytest.mark.unit
def test_reconcile_candidate_invoice_pending_moves_to_invoice_column(mocker, mock_kanboard):
    mocker.patch("vms.clients.kanboard_client.get_project_id", return_value=25)
    mock_kanboard.side_effect = [
        {"id": 77, "column_id": "3"},                     # getTask
        [{"id": "12", "title": kb.COLUMN_INVOICE}],        # getColumns
        None,                                               # moveTaskPosition
    ]
    candidate = {"kanboard_task_id": 77, "status": CandidateStatus.INVOICE_PENDING.value}

    kb.reconcile_candidate(1, candidate)

    _, method, params = mock_kanboard.call_args_list[2].args
    assert method == "moveTaskPosition"
    assert params["column_id"] == 12


@pytest.mark.integration
def test_reconcile_candidate_by_id_loads_the_candidate_and_reconciles_its_task(
    app, db_session, user, mocker, mock_kanboard
):
    from vms.domain.models import EmailCandidate

    candidate = EmailCandidate(
        user_id=user["id"],
        email_id="msg-reconcile-1",
        status=CandidateStatus.PROCESSED.value,
        kanboard_task_id=99,
    )
    db_session.add(candidate)
    db_session.commit()
    candidate_id = candidate.id

    mocker.patch("vms.clients.kanboard_client.get_project_id", return_value=25)
    mock_kanboard.side_effect = [
        {"id": 99, "column_id": "3"},
        [{"id": "9", "title": kb.COLUMN_PROCESSED}],
        None,
    ]

    kb.reconcile_candidate_by_id(user["id"], candidate_id)

    _, method, params = mock_kanboard.call_args_list[2].args
    assert method == "moveTaskPosition"
    assert params["column_id"] == 9


@pytest.mark.integration
def test_reconcile_candidate_by_id_missing_candidate_is_a_noop(app, mock_kanboard):
    kb.reconcile_candidate_by_id(1, 999999)

    assert mock_kanboard.call_count == 0


@pytest.mark.integration
def test_reconcile_candidate_by_id_swallows_candidate_lookup_errors(app, mocker):
    mocker.patch("vms.clients.email_client.get_candidate_by_id", side_effect=Exception("db down"))

    kb.reconcile_candidate_by_id(1, 1)  # must not raise

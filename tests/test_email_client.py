"""Tests für email_client.py -- IMAP-Import von Leihanfragen + Kandidaten-CRUD.

Netzwerkgrenze: nur imaplib.IMAP4_SSL (get_imap_connection). Kein SMTP/Mail hier
(siehe auth.py), also ist die `mailbox`-Fixture für dieses Modul irrelevant.
Externe Grenzen, die gepatcht werden: email_client.imaplib.IMAP4_SSL,
email_client.decrypt_value, kanboard_client.get_leihanfragen_tasks. Alles
DB-Bezogene läuft über die echte Testcontainer-DB (db_session/user/app).
"""
import json
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from email.utils import formatdate

import pytest


# ---------------------------------------------------------------------------
# 1. Pure parsers -- no network/DB/app_ctx
# ---------------------------------------------------------------------------

@pytest.mark.unit
def test_decode_mime_header_empty_returns_empty_string():
    from vms.clients.email_client import decode_mime_header

    assert decode_mime_header('') == ''
    assert decode_mime_header(None) == ''


@pytest.mark.unit
def test_decode_mime_header_decodes_rfc2047_encoded_word_with_explicit_charset():
    from vms.clients.email_client import decode_mime_header

    # '=?utf-8?b?...?=' base64-encodes "Hällo" in utf-8.
    header = '=?utf-8?b?SMOkbGxv?='
    assert decode_mime_header(header) == 'Hällo'


@pytest.mark.unit
def test_decode_mime_header_plain_ascii_passes_through():
    from vms.clients.email_client import decode_mime_header

    assert decode_mime_header('Hello World') == 'Hello World'


@pytest.mark.unit
def test_decode_mime_header_bytes_part_with_none_encoding_falls_back_to_utf8_replace():
    """An encoded word directly followed by an unencoded run collapses into a
    bytes chunk with charset None -- exercises the `encoding or 'utf-8'`
    fallback with errors='replace'."""
    from vms.clients.email_client import decode_mime_header

    header = '=?utf-8?b?SMOkbGxv?= Bonus'
    result = decode_mime_header(header)
    # decode_header collapses the run into (b'H\xc3\xa4llo', 'utf-8') and
    # (b' Bonus', None) -- the second decodes via the utf-8/'replace' fallback.
    assert result == 'Hällo  Bonus'


# --- get_email_body ---------------------------------------------------------

@pytest.mark.unit
def test_get_email_body_multipart_extracts_text_plain_and_skips_attachment():
    import email as email_pkg

    msg = MIMEMultipart()
    msg['Subject'] = 'Test'
    attachment = MIMEApplication(b'binary-stuff', _subtype='octet-stream')
    attachment.add_header('Content-Disposition', 'attachment', filename='x.bin')
    msg.attach(attachment)
    msg.attach(MIMEText('Der eigentliche Text', 'plain', 'utf-8'))

    parsed = email_pkg.message_from_string(msg.as_string())

    from vms.clients.email_client import get_email_body
    body = get_email_body(parsed)

    assert body == 'Der eigentliche Text'


@pytest.mark.unit
def test_get_email_body_multipart_with_no_text_plain_part_returns_empty_string():
    """The for-loop over msg.walk() runs to completion without ever hitting the
    `break` (no non-attachment text/plain part exists) -- body stays ''."""
    import email as email_pkg

    msg = MIMEMultipart()
    msg['Subject'] = 'Nur Anhang'
    attachment = MIMEApplication(b'binary-stuff', _subtype='octet-stream')
    attachment.add_header('Content-Disposition', 'attachment', filename='x.bin')
    msg.attach(attachment)

    parsed = email_pkg.message_from_string(msg.as_string())

    from vms.clients.email_client import get_email_body
    assert get_email_body(parsed) == ''


@pytest.mark.unit
def test_get_email_body_non_multipart_decodes_payload():
    import email as email_pkg

    msg = EmailMessage()
    msg['Subject'] = 'Single part'
    msg.set_content('Einfacher Text ohne Anhang')

    parsed = email_pkg.message_from_bytes(msg.as_bytes())

    from vms.clients.email_client import get_email_body
    assert get_email_body(parsed) == 'Einfacher Text ohne Anhang\n'


@pytest.mark.unit
def test_get_email_body_empty_payload_returns_empty_string():
    import email as email_pkg

    raw = "Subject: leer\r\nContent-Type: text/plain\r\n\r\n"
    parsed = email_pkg.message_from_string(raw)

    from vms.clients.email_client import get_email_body
    assert get_email_body(parsed) == ''


# --- parse_email_content -----------------------------------------------------

@pytest.mark.unit
def test_parse_email_content_extracts_known_fields_with_multiline_value():
    from vms.clients.email_client import parse_email_content

    body = (
        "Vor- und Nachname: Erika Mustermann\n"
        "Anschrift: Musterstraße 1\n"
        "12345 Musterstadt\n"
        "E-Mail-Adresse: erika@example.com\n"
        "Datum: 01.05.2024\n"
    )
    result = parse_email_content(body)

    assert result['vorname_nachname'] == 'Erika Mustermann'
    # continuation line got appended with a newline join
    assert result['anschrift'] == 'Musterstraße 1\n12345 Musterstadt'
    assert result['email_address'] == 'erika@example.com'
    assert result['datum'] == '01.05.2024'


@pytest.mark.unit
def test_parse_email_content_empty_body_returns_empty_dict():
    from vms.clients.email_client import parse_email_content

    assert parse_email_content('') == {}
    assert parse_email_content('irrelevanter Fließtext ohne Doppelpunkt-Felder') == {}


@pytest.mark.unit
def test_parse_email_content_skips_blank_lines_between_fields():
    from vms.clients.email_client import parse_email_content

    body = "Vor- und Nachname: Erika Mustermann\n\nDatum: 01.05.2024\n"
    result = parse_email_content(body)

    assert result['vorname_nachname'] == 'Erika Mustermann'
    assert result['datum'] == '01.05.2024'


# --- extract_form_section -----------------------------------------------------

@pytest.mark.unit
def test_extract_form_section_empty_returns_empty_string():
    from vms.clients.email_client import extract_form_section

    assert extract_form_section('') == ''


@pytest.mark.unit
def test_extract_form_section_without_marker_returned_unchanged():
    from vms.clients.email_client import extract_form_section

    content = "Hallo,\nkeine Formularfelder hier.\nGruß"
    assert extract_form_section(content) == content


@pytest.mark.unit
def test_extract_form_section_slices_block_and_breaks_at_rahmenbedingungen():
    from vms.clients.email_client import extract_form_section

    content = (
        "Präambel-Text davor, nicht relevant\n"
        "Vor- und Nachname: Erika Mustermann\n"
        "Anschrift: Musterstraße 1\n"
        "Ich habe die Rahmenbedingungen gelesen: Ja\n"
        "Sollte nicht mehr enthalten sein"
    )
    result = extract_form_section(content)

    assert result.startswith('Vor- und Nachname: Erika Mustermann')
    assert 'Ich habe die Rahmenbedingungen gelesen: Ja' in result
    assert 'Präambel-Text' not in result
    assert 'Sollte nicht mehr enthalten sein' not in result


@pytest.mark.unit
def test_extract_form_section_runs_to_end_when_rahmenbedingungen_marker_is_missing():
    """The for-loop never hits its `break` when the closing marker is absent --
    the whole tail of the content is included instead of being cut off."""
    from vms.clients.email_client import extract_form_section

    content = (
        "Vor- und Nachname: Erika Mustermann\n"
        "Anschrift: Musterstraße 1\n"
        "Kein Abschluss-Marker hier"
    )
    result = extract_form_section(content)

    assert result == content


# --- is_loan_request_email -----------------------------------------------------

@pytest.mark.unit
@pytest.mark.parametrize('subject,expected', [
    ('[stuve.anlage] Bestellung Anlagenreferat', True),
    ('  [stuve.anlage] Bestellung Anlagenreferat', True),
    ('Re: [stuve.anlage] Bestellung Anlagenreferat', False),
    ('AW: [stuve.anlage] Bestellung Anlagenreferat', False),
    ('Fwd: [stuve.anlage] Bestellung Anlagenreferat', False),
    ('Unrelated subject line', False),
])
def test_is_loan_request_email_matches_only_leading_stuve_bestellung(subject, expected):
    from vms.clients.email_client import is_loan_request_email

    assert is_loan_request_email(subject) is expected


# ---------------------------------------------------------------------------
# 2. IMAP path -- imaplib.IMAP4_SSL + decrypt_value patched, never a socket
# ---------------------------------------------------------------------------

class _FakeIMAPConn:
    """Small scriptable stand-in for imaplib.IMAP4_SSL so fetch_emails_for_user
    can be exercised without touching a real socket."""

    def __init__(self, folders=None, search_result=('OK', [b'1']),
                 fetch_map=None, select_status='OK', list_status='OK'):
        self.folders = folders if folders is not None else [
            b'(\\HasNoChildren) "/" "INBOX"'
        ]
        self.search_result = search_result
        self.fetch_map = fetch_map or {}
        self.select_status = select_status
        self.list_status = list_status
        self.logged_out = False

    def list(self):
        return (self.list_status, self.folders)

    def select(self, mailbox, readonly=True):
        return (self.select_status, [b'1'])

    def search(self, charset, *criteria):
        return self.search_result

    def fetch(self, msg_id, parts):
        raw = self.fetch_map.get(msg_id)
        if raw is None:
            return ('OK', [None])
        return ('OK', [(b'1 (RFC822 {%d}' % len(raw), raw)])

    def logout(self):
        self.logged_out = True


def _build_email_bytes(subject, sender='absender@example.com', date_header=None,
                        body='Vor- und Nachname: Erika Mustermann\nDatum: 01.05.2024\n',
                        message_id='<abc@example.com>'):
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = sender
    msg['Date'] = date_header if date_header is not None else formatdate(localtime=False)
    msg['Message-ID'] = message_id
    msg.set_content(body)
    return msg.as_bytes()


LOAN_SUBJECT = '[stuve.anlage] Bestellung Anlagenreferat'


@pytest.mark.unit
def test_get_imap_connection_uses_default_port_when_missing(mocker):
    from vms.clients.email_client import get_imap_connection

    mocker.patch('vms.clients.email_client.decrypt_value', return_value='geheim')
    fake_conn = mocker.MagicMock()
    ssl_ctor = mocker.patch('vms.clients.email_client.imaplib.IMAP4_SSL', return_value=fake_conn)

    settings = {
        'imap_server': 'imap.example.com',
        'imap_user': 'user@example.com',
        'encrypted_imap_password': 'ciphertext',
        # imap_port intentionally omitted
    }
    conn, user = get_imap_connection(settings)

    assert conn is fake_conn
    assert user == 'user@example.com'
    ssl_ctor.assert_called_once_with('imap.example.com', 993)
    fake_conn.login.assert_called_once_with('user@example.com', 'geheim')


@pytest.mark.unit
def test_get_imap_connection_incomplete_config_raises_value_error(mocker):
    from vms.clients.email_client import get_imap_connection

    mocker.patch('vms.clients.email_client.decrypt_value', return_value=None)
    settings = {'imap_server': 'imap.example.com', 'imap_user': 'user@example.com'}

    with pytest.raises(ValueError, match='unvollständig'):
        get_imap_connection(settings)


@pytest.mark.unit
def test_get_imap_connection_connect_failure_is_rewrapped_as_value_error(mocker):
    from vms.clients.email_client import get_imap_connection

    mocker.patch('vms.clients.email_client.decrypt_value', return_value='geheim')
    mocker.patch('vms.clients.email_client.imaplib.IMAP4_SSL', side_effect=OSError('connection refused'))

    settings = {
        'imap_server': 'imap.example.com',
        'imap_user': 'user@example.com',
        'encrypted_imap_password': 'ciphertext',
    }
    with pytest.raises(ValueError, match='IMAP-Verbindung fehlgeschlagen'):
        get_imap_connection(settings)


@pytest.mark.unit
def test_get_imap_connection_login_failure_is_rewrapped_as_value_error(mocker):
    from vms.clients.email_client import get_imap_connection

    mocker.patch('vms.clients.email_client.decrypt_value', return_value='geheim')
    fake_conn = mocker.MagicMock()
    fake_conn.login.side_effect = Exception('auth failed')
    mocker.patch('vms.clients.email_client.imaplib.IMAP4_SSL', return_value=fake_conn)

    settings = {
        'imap_server': 'imap.example.com',
        'imap_user': 'user@example.com',
        'encrypted_imap_password': 'ciphertext',
    }
    with pytest.raises(ValueError, match='IMAP-Verbindung fehlgeschlagen'):
        get_imap_connection(settings)


@pytest.mark.unit
def test_fetch_emails_for_user_no_settings_raises_value_error(mocker):
    from vms.clients.email_client import fetch_emails_for_user

    mocker.patch('vms.clients.email_client.get_user_settings', return_value=None)

    with pytest.raises(ValueError, match='keine E-Mail-Einstellungen'):
        fetch_emails_for_user(42)


@pytest.mark.unit
def test_fetch_emails_for_user_happy_path_parses_matching_email(mocker):
    from vms.clients.email_client import fetch_emails_for_user

    raw = _build_email_bytes(LOAN_SUBJECT, message_id='<happy@example.com>')
    conn = _FakeIMAPConn(fetch_map={b'1': raw})

    mocker.patch('vms.clients.email_client.get_user_settings', return_value={'imap_server': 'x'})
    # A real prior sync timestamp -- see the dedicated bug test below for the
    # separate (broken) last_sync=None / year-start path.
    mocker.patch('vms.clients.email_client.get_last_sync',
                 return_value=datetime.now(timezone.utc) - timedelta(days=5))
    mocker.patch('vms.clients.email_client.get_imap_connection', return_value=(conn, 'user@example.com'))

    emails = fetch_emails_for_user(1)

    assert len(emails) == 1
    e = emails[0]
    assert e['subject'] == LOAN_SUBJECT
    assert e['vorname_nachname'] == 'Erika Mustermann'
    assert e['datum'] == '01.05.2024'
    assert e['email_id'] == '<happy@example.com>'
    assert conn.logged_out is True


@pytest.mark.unit
def test_first_ever_sync_with_no_prior_last_sync_should_still_return_matching_emails(mocker):
    from vms.clients.email_client import fetch_emails_for_user

    raw = _build_email_bytes(LOAN_SUBJECT, message_id='<first-sync@example.com>')
    conn = _FakeIMAPConn(fetch_map={b'1': raw})

    mocker.patch('vms.clients.email_client.get_user_settings', return_value={'imap_server': 'x'})
    mocker.patch('vms.clients.email_client.get_last_sync', return_value=None)  # no prior sync at all
    mocker.patch('vms.clients.email_client.get_imap_connection', return_value=(conn, 'user@example.com'))

    emails = fetch_emails_for_user(1)

    # Domain-correct: with no prior sync state, the year-start search should
    # still surface matching emails -- not silently return nothing.
    assert [e['email_id'] for e in emails] == ['<first-sync@example.com>']


@pytest.mark.unit
def test_fetch_emails_for_user_uses_since_last_sync_when_set(mocker):
    from vms.clients.email_client import fetch_emails_for_user

    raw = _build_email_bytes(LOAN_SUBJECT, message_id='<recent@example.com>')
    conn = _FakeIMAPConn(fetch_map={b'1': raw})

    mocker.patch('vms.clients.email_client.get_user_settings', return_value={'imap_server': 'x'})
    last_sync = datetime.now(timezone.utc) - timedelta(days=5)
    mocker.patch('vms.clients.email_client.get_last_sync', return_value=last_sync)
    mocker.patch('vms.clients.email_client.get_imap_connection', return_value=(conn, 'user@example.com'))

    emails = fetch_emails_for_user(1)

    assert [e['email_id'] for e in emails] == ['<recent@example.com>']


@pytest.mark.unit
def test_fetch_emails_for_user_skips_mail_older_than_last_sync(mocker):
    from vms.clients.email_client import fetch_emails_for_user

    old_date = formatdate(
        (datetime.now(timezone.utc) - timedelta(days=30)).timestamp(), localtime=False)
    raw = _build_email_bytes(LOAN_SUBJECT, date_header=old_date, message_id='<old@example.com>')
    conn = _FakeIMAPConn(fetch_map={b'1': raw})

    mocker.patch('vms.clients.email_client.get_user_settings', return_value={'imap_server': 'x'})
    last_sync = datetime.now(timezone.utc) - timedelta(days=1)
    mocker.patch('vms.clients.email_client.get_last_sync', return_value=last_sync)
    mocker.patch('vms.clients.email_client.get_imap_connection', return_value=(conn, 'user@example.com'))

    emails = fetch_emails_for_user(1)

    assert emails == []


@pytest.mark.unit
def test_fetch_emails_for_user_dedupes_repeated_message_id(mocker):
    from vms.clients.email_client import fetch_emails_for_user

    raw = _build_email_bytes(LOAN_SUBJECT, message_id='<dupe@example.com>')
    conn = _FakeIMAPConn(
        search_result=('OK', [b'1 2']),
        fetch_map={b'1': raw, b'2': raw},
    )

    mocker.patch('vms.clients.email_client.get_user_settings', return_value={'imap_server': 'x'})
    mocker.patch('vms.clients.email_client.get_last_sync',
                 return_value=datetime.now(timezone.utc) - timedelta(days=5))
    mocker.patch('vms.clients.email_client.get_imap_connection', return_value=(conn, 'user@example.com'))

    emails = fetch_emails_for_user(1)

    assert len(emails) == 1


@pytest.mark.unit
def test_fetch_emails_for_user_rejects_reply_prefixed_subject(mocker):
    from vms.clients.email_client import fetch_emails_for_user

    raw = _build_email_bytes('Re: ' + LOAN_SUBJECT, message_id='<reply@example.com>')
    conn = _FakeIMAPConn(fetch_map={b'1': raw})

    mocker.patch('vms.clients.email_client.get_user_settings', return_value={'imap_server': 'x'})
    mocker.patch('vms.clients.email_client.get_last_sync',
                 return_value=datetime.now(timezone.utc) - timedelta(days=5))
    mocker.patch('vms.clients.email_client.get_imap_connection', return_value=(conn, 'user@example.com'))

    assert fetch_emails_for_user(1) == []


@pytest.mark.unit
def test_fetch_emails_for_user_list_not_ok_returns_empty(mocker):
    from vms.clients.email_client import fetch_emails_for_user

    conn = _FakeIMAPConn(list_status='NO')

    mocker.patch('vms.clients.email_client.get_user_settings', return_value={'imap_server': 'x'})
    mocker.patch('vms.clients.email_client.get_last_sync', return_value=None)
    mocker.patch('vms.clients.email_client.get_imap_connection', return_value=(conn, 'user@example.com'))

    assert fetch_emails_for_user(1) == []
    assert conn.logged_out is True


@pytest.mark.unit
def test_fetch_emails_for_user_skips_folder_when_select_fails_but_processes_others(mocker):
    from vms.clients.email_client import fetch_emails_for_user

    raw = _build_email_bytes(LOAN_SUBJECT, message_id='<second-folder@example.com>')
    folders = [
        b'(\\HasNoChildren) "/" "Broken"',
        b'(\\HasNoChildren) "/" "INBOX"',
    ]
    conn = _FakeIMAPConn(folders=folders, fetch_map={b'1': raw})
    # First select() call (Broken) fails, second (INBOX) succeeds.
    conn.select = mocker.MagicMock(side_effect=[('NO', [None]), ('OK', [b'1'])])

    mocker.patch('vms.clients.email_client.get_user_settings', return_value={'imap_server': 'x'})
    mocker.patch('vms.clients.email_client.get_last_sync',
                 return_value=datetime.now(timezone.utc) - timedelta(days=5))
    mocker.patch('vms.clients.email_client.get_imap_connection', return_value=(conn, 'user@example.com'))

    emails = fetch_emails_for_user(1)

    assert [e['email_id'] for e in emails] == ['<second-folder@example.com>']


@pytest.mark.unit
def test_fetch_emails_for_user_skips_folder_with_empty_parsed_name(mocker):
    """A folder listing line whose parsed name is empty must be skipped
    (email_client.py:252) without aborting the other, well-formed folder."""
    from vms.clients.email_client import fetch_emails_for_user

    raw = _build_email_bytes(LOAN_SUBJECT, message_id='<after-empty-name@example.com>')
    folders = [
        b'(\\HasNoChildren) "/" ""',
        b'(\\HasNoChildren) "/" "INBOX"',
    ]
    conn = _FakeIMAPConn(folders=folders, fetch_map={b'1': raw})

    mocker.patch('vms.clients.email_client.get_user_settings', return_value={'imap_server': 'x'})
    mocker.patch('vms.clients.email_client.get_last_sync',
                 return_value=datetime.now(timezone.utc) - timedelta(days=5))
    mocker.patch('vms.clients.email_client.get_imap_connection', return_value=(conn, 'user@example.com'))

    emails = fetch_emails_for_user(1)

    assert [e['email_id'] for e in emails] == ['<after-empty-name@example.com>']


@pytest.mark.unit
def test_fetch_emails_for_user_search_not_ok_skips_folder(mocker):
    from vms.clients.email_client import fetch_emails_for_user

    conn = _FakeIMAPConn(search_result=('OK', [b'']))

    mocker.patch('vms.clients.email_client.get_user_settings', return_value={'imap_server': 'x'})
    mocker.patch('vms.clients.email_client.get_last_sync', return_value=None)
    mocker.patch('vms.clients.email_client.get_imap_connection', return_value=(conn, 'user@example.com'))

    assert fetch_emails_for_user(1) == []


@pytest.mark.unit
def test_fetch_emails_for_user_swallows_per_message_fetch_error(mocker):
    """conn.fetch() returning no payload for one message must not abort the
    whole batch -- the other message in the same folder still comes through."""
    from vms.clients.email_client import fetch_emails_for_user

    good_raw = _build_email_bytes(LOAN_SUBJECT, message_id='<good@example.com>')
    conn = _FakeIMAPConn(
        search_result=('OK', [b'1 2']),
        fetch_map={b'1': None, b'2': good_raw},  # b'1' -> fetch() yields no data -> raises
    )

    mocker.patch('vms.clients.email_client.get_user_settings', return_value={'imap_server': 'x'})
    mocker.patch('vms.clients.email_client.get_last_sync',
                 return_value=datetime.now(timezone.utc) - timedelta(days=5))
    mocker.patch('vms.clients.email_client.get_imap_connection', return_value=(conn, 'user@example.com'))

    emails = fetch_emails_for_user(1)

    assert [e['email_id'] for e in emails] == ['<good@example.com>']


@pytest.mark.unit
def test_fetch_emails_for_user_swallows_per_folder_error(mocker):
    """A folder whose name can't even be decoded must not prevent processing of
    a later, well-formed folder."""
    from vms.clients.email_client import fetch_emails_for_user

    class _BadFolder:
        def decode(self):
            raise ValueError('cannot decode this folder listing entry')

    raw = _build_email_bytes(LOAN_SUBJECT, message_id='<after-bad-folder@example.com>')
    conn = _FakeIMAPConn(
        folders=[_BadFolder(), b'(\\HasNoChildren) "/" "INBOX"'],
        fetch_map={b'1': raw},
    )

    mocker.patch('vms.clients.email_client.get_user_settings', return_value={'imap_server': 'x'})
    mocker.patch('vms.clients.email_client.get_last_sync',
                 return_value=datetime.now(timezone.utc) - timedelta(days=5))
    mocker.patch('vms.clients.email_client.get_imap_connection', return_value=(conn, 'user@example.com'))

    emails = fetch_emails_for_user(1)

    assert [e['email_id'] for e in emails] == ['<after-bad-folder@example.com>']


@pytest.mark.unit
def test_fetch_emails_for_user_wraps_connection_error_as_imap_fehler(mocker):
    from vms.clients.email_client import fetch_emails_for_user

    mocker.patch('vms.clients.email_client.get_user_settings', return_value={'imap_server': 'x'})
    mocker.patch('vms.clients.email_client.get_last_sync', return_value=None)
    mocker.patch('vms.clients.email_client.get_imap_connection', side_effect=ValueError('boom'))

    with pytest.raises(Exception, match='IMAP-Fehler'):
        fetch_emails_for_user(1)


@pytest.mark.unit
def test_unparseable_date_header_should_not_always_bypass_the_last_sync_filter(mocker):
    from vms.clients.email_client import fetch_emails_for_user

    raw = _build_email_bytes(
        LOAN_SUBJECT, date_header='not-a-real-date', message_id='<garbage-date@example.com>')
    conn = _FakeIMAPConn(fetch_map={b'1': raw})

    mocker.patch('vms.clients.email_client.get_user_settings', return_value={'imap_server': 'x'})
    # A realistic last_sync: captured well before "now" (as it always is in
    # practice, since sync timestamps are persisted before the next run).
    last_sync = datetime.now(timezone.utc) - timedelta(days=30)
    mocker.patch('vms.clients.email_client.get_last_sync', return_value=last_sync)
    mocker.patch('vms.clients.email_client.get_imap_connection', return_value=(conn, 'user@example.com'))

    emails = fetch_emails_for_user(1)

    # Domain-correct: we cannot confirm this mail is new, so it must not be
    # surfaced as new email every time fetch_emails_for_user runs.
    assert emails == []


@pytest.mark.unit
def test_sync_emails_composes_fetch_save_and_update_in_order(mocker):
    from vms.clients.email_client import sync_emails

    calls = []
    mocker.patch('vms.clients.email_client.fetch_emails_for_user',
                 side_effect=lambda uid: (calls.append('fetch'), [{'email_id': 'a'}])[1])
    mocker.patch('vms.clients.email_client.save_candidates',
                 side_effect=lambda emails, uid: (calls.append('save'), 3)[1])
    mocker.patch('vms.clients.email_client.update_last_sync',
                 side_effect=lambda uid: calls.append('update'))

    result = sync_emails(7)

    assert result == 3
    assert calls == ['fetch', 'save', 'update']


# ---------------------------------------------------------------------------
# 3. DB CRUD + serialization -- real testcontainer DB
# ---------------------------------------------------------------------------

def _make_candidate(db_session, user_id, **overrides):
    from vms.domain.models import EmailCandidate
    defaults = dict(user_id=user_id, vorname_nachname='Erika Mustermann',
                     veranstaltungsname='Sommerfest', datum='2024-05-01',
                     status='pending', tags=[])
    defaults.update(overrides)
    c = EmailCandidate(**defaults)
    db_session.add(c)
    db_session.flush()
    return c.id


@pytest.mark.integration
def test_get_candidates_filters_by_status_and_all_bypasses_filter(app_ctx, db_session, user):
    from vms.clients.email_client import get_candidates

    _make_candidate(db_session, user['id'], status='pending', email_id='c-pending')
    _make_candidate(db_session, user['id'], status='done', email_id='c-done')
    db_session.commit()

    pending_only = get_candidates('pending')
    everything = get_candidates('ALL')

    assert {c['email_id'] for c in pending_only} == {'c-pending'}
    assert {c['email_id'] for c in everything} == {'c-pending', 'c-done'}


@pytest.mark.integration
def test_get_candidates_parses_tags_stored_as_json_string(app_ctx, db_session, user):
    from vms.clients.email_client import get_candidates

    _make_candidate(db_session, user['id'], email_id='c-tags-str',
                     tags=json.dumps(['vip', 'gross']))
    db_session.commit()

    result = get_candidates('ALL')
    row = next(c for c in result if c['email_id'] == 'c-tags-str')

    assert row['tags'] == ['vip', 'gross']


@pytest.mark.integration
def test_get_candidates_malformed_json_tags_fall_back_to_empty_list(app_ctx, db_session, user):
    """Bare except at email_client.py:426. Pinned as a defensible display
    fallback (same pattern as format_money_de): tags stay in the DB untouched,
    only the API response degrades to []. Not treated as a data-loss bug."""
    from vms.clients.email_client import get_candidates

    _make_candidate(db_session, user['id'], email_id='c-tags-bad', tags='not-json{')
    db_session.commit()

    result = get_candidates('ALL')
    row = next(c for c in result if c['email_id'] == 'c-tags-bad')

    assert row['tags'] == []


@pytest.mark.integration
def test_get_candidates_leaves_a_real_tag_list_untouched(app_ctx, db_session, user):
    """When tags is already a proper (non-empty, non-string) JSONB list, the
    `elif not d.get('tags')` branch is skipped entirely (email_client.py:428)."""
    from vms.clients.email_client import get_candidates

    _make_candidate(db_session, user['id'], email_id='c-tags-list', tags=['schon-liste'])
    db_session.commit()

    row = next(c for c in get_candidates('ALL') if c['email_id'] == 'c-tags-list')

    assert row['tags'] == ['schon-liste']


@pytest.mark.integration
def test_get_candidates_serializes_received_at_to_isoformat(app_ctx, db_session, user):
    from vms.clients.email_client import get_candidates

    ts = datetime(2024, 5, 1, 10, 30, tzinfo=timezone.utc)
    _make_candidate(db_session, user['id'], email_id='c-date', received_at=ts)
    db_session.commit()

    row = next(c for c in get_candidates('ALL') if c['email_id'] == 'c-date')

    assert row['received_at'] == ts.isoformat()


@pytest.mark.integration
def test_get_candidate_by_id_found_vs_missing(app_ctx, db_session, user):
    from vms.clients.email_client import get_candidate_by_id

    cid = _make_candidate(db_session, user['id'], email_id='c-single', tags=['a'])
    db_session.commit()

    found = get_candidate_by_id(cid)
    missing = get_candidate_by_id(cid + 100000)

    assert found['email_id'] == 'c-single'
    assert found['tags'] == ['a']
    assert missing is None


@pytest.mark.integration
def test_get_candidate_by_id_missing_tags_normalizes_to_empty_list(app_ctx, db_session, user):
    """Exercises the `elif not c.get('tags')` True branch (email_client.py:503-504),
    distinct from the found-with-real-tags case above."""
    from vms.clients.email_client import get_candidate_by_id

    cid = _make_candidate(db_session, user['id'], email_id='c-no-tags', tags=[])
    db_session.commit()

    found = get_candidate_by_id(cid)

    assert found['tags'] == []


@pytest.mark.integration
def test_get_candidate_by_id_malformed_tags_fall_back_to_empty_list(app_ctx, db_session, user):
    from vms.clients.email_client import get_candidate_by_id

    cid = _make_candidate(db_session, user['id'], email_id='c-single-bad-tags', tags='{broken')
    db_session.commit()

    found = get_candidate_by_id(cid)

    assert found['tags'] == []


@pytest.mark.integration
def test_update_candidate_not_found_returns_false(app_ctx, db_session, user):
    from vms.clients.email_client import update_candidate

    assert update_candidate(999999, {'subject': 'x'}) is False


@pytest.mark.integration
def test_update_candidate_updates_whitelisted_field_and_returns_true(app_ctx, db_session, user):
    from vms.clients.email_client import update_candidate
    from vms.domain.models import EmailCandidate

    cid = _make_candidate(db_session, user['id'], email_id='c-update')
    db_session.commit()

    ok = update_candidate(cid, {'veranstaltungsname': 'Neuer Name', 'ignored_field': 'x'})

    assert ok is True
    db_session.expire_all()
    row = db_session.query(EmailCandidate).filter_by(id=cid).one()
    assert row.veranstaltungsname == 'Neuer Name'


@pytest.mark.integration
def test_update_candidate_normalizes_datum_and_end_date_to_iso(app_ctx, db_session, user):
    from vms.clients.email_client import update_candidate
    from vms.domain.models import EmailCandidate

    cid = _make_candidate(db_session, user['id'], email_id='c-update-dates')
    db_session.commit()

    update_candidate(cid, {'datum': '01.05.2024', 'end_date': '03.05.2024'})

    db_session.expire_all()
    row = db_session.query(EmailCandidate).filter_by(id=cid).one()
    assert row.datum == '2024-05-01'
    assert row.end_date == '2024-05-03'


@pytest.mark.integration
def test_save_candidates_inserts_new_and_skips_existing(app_ctx, db_session, user):
    from vms.clients.email_client import save_candidates
    from vms.domain.models import EmailCandidate

    _make_candidate(db_session, user['id'], email_id='already-there')
    db_session.commit()

    emails = [
        {'email_id': 'already-there', 'subject': 'dup', 'received_at': '2024-01-01T00:00:00'},
        {'email_id': 'brand-new', 'subject': 'new one', 'received_at': '2024-01-02T00:00:00',
         'vorname_nachname': 'Max Mustermann'},
    ]

    count = save_candidates(emails, user['id'])

    assert count == 1
    rows = db_session.query(EmailCandidate).filter(
        EmailCandidate.email_id.in_(['already-there', 'brand-new'])).all()
    assert {r.email_id for r in rows} == {'already-there', 'brand-new'}
    new_row = next(r for r in rows if r.email_id == 'brand-new')
    assert new_row.vorname_nachname == 'Max Mustermann'


@pytest.mark.integration
def test_save_candidates_swallows_a_broken_row_and_keeps_the_others(app_ctx, db_session, user):
    from vms.clients.email_client import save_candidates
    from vms.domain.models import EmailCandidate

    class _BoomDict(dict):
        """Behaves like a normal payload dict except .get('raw_content', ...)
        blows up, simulating unexpected bad data for exactly one row."""
        def get(self, key, default=None):
            if key == 'raw_content':
                raise RuntimeError('kaputte Nutzdaten')
            return super().get(key, default)

    good = {'email_id': 'good-row', 'subject': 'ok', 'received_at': '2024-01-01T00:00:00'}
    bad = _BoomDict(email_id='bad-row', subject='boom', received_at='2024-01-01T00:00:00')

    count = save_candidates([bad, good], user['id'])

    assert count == 1
    rows = {r.email_id for r in db_session.query(EmailCandidate).all()}
    assert 'good-row' in rows
    assert 'bad-row' not in rows


@pytest.mark.integration
def test_create_manual_candidate_sets_processed_status_and_returns_id(app_ctx, db_session, user):
    from vms.clients.email_client import create_manual_candidate
    from vms.domain.models import EmailCandidate, CandidateStatus

    new_id = create_manual_candidate({
        'vorname_nachname': 'Manuell Angelegt',
        'veranstaltungsname': 'Handeingabe',
        'datum': '01.05.2024',
    }, user['id'])

    row = db_session.query(EmailCandidate).filter_by(id=new_id).one()
    assert row.vorname_nachname == 'Manuell Angelegt'
    assert row.status == CandidateStatus.PROCESSED.value
    assert row.datum == '2024-05-01'
    assert row.email_id.startswith('manual_')


@pytest.mark.integration
def test_mark_candidate_processed_and_done_toggle_contract_created(app_ctx, db_session, user):
    from vms.clients.email_client import mark_candidate_processed, mark_candidate_done
    from vms.domain.models import EmailCandidate, CandidateStatus

    cid = _make_candidate(db_session, user['id'], email_id='c-toggle', status='done',
                          contract_created=True)
    db_session.commit()

    ok = mark_candidate_processed(cid)

    assert ok is True
    db_session.expire_all()
    row = db_session.query(EmailCandidate).filter_by(id=cid).one()
    assert row.status == CandidateStatus.PROCESSED.value
    assert row.contract_created is False

    ok2 = mark_candidate_done(cid)

    assert ok2 is True
    db_session.expire_all()
    row = db_session.query(EmailCandidate).filter_by(id=cid).one()
    assert row.status == CandidateStatus.DONE.value
    assert row.contract_created is True


@pytest.mark.integration
def test_mark_candidate_processed_missing_id_returns_false(app_ctx, db_session, user):
    from vms.clients.email_client import mark_candidate_processed

    assert mark_candidate_processed(999999) is False


@pytest.mark.integration
def test_delete_candidate_found_true_missing_false(app_ctx, db_session, user):
    from vms.clients.email_client import delete_candidate
    from vms.domain.models import EmailCandidate

    cid = _make_candidate(db_session, user['id'], email_id='c-delete')
    db_session.commit()

    assert delete_candidate(cid) is True
    assert db_session.query(EmailCandidate).filter_by(id=cid).first() is None
    assert delete_candidate(cid) is False


@pytest.mark.integration
def test_save_kanboard_task_id_sets_id_when_row_present(app_ctx, db_session, user):
    from vms.clients.email_client import save_kanboard_task_id
    from vms.domain.models import EmailCandidate

    cid = _make_candidate(db_session, user['id'], email_id='c-kb')
    db_session.commit()

    # Function has no return statement (implicitly returns None either way) --
    # the persisted side effect is what we assert on.
    result = save_kanboard_task_id(cid, 4242)

    assert result is None
    db_session.expire_all()
    row = db_session.query(EmailCandidate).filter_by(id=cid).one()
    assert row.kanboard_task_id == 4242


@pytest.mark.integration
def test_save_kanboard_task_id_missing_row_is_a_silent_no_op(app_ctx, db_session, user):
    from vms.clients.email_client import save_kanboard_task_id

    # Must not raise even though no row matches.
    assert save_kanboard_task_id(999999, 1) is None


@pytest.mark.integration
def test_get_last_sync_returns_none_when_no_rows(app_ctx, db_session):
    from vms.clients.email_client import get_last_sync

    assert get_last_sync() is None


@pytest.mark.integration
def test_update_last_sync_adds_row_for_new_user_and_get_last_sync_reads_it_back(
        app_ctx, db_session, user):
    from vms.clients.email_client import update_last_sync, get_last_sync
    from vms.domain.models import EmailSyncState

    assert db_session.query(EmailSyncState).filter_by(user_id=user['id']).first() is None

    update_last_sync(user['id'])

    row = db_session.query(EmailSyncState).filter_by(user_id=user['id']).first()
    assert row is not None
    last_sync = get_last_sync()
    assert last_sync is not None
    assert last_sync.tzinfo is not None  # tz-aware, never a naive datetime


@pytest.mark.integration
def test_update_last_sync_second_call_updates_existing_row_without_duplicating(
        app_ctx, db_session, user):
    """Second call for the same user hits the `if not row` False branch
    (email_client.py:97) -- the existing row is updated in place by the
    bulk UPDATE above, no new row is inserted."""
    from vms.clients.email_client import update_last_sync
    from vms.domain.models import EmailSyncState

    update_last_sync(user['id'])
    first_count = db_session.query(EmailSyncState).filter_by(user_id=user['id']).count()

    update_last_sync(user['id'])
    second_count = db_session.query(EmailSyncState).filter_by(user_id=user['id']).count()

    assert first_count == 1
    assert second_count == 1


@pytest.mark.integration
def test_update_last_sync_reset_to_start_of_year_sets_january_first(app_ctx, db_session, user):
    from vms.clients.email_client import update_last_sync, get_last_sync

    update_last_sync(user['id'], reset_to_start_of_year=True)

    last_sync = get_last_sync()
    current_year = datetime.now().year
    assert last_sync == datetime(current_year, 1, 1, tzinfo=timezone.utc)


@pytest.mark.integration
def test_update_last_sync_without_reset_uses_current_time(app_ctx, db_session, user):
    from vms.clients.email_client import update_last_sync, get_last_sync

    before = datetime.now(timezone.utc)
    update_last_sync(user['id'])
    after = datetime.now(timezone.utc)

    last_sync = get_last_sync()
    assert before <= last_sync <= after


@pytest.mark.integration
def test_update_last_sync_updates_all_existing_rows_globally(app_ctx, db_session, user):
    """Sync state is global across users (docstring): triggering it for one
    user must bump every other user's row too, to avoid duplicate fetches."""
    from vms.auth import User
    from vms.clients.email_client import update_last_sync, get_last_sync
    from vms.domain.models import EmailSyncState

    other = User.create(username='other', password='Sup3r-Secret!',
                        display_name='Other', email='other@example.com', is_active=True)
    db_session.add(EmailSyncState(user_id=other.id,
                                   last_sync=datetime(2000, 1, 1, tzinfo=timezone.utc)))
    db_session.commit()

    update_last_sync(user['id'])

    db_session.expire_all()
    other_row = db_session.query(EmailSyncState).filter_by(user_id=other.id).one()
    assert other_row.last_sync.year != 2000


# ---------------------------------------------------------------------------
# 4. sync_with_kanboard
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_sync_with_kanboard_swallows_get_tasks_error_and_returns_zero_counts(
        app_ctx, db_session, user, mocker):
    from vms.clients.email_client import sync_with_kanboard

    mocker.patch('vms.clients.kanboard_client.get_leihanfragen_tasks', side_effect=Exception('kanboard down'))

    result = sync_with_kanboard(user['id'])

    assert result == {'updated': 0, 'created': 0}


@pytest.mark.integration
def test_sync_with_kanboard_creates_new_candidate_for_unlinked_task(
        app_ctx, db_session, user, mocker):
    from vms.clients.email_client import sync_with_kanboard
    from vms.domain.models import EmailCandidate

    task = {
        'id': '501',
        'title': 'Neue Anfrage',
        'description': 'Beschreibung',
        'date_due': '0',
        'tags': ['neu'],
        'parsed_data': {'vorname_nachname': 'Kanban Task', 'email_address': 'kb@example.com'},
    }
    mocker.patch('vms.clients.kanboard_client.get_leihanfragen_tasks', return_value=[task])

    result = sync_with_kanboard(user['id'])

    assert result == {'updated': 0, 'created': 1}
    row = db_session.query(EmailCandidate).filter_by(kanboard_task_id=501).one()
    assert row.vorname_nachname == 'Kanban Task'
    assert row.tags == ['neu']


@pytest.mark.integration
def test_sync_pushes_vms_data_to_kanboard_when_diverged(
        app_ctx, db_session, user, mocker):
    """VMS is the truth: a linked candidate whose data differs from Kanboard is
    pushed INTO the Kanboard task; the VMS row is left untouched."""
    from vms.clients.email_client import sync_with_kanboard
    from vms.domain.models import EmailCandidate

    cid = _make_candidate(db_session, user['id'], email_id='kb_777', kanboard_task_id=777,
                          veranstaltungsname='VMS Titel', vorname_nachname='VMS Name',
                          email_address='vms@example.com', datum='2024-06-15', tags=['vip'])
    db_session.commit()

    # Stale Kanboard task: different title, empty description, no tags, no date.
    task = {'id': '777', 'title': 'Alter Kanboard Titel', 'description': '',
            'date_due': '0', 'tags': [], 'parsed_data': {}}
    mocker.patch('vms.clients.kanboard_client.get_leihanfragen_tasks', return_value=[task])
    update = mocker.patch('vms.clients.kanboard_client.update_task', return_value=True)

    result = sync_with_kanboard(user['id'])

    assert result == {'updated': 1, 'created': 0}
    args, kwargs = update.call_args
    assert args[1] == 777  # task_id
    assert kwargs['title'] == 'VMS Titel'
    assert kwargs['tags'] == ['vip']
    assert kwargs['due_date'] == '2024-06-15'
    assert 'E-Mail: vms@example.com' in kwargs['description']
    assert 'Vor- und Nachname: VMS Name' in kwargs['description']

    db_session.expire_all()
    row = db_session.query(EmailCandidate).filter_by(id=cid).one()
    assert row.veranstaltungsname == 'VMS Titel'
    assert row.email_address == 'vms@example.com'


@pytest.mark.integration
def test_sync_does_not_overwrite_vms_fields_from_kanboard(
        app_ctx, db_session, user, mocker):
    """Regression: fields entered in VMS (email, name, address, phone) that
    Kanboard does not have must survive the sync instead of being wiped."""
    from vms.clients.email_client import sync_with_kanboard
    from vms.domain.models import EmailCandidate

    cid = _make_candidate(db_session, user['id'], email_id='kb_778', kanboard_task_id=778,
                          veranstaltungsname='Fest', email_address='nachgetragen@example.com',
                          vorname_nachname='Nachgetragener Name',
                          anschrift='Nachgetragene Str. 5', telefon='0170 999',
                          datum='2024-06-15', tags=[])
    db_session.commit()

    # Kanboard knows none of the added fields -- the old code would clear them.
    task = {'id': '778', 'title': 'Fest', 'description': '',
            'date_due': '0', 'tags': [], 'parsed_data': {}}
    mocker.patch('vms.clients.kanboard_client.get_leihanfragen_tasks', return_value=[task])
    mocker.patch('vms.clients.kanboard_client.update_task', return_value=True)

    sync_with_kanboard(user['id'])

    db_session.expire_all()
    row = db_session.query(EmailCandidate).filter_by(id=cid).one()
    assert row.email_address == 'nachgetragen@example.com'
    assert row.vorname_nachname == 'Nachgetragener Name'
    assert row.anschrift == 'Nachgetragene Str. 5'
    assert row.telefon == '0170 999'


@pytest.mark.integration
def test_sync_in_sync_candidate_does_not_push_or_count(
        app_ctx, db_session, user, mocker):
    """Idempotency: when the VMS candidate already matches the Kanboard task,
    no update_task call is made and it is not counted as updated."""
    from vms.clients.email_client import sync_with_kanboard
    import vms.clients.kanboard_client as kb

    fields = dict(vorname_nachname='Max', anschrift='Str 1', email_address='m@e.de',
                  telefon='030', veranstaltungsname='Gleich', veranstaltungsart='Fest',
                  veranstaltungsort='Ort', veranstaltungsbereich='Aussen',
                  personenzahl='10', material='Zelt', sonstiges='x',
                  rahmenbedingungen='y')
    _make_candidate(db_session, user['id'], email_id='kb_779', kanboard_task_id=779,
                    datum='2024-06-15', tags=['x'], subject='Gleich', **fields)
    db_session.commit()

    task = {'id': '779', 'title': 'Gleich',
            'description': kb.build_task_description(fields),
            'date_due': '2024-06-15', 'tags': ['x'], 'parsed_data': {}}
    mocker.patch('vms.clients.kanboard_client.get_leihanfragen_tasks', return_value=[task])
    update = mocker.patch('vms.clients.kanboard_client.update_task', return_value=True)

    result = sync_with_kanboard(user['id'])

    assert result == {'updated': 0, 'created': 0}
    update.assert_not_called()


@pytest.mark.integration
def test_sync_swallows_per_item_push_error_but_keeps_other_items(
        app_ctx, db_session, user, mocker):
    """A push failure on one task is logged and swallowed; the other task is
    still pushed and only the successful push counts."""
    from vms.clients.email_client import sync_with_kanboard

    _make_candidate(db_session, user['id'], email_id='kb_801', kanboard_task_id=801,
                    veranstaltungsname='Eins', tags=[])
    _make_candidate(db_session, user['id'], email_id='kb_802', kanboard_task_id=802,
                    veranstaltungsname='Zwei', tags=[])
    db_session.commit()

    task1 = {'id': '801', 'title': 'Stale1', 'description': '', 'date_due': '0',
             'tags': [], 'parsed_data': {}}
    task2 = {'id': '802', 'title': 'Stale2', 'description': '', 'date_due': '0',
             'tags': [], 'parsed_data': {}}
    mocker.patch('vms.clients.kanboard_client.get_leihanfragen_tasks',
                 return_value=[task1, task2])

    def _update(user_id, task_id, **kw):
        if task_id == 801:
            raise Exception('boom')
        return True
    update = mocker.patch('vms.clients.kanboard_client.update_task', side_effect=_update)

    result = sync_with_kanboard(user['id'])

    assert result == {'updated': 1, 'created': 0}
    assert update.call_count == 2


@pytest.mark.integration
def test_sync_empty_vms_datum_does_not_clear_kanboard_date(
        app_ctx, db_session, user, mocker):
    """A VMS candidate with no date must not clear a date already set in Kanboard:
    with everything else in sync, the empty datum triggers no push."""
    from vms.clients.email_client import sync_with_kanboard
    import vms.clients.kanboard_client as kb

    fields = dict(veranstaltungsname='Fest', email_address='a@b.de')
    _make_candidate(db_session, user['id'], email_id='kb_810', kanboard_task_id=810,
                    datum='', tags=[], subject='Fest', vorname_nachname=None, **fields)
    db_session.commit()

    task = {'id': '810', 'title': 'Fest', 'description': kb.build_task_description(fields),
            'date_due': '2024-06-15', 'tags': [], 'parsed_data': {}}
    mocker.patch('vms.clients.kanboard_client.get_leihanfragen_tasks', return_value=[task])
    update = mocker.patch('vms.clients.kanboard_client.update_task', return_value=True)

    result = sync_with_kanboard(user['id'])

    assert result == {'updated': 0, 'created': 0}
    update.assert_not_called()


@pytest.mark.integration
def test_sync_with_kanboard_swallows_per_item_create_error_but_keeps_other_items(
        app_ctx, db_session, user, mocker):
    from vms.clients.email_client import sync_with_kanboard
    from vms.domain.models import EmailCandidate

    broken_task = {
        'id': '901',
        'title': 'Kaputt',
        'description': 'x',
        'date_due': '0',
        'tags': [],
        'parsed_data': 'not-a-dict',  # .get() on a str raises AttributeError
    }
    good_task = {
        'id': '902',
        'title': 'Funktioniert',
        'description': 'y',
        'date_due': '0',
        'tags': [],
        'parsed_data': {},
    }
    mocker.patch('vms.clients.kanboard_client.get_leihanfragen_tasks', return_value=[broken_task, good_task])

    result = sync_with_kanboard(user['id'])

    assert result == {'updated': 0, 'created': 1}
    assert db_session.query(EmailCandidate).filter_by(kanboard_task_id=901).first() is None
    assert db_session.query(EmailCandidate).filter_by(kanboard_task_id=902).first() is not None


@pytest.mark.integration
def test_sync_with_kanboard_converts_date_due_unix_timestamp_to_iso(
        app_ctx, db_session, user, mocker):
    from vms.clients.email_client import sync_with_kanboard
    from vms.domain.models import EmailCandidate

    # 2024-06-15 12:00:00 UTC -- comfortably inside the day in Europe/Berlin too.
    ts = int(datetime(2024, 6, 15, 12, 0, tzinfo=timezone.utc).timestamp())
    task = {
        'id': '950', 'title': 'Datumstest', 'description': '', 'date_due': str(ts),
        'tags': [], 'parsed_data': {},
    }
    mocker.patch('vms.clients.kanboard_client.get_leihanfragen_tasks', return_value=[task])

    sync_with_kanboard(user['id'])

    row = db_session.query(EmailCandidate).filter_by(kanboard_task_id=950).one()
    assert row.datum == '2024-06-15'


@pytest.mark.integration
def test_sync_with_kanboard_unparseable_date_due_falls_back_to_to_iso_date(
        app_ctx, db_session, user, mocker):
    from vms.clients.email_client import sync_with_kanboard
    from vms.domain.models import EmailCandidate

    task = {
        'id': '960', 'title': 'Kaputtes Datum', 'description': '', 'date_due': 'nicht-numerisch',
        'tags': [], 'parsed_data': {},
    }
    mocker.patch('vms.clients.kanboard_client.get_leihanfragen_tasks', return_value=[task])

    sync_with_kanboard(user['id'])

    row = db_session.query(EmailCandidate).filter_by(kanboard_task_id=960).one()
    # to_iso_date can't parse it either -> original value preserved verbatim.
    assert row.datum == 'nicht-numerisch'


# ---------------------------------------------------------------------------
# get_calendar_events -- window edges + 3-way color (not duplicating
# test_email_routes.py, which already covers the fully-inside/fully-outside
# window cases via the route).
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_calendar_events_keeps_multiday_event_whose_end_reaches_into_window(
        app_ctx, db_session, user):
    from vms.clients.email_client import get_calendar_events

    _make_candidate(db_session, user['id'], veranstaltungsname='Überlappend',
                    datum='2024-03-28', end_date='2024-04-02', status='done',
                    email_id='cal-overlap')
    db_session.commit()

    events = get_calendar_events(range_start='2024-04-01', range_end='2024-06-01')

    assert [e['title'] for e in events] == ['Überlappend']


@pytest.mark.integration
def test_calendar_events_excludes_event_entirely_before_window_start(app_ctx, db_session, user):
    from vms.clients.email_client import get_calendar_events

    _make_candidate(db_session, user['id'], veranstaltungsname='Weit weg',
                    datum='2020-01-01', status='done', email_id='cal-before-start')
    db_session.commit()

    events = get_calendar_events(range_start='2024-04-01', range_end='2024-06-01')

    assert events == []


@pytest.mark.integration
def test_calendar_events_excludes_event_starting_at_or_after_window_end(app_ctx, db_session, user):
    from vms.clients.email_client import get_calendar_events

    _make_candidate(db_session, user['id'], veranstaltungsname='Genau am Ende',
                    datum='2024-06-01', status='done', email_id='cal-boundary')
    db_session.commit()

    # window end is exclusive: an event starting exactly on range_end is out.
    events = get_calendar_events(range_start='2024-04-01', range_end='2024-06-01')

    assert events == []


@pytest.mark.integration
def test_calendar_events_color_is_gray_for_past_amber_for_pending_green_for_active(
        app_ctx, db_session, user):
    from vms.clients.email_client import get_calendar_events

    _make_candidate(db_session, user['id'], veranstaltungsname='Vorbei',
                    datum='2000-01-01', status='done', email_id='cal-past')
    _make_candidate(db_session, user['id'], veranstaltungsname='Ausstehend',
                    datum='2999-01-01', status='pending', email_id='cal-pending')
    _make_candidate(db_session, user['id'], veranstaltungsname='Laufend',
                    datum='2999-01-02', status='processed', email_id='cal-active')
    db_session.commit()

    events = get_calendar_events()
    by_title = {e['title']: e['backgroundColor'] for e in events}

    assert by_title['Vorbei'] == '#6c757d'
    assert by_title['Ausstehend'] == '#f59e0b'
    assert by_title['Laufend'] == '#10b981'


@pytest.mark.integration
def test_calendar_events_skips_candidate_with_unparseable_datum(app_ctx, db_session, user):
    from vms.clients.email_client import get_calendar_events

    _make_candidate(db_session, user['id'], veranstaltungsname='Krummes Datum',
                    datum='nicht auswertbar', status='done', email_id='cal-bad-date')
    db_session.commit()

    events = get_calendar_events()

    assert events == []


# ---------------------------------------------------------------------------
# get_archived_candidates -- filter edges not already covered by
# test_email_routes.py
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_get_archived_candidates_tag_filter_narrows_results(app_ctx, db_session, user):
    from vms.clients.email_client import get_archived_candidates

    _make_candidate(db_session, user['id'], veranstaltungsname='Mit Tag',
                    datum='2020-01-01', status='returned', tags=['besonders'],
                    email_id='arch-tag-1')
    _make_candidate(db_session, user['id'], veranstaltungsname='Ohne Tag',
                    datum='2020-01-02', status='returned', tags=[],
                    email_id='arch-tag-2')
    db_session.commit()

    result = get_archived_candidates(tag_filter='besonders')

    assert [i['veranstaltungsname'] for i in result['items']] == ['Mit Tag']


@pytest.mark.integration
def test_get_archived_candidates_search_query_filters_by_name(app_ctx, db_session, user):
    from vms.clients.email_client import get_archived_candidates

    _make_candidate(db_session, user['id'], vorname_nachname='Erika Mustermann',
                    veranstaltungsname='Sommerfest', datum='2020-01-01', status='returned',
                    email_id='arch-search-1')
    _make_candidate(db_session, user['id'], vorname_nachname='Max Beispiel',
                    veranstaltungsname='Winterball', datum='2020-01-02', status='returned',
                    email_id='arch-search-2')
    db_session.commit()

    result = get_archived_candidates(search_query='Mustermann')

    assert [i['veranstaltungsname'] for i in result['items']] == ['Sommerfest']


@pytest.mark.integration
def test_get_archived_candidates_malformed_tags_fall_back_to_empty_list(app_ctx, db_session, user):
    from vms.clients.email_client import get_archived_candidates

    _make_candidate(db_session, user['id'], veranstaltungsname='Kaputte Tags',
                    datum='2020-01-01', status='returned', tags='{invalid',
                    email_id='arch-bad-tags')
    db_session.commit()

    result = get_archived_candidates()
    row = next(i for i in result['items'] if i['veranstaltungsname'] == 'Kaputte Tags')

    assert row['tags'] == []


@pytest.mark.integration
def test_get_archived_candidates_date_filter_matches_exact_date(app_ctx, db_session, user):
    from vms.clients.email_client import get_archived_candidates

    _make_candidate(db_session, user['id'], veranstaltungsname='Richtiger Tag',
                    datum='2020-06-15', status='returned', email_id='arch-date-1')
    _make_candidate(db_session, user['id'], veranstaltungsname='Anderer Tag',
                    datum='2020-06-16', status='returned', email_id='arch-date-2')
    db_session.commit()

    result = get_archived_candidates(date_filter='2020-06-15')

    assert [i['veranstaltungsname'] for i in result['items']] == ['Richtiger Tag']


@pytest.mark.integration
def test_get_archived_candidates_invalid_date_filter_should_not_silently_return_everything(
        app_ctx, db_session, user):
    from vms.clients.email_client import get_archived_candidates

    _make_candidate(db_session, user['id'], veranstaltungsname='Archiviert Eins',
                    datum='2020-06-15', status='returned', email_id='arch-invalid-1')
    _make_candidate(db_session, user['id'], veranstaltungsname='Archiviert Zwei',
                    datum='2020-06-16', status='returned', email_id='arch-invalid-2')
    db_session.commit()

    result = get_archived_candidates(date_filter='not-a-real-date')

    # Domain-correct: an unparseable filter should yield no matches, not the
    # entire unfiltered archive.
    assert result['items'] == []
    assert result['total'] == 0


@pytest.mark.integration
def test_get_archived_candidates_db_error_returns_error_shape_without_crashing(
        app_ctx, db_session, user):
    """A negative LIMIT reaches Postgres (invalid) and blows up inside the
    query execution -- outside the inner date_filter try/except -- exercising
    the outer swallow-and-return-empty-with-error arm (L807-817)."""
    from vms.clients.email_client import get_archived_candidates

    _make_candidate(db_session, user['id'], veranstaltungsname='Irrelevant',
                    datum='2020-01-01', status='returned', email_id='arch-error')
    db_session.commit()

    result = get_archived_candidates(limit=-1)

    assert result['items'] == []
    assert result['total'] == 0
    assert result['pages'] == 0
    assert 'error' in result


# ---------------------------------------------------------------------------
# Kanboard-Import: Status hängt am Fälligkeitsdatum
# Spec: docs/specs/vorgangslisten-und-datumspflicht.md
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_kanboard_task_ohne_datum_wird_pending_importiert(app_ctx, db_session, user, mocker):
    """Ein Verleih ohne Datum darf nicht entstehen. Statt den Task zu verwerfen,
    landet er als Anfrage in 'Offen', wo das Datum nachgepflegt wird."""
    from vms.clients.email_client import sync_with_kanboard
    from vms.domain.models import CandidateStatus, EmailCandidate

    mocker.patch("vms.clients.kanboard_client.get_leihanfragen_tasks", return_value=[
        {"id": 4711, "title": "Ohne Termin", "description": "", "date_due": "0",
         "parsed_data": {}, "tags": []},
    ])

    result = sync_with_kanboard(user["id"])

    assert result["created"] == 1
    row = db_session.query(EmailCandidate).filter_by(kanboard_task_id=4711).one()
    assert row.status == CandidateStatus.PENDING.value


@pytest.mark.integration
def test_kanboard_task_mit_datum_bleibt_processed(app_ctx, db_session, user, mocker):
    from vms.clients.email_client import sync_with_kanboard
    from vms.domain.models import CandidateStatus, EmailCandidate

    mocker.patch("vms.clients.kanboard_client.get_leihanfragen_tasks", return_value=[
        {"id": 4712, "title": "Mit Termin", "description": "",
         "date_due": "1785283200", "parsed_data": {}, "tags": []},
    ])

    result = sync_with_kanboard(user["id"])

    assert result["created"] == 1
    row = db_session.query(EmailCandidate).filter_by(kanboard_task_id=4712).one()
    assert row.status == CandidateStatus.PROCESSED.value
    assert row.datum

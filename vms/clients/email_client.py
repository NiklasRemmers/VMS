"""
Email Client for importing loan requests from IMAP mailboxes.
Supports per-user configuration.
Uses SQLAlchemy with PostgreSQL.
"""
import imaplib
import email
import logging
from email.header import decode_header
from email.utils import parsedate_to_datetime
import os
import re
import uuid
from datetime import date, datetime, timezone, timedelta
import base64
from typing import List, Dict, Optional, Any
from vms.infra.security import decrypt_value
from vms.domain.database import get_session, get_user_settings
import json
import vms.clients.kanboard_client as kanboard_client
from sqlalchemy import func, or_, false

from vms.domain.models import (
    EmailCandidate, EmailSyncState, CandidateStatus, ACTIVE_STATUSES,
    TERMINAL_STATUSES,
    set_candidate_status, status_label,
    format_de_date, parse_flexible_date, to_iso_date,
)

logger = logging.getLogger(__name__)

# Zugriffsmodell: VMS trennt bewusst nicht nach Eigentümer -- jeder angemeldete
# Nutzer sieht und bearbeitet alle Vorgänge (siehe auch den Docstring von
# get_archived_candidates: "shared across all users"). Die Funktionen hier nahmen
# früher ein `user_id` entgegen, das nie ausgewertet wurde; das täuschte eine
# Mandantengrenze vor, die es nie gab. Der Parameter wurde entfernt, nicht
# implementiert.


def get_imap_connection(settings):
    """
    Connect to IMAP server using provided settings.
    Supports manual configuration only.
    """
    server = settings.get('imap_server')
    port = settings.get('imap_port') or 993
    user = settings.get('imap_user')
    # Decrypt password
    password = decrypt_value(settings.get('encrypted_imap_password'))
    
    if not server or not user or not password:
        raise ValueError("IMAP-Konfiguration unvollständig. Bitte prüfen Sie die Einstellungen.")
        
    try:
        conn = imaplib.IMAP4_SSL(server, port)
        conn.login(user, password)
        return conn, user
    except Exception as e:
        raise ValueError(f"IMAP-Verbindung fehlgeschlagen: {e}")


def get_last_sync() -> Optional[datetime]:
    """Get timestamp of last email sync (global across all users).
    
    Returns the most recent sync timestamp from any user to prevent
    duplicate email fetches when multiple users sync.
    """
    with get_session() as s:
        row = s.query(func.max(EmailSyncState.last_sync)).scalar()
        if row:
            # Ensure it is treated as UTC (assuming DB stores naive UTC)
            if row.tzinfo is None:
                return row.replace(tzinfo=timezone.utc)
            return row
    return None


def update_last_sync(user_id: int, reset_to_start_of_year: bool = False):
    """Update last sync timestamp globally (for all users).
    
    Updates the sync state for the triggering user and ensures
    all other users' sync states are also updated to prevent
    duplicate email fetches.
    """
    if reset_to_start_of_year:
        current_year = datetime.now().year
        timestamp = datetime(current_year, 1, 1, tzinfo=timezone.utc)
    else:
        timestamp = datetime.now(timezone.utc)
    
    with get_session() as s:
        # Update all existing sync state rows
        s.query(EmailSyncState).update({EmailSyncState.last_sync: timestamp})
        
        # Ensure the current user has a row
        row = s.query(EmailSyncState).filter_by(user_id=user_id).first()
        if not row:
            s.add(EmailSyncState(user_id=user_id, last_sync=timestamp))


def decode_mime_header(header_value: str) -> str:
    """Decode MIME encoded header value."""
    if not header_value:
        return ''
    decoded_parts = []
    for part, encoding in decode_header(header_value):
        if isinstance(part, bytes):
            decoded_parts.append(part.decode(encoding or 'utf-8', errors='replace'))
        else:
            decoded_parts.append(part)
    return ' '.join(decoded_parts)


def get_email_body(msg) -> str:
    """Extract plain text body from email message."""
    body = ''
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get('Content-Disposition', ''))
            if content_type == 'text/plain' and 'attachment' not in content_disposition:
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or 'utf-8'
                body = payload.decode(charset, errors='replace')
                break
    else:
        payload = msg.get_payload(decode=True)
        charset = msg.get_content_charset() or 'utf-8'
        if payload:
            body = payload.decode(charset, errors='replace')
    return body


def parse_email_content(content: str) -> Dict[str, str]:
    """Parse the email content to extract structured fields. Handles multi-line values."""
    result = {}
    known_fields = {
        'Vor- und Nachname': 'vorname_nachname',
        'Anschrift': 'anschrift',
        'E-Mail-Adresse': 'email_address',
        'Telefonnummer': 'telefon',
        'Name der Veranstaltung': 'veranstaltungsname',
        'Art der Veranstaltung': 'veranstaltungsart',
        'Veranstaltungsort': 'veranstaltungsort',
        'Veranstaltungsbereich': 'veranstaltungsbereich',
        'Erwartete Personenzahl': 'personenzahl',
        'Datum': 'datum',
        'Benötigtes Material': 'material',
        'Was du uns sonst noch mitteilen möchtest': 'sonstiges',
        'Ich habe die Rahmenbedingungen gelesen': 'rahmenbedingungen',
    }

    current_field = None
    current_value = []

    lines = content.splitlines()
    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Check if line starts with a known field
        found_field = None
        for display_name, internal_name in known_fields.items():
            if line.lower().startswith(display_name.lower() + ':'):
                found_field = internal_name
                # Extract value from same line if present
                value_part = line[len(display_name) + 1:].strip()
                
                # Save previous field if existing
                if current_field:
                    result[current_field] = '\n'.join(current_value).strip()
                
                # Start new field
                current_field = found_field
                current_value = [value_part] if value_part else []
                break
        
        if not found_field and current_field:
            # Append to current field
            current_value.append(line)

    # Save last field
    if current_field:
        result[current_field] = '\n'.join(current_value).strip()

    return result


def extract_form_section(content: str) -> str:
    """Extract form section from email content."""
    if not content:
        return ''
    lines = content.split('\n')
    result_lines = []
    in_form_section = False
    if 'Vor- und Nachname:' not in content:
        return content
    for line in lines:
        if 'Vor- und Nachname:' in line:
            in_form_section = True
        if in_form_section:
            result_lines.append(line)
            if 'Ich habe die Rahmenbedingungen gelesen:' in line:
                break
    return '\n'.join(result_lines).strip()


def is_loan_request_email(subject: str) -> bool:
    """Check if email subject matches loan request pattern.
    
    Only matches subjects STARTING with: [stuve.anlage] Bestellung ...
    Rejects reply/forward prefixes like Re:, AW:, Fwd:, WG:
    """
    pattern = r'^\s*\[stuve\.anlage\]\s+Bestellung\s+.+'
    return bool(re.match(pattern, subject, re.IGNORECASE))


def fetch_emails_for_user(user_id: int) -> List[Dict]:
    """Fetch new emails for specific user using their settings."""
    settings = get_user_settings(user_id)
    if not settings:
        raise ValueError("Benutzer hat keine E-Mail-Einstellungen.")
        
    emails = []
    last_sync = get_last_sync()
    seen_message_ids = set()
    
    conn = None
    try:
        conn, email_addr = get_imap_connection(settings)
        
        status, folder_list = conn.list()
        if status != 'OK':
            conn.logout()
            return emails
            
        if last_sync:
            date_str = last_sync.strftime('%d-%b-%Y')
            search_criteria = f'(SINCE "{date_str}")'
        else:
            year_start = datetime(datetime.now().year, 1, 1)
            date_str = year_start.strftime('%d-%b-%Y')
            search_criteria = f'(SINCE "{date_str}")'
            
        for folder_info in folder_list:
            try:
                folder_str = folder_info.decode()
                match = re.search(r'\(([^)]*)\) "([^"]*)" (.+)', folder_str)
                folder_name = match.group(3).strip('"') if match else folder_str.split('"')[-2]
                
                if not folder_name: continue
                
                mailbox = f'"{folder_name}"' if ' ' in folder_name and not folder_name.startswith('"') else folder_name
                
                status, _ = conn.select(mailbox, readonly=True)
                if status != 'OK': continue
                
                final_criteria = ['(HEADER Subject "[stuve.anlage] Bestellung Anlagenreferat")']
                if last_sync:
                     final_criteria.append(search_criteria)
                     
                status, messages = conn.search(None, *final_criteria)
                 
                if status != 'OK' or not messages[0]:
                    continue
                    
                for msg_id in messages[0].split():
                    try:
                        _, msg_data = conn.fetch(msg_id, '(RFC822)')
                        email_body = msg_data[0][1]
                        message = email.message_from_bytes(email_body)
                        msg_uid = message.get('Message-ID')
                        
                        if not msg_uid or msg_uid in seen_message_ids: continue
                        seen_message_ids.add(msg_uid)
                        
                        subject = decode_mime_header(message.get('Subject', ''))
                        if not is_loan_request_email(subject): continue
                        
                        sender = decode_mime_header(message.get('From', ''))
                        date_str_email = message.get('Date', '')
                        try:
                            received_at = parsedate_to_datetime(date_str_email)
                        except Exception:
                            # Der Fallback auf "jetzt" verfälscht den Vergleich mit
                            # last_sync direkt darunter: die Mail gilt immer als neu.
                            logger.warning("Date-Header %r nicht interpretierbar; "
                                           "nutze aktuelle Zeit, Sync-Fenster kann "
                                           "dadurch danebenliegen", date_str_email,
                                           exc_info=True)
                            # Ohne bekanntes Datum als "ältestmöglich" behandeln:
                            # so unterläuft die Mail den last_sync-Filter nicht,
                            # statt (via now()) für immer als neu zu gelten.
                            received_at = datetime.min.replace(tzinfo=timezone.utc)

                        # Convert to UTC for accurate comparison
                        received_utc = received_at.astimezone(timezone.utc) if received_at.tzinfo else received_at.replace(tzinfo=timezone.utc)
                        if last_sync:
                            last_sync_utc = last_sync.astimezone(timezone.utc) if last_sync.tzinfo else last_sync.replace(tzinfo=timezone.utc)
                            if received_utc < last_sync_utc:
                                continue
                            
                        body = get_email_body(message)
                        parsed = parse_email_content(body)
                        
                        emails.append({
                            'email_id': msg_uid,
                            'subject': subject,
                            'sender': sender,
                            'received_at': received_at.isoformat(),
                            'raw_content': body,
                            **parsed
                        })
                    except Exception as e:
                        print(f"Error reading email {msg_id}: {e}")
                        continue
            except Exception as e:
                print(f"Error accessing folder {folder_info}: {e}")
                continue
                
        conn.logout()
        return emails
        
    except Exception as e:
        raise Exception(f"IMAP-Fehler: {str(e)}")


def save_candidates(emails: List[Dict], user_id: int) -> int:
    """Save email candidates to database for specific user."""
    count = 0
    
    with get_session() as s:
        for email_data in emails:
            try:
                # Check if already exists
                existing = s.query(EmailCandidate).filter_by(
                    email_id=email_data.get('email_id')
                ).first()
                
                if existing:
                    continue
                
                candidate = EmailCandidate(
                    user_id=user_id,
                    email_id=email_data.get('email_id'),
                    subject=email_data.get('subject'),
                    sender=email_data.get('sender'),
                    received_at=email_data.get('received_at'),
                    vorname_nachname=email_data.get('vorname_nachname'),
                    anschrift=email_data.get('anschrift'),
                    email_address=email_data.get('email_address'),
                    telefon=email_data.get('telefon'),
                    veranstaltungsname=email_data.get('veranstaltungsname'),
                    veranstaltungsart=email_data.get('veranstaltungsart'),
                    veranstaltungsort=email_data.get('veranstaltungsort'),
                    veranstaltungsbereich=email_data.get('veranstaltungsbereich'),
                    personenzahl=email_data.get('personenzahl'),
                    datum=to_iso_date(email_data.get('datum')),
                    material=email_data.get('material'),
                    sonstiges=email_data.get('sonstiges'),
                    rahmenbedingungen=email_data.get('rahmenbedingungen'),
                    raw_content=email_data.get('raw_content'),
                )
                s.add(candidate)
                count += 1
            except Exception as e:
                print(f"Error saving candidate: {e}")
                continue
    
    return count


def create_manual_candidate(form_data: Dict, user_id: int) -> int:
    """Create a loan request candidate manually (not from an email).

    Inserted directly with status='processed' so it lands in the
    "Erledigte Anfragen" list. Returns the new candidate id.
    """
    with get_session() as s:
        candidate = EmailCandidate(
            user_id=user_id,
            email_id=f"manual_{uuid.uuid4()}",
            vorname_nachname=form_data.get('vorname_nachname'),
            anschrift=form_data.get('anschrift'),
            email_address=form_data.get('email_address'),
            veranstaltungsname=form_data.get('veranstaltungsname'),
            veranstaltungsort=form_data.get('veranstaltungsort'),
            personenzahl=form_data.get('personenzahl'),
            datum=to_iso_date(form_data.get('datum')),
            end_date=to_iso_date(form_data.get('end_date')),
            raw_content=form_data.get('raw_content'),
            tags=form_data.get('tags') or [],
            kanboard_task_id=form_data.get('kanboard_task_id'),
            responsible_user_id=form_data.get('responsible_user_id'),
            status=CandidateStatus.PROCESSED.value,
            contract_created=False,
        )
        s.add(candidate)
        s.flush()
        return candidate.id


def sync_emails(user_id: int) -> int:
    """Sync emails for specific user."""
    emails = fetch_emails_for_user(user_id)
    count = save_candidates(emails, user_id)
    update_last_sync(user_id)
    return count


def get_candidates(status_filter='pending'):
    """Get candidates filtered by status (shared across all users)."""
    with get_session() as s:
        q = s.query(EmailCandidate)
        
        if status_filter != 'ALL':
            q = q.filter(EmailCandidate.status == status_filter)
        
        q = q.order_by(EmailCandidate.received_at.desc())
        rows = q.all()
        
        result = []
        for row in rows:
            d = row.to_dict()
            # Handle tags serialization
            if d.get('tags') and isinstance(d['tags'], str):
                try:
                    d['tags'] = json.loads(d['tags'])
                except:
                    d['tags'] = []
            elif not d.get('tags'):
                d['tags'] = []
            # Handle datetime serialization
            for key in ['received_at', 'created_at', 'returned_at']:
                if d.get(key) and hasattr(d[key], 'isoformat'):
                    d[key] = d[key].isoformat()
            # Normalize the loan date to DD.MM.YYYY for display and expose a
            # display-formatted end date. end_date itself stays ISO because the
            # <input type="date"> in the edit form needs the ISO value.
            d['datum'] = format_de_date(d.get('datum'))
            d['end_date_display'] = format_de_date(d.get('end_date'))
            result.append(d)

        return result


def mark_candidate_processed(candidate_id):
    """Vertrag zurücknehmen: zurück auf 'processed', contract_created wieder False."""
    with get_session() as s:
        return set_candidate_status(s, candidate_id, CandidateStatus.PROCESSED) is not None


def mark_candidate_done(candidate_id):
    """Leihvertrag wurde erzeugt: auf 'done', contract_created auf True."""
    with get_session() as s:
        return set_candidate_status(s, candidate_id, CandidateStatus.DONE) is not None


def delete_candidate(candidate_id):
    with get_session() as s:
        row = s.query(EmailCandidate).filter_by(id=candidate_id).first()
        if row:
            s.delete(row)
            return True
    return False


def update_candidate(candidate_id, form_data: Dict):
    valid_fields = [
        'subject', 'sender', 'vorname_nachname', 'anschrift', 'email_address',
        'telefon', 'veranstaltungsname', 'veranstaltungsart', 'veranstaltungsort',
        'veranstaltungsbereich', 'personenzahl', 'datum', 'material',
        'sonstiges', 'rahmenbedingungen', 'raw_content', 'contract_created',
        'kanboard_task_id', 'end_date', 'tags', 'status',
        'return_note', 'returned_at', 'responsible_user_id'
    ]
    
    with get_session() as s:
        row = s.query(EmailCandidate).filter_by(id=candidate_id).first()
        if not row:
            return False
        
        for key in valid_fields:
            if key in form_data:
                value = form_data[key]
                # Store dates ISO-normalized per the "intern ISO" convention.
                if key in ('datum', 'end_date'):
                    value = to_iso_date(value)
                setattr(row, key, value)

        return True


def get_candidate_by_id(candidate_id):
    with get_session() as s:
        row = s.query(EmailCandidate).filter_by(id=candidate_id).first()
        
        if row:
            c = row.to_dict()
            # Handle tags
            if c.get('tags') and isinstance(c['tags'], str):
                try:
                    c['tags'] = json.loads(c['tags'])
                except:
                    c['tags'] = []
            elif not c.get('tags'):
                c['tags'] = []
            # Handle datetime serialization
            for key in ['received_at', 'created_at']:
                if c.get(key) and hasattr(c[key], 'isoformat'):
                    c[key] = c[key].isoformat()
            return c
    return None


def save_kanboard_task_id(candidate_id, task_id):
    with get_session() as s:
        row = s.query(EmailCandidate).filter_by(id=candidate_id).first()
        if row:
            row.kanboard_task_id = task_id


def sync_with_kanboard(user_id: int):
    """Sync specific user's candidates with Kanboard."""
    try:
        tasks = kanboard_client.get_leihanfragen_tasks(user_id)
    except Exception as e:
        print(f"Kanboard sync error for user {user_id}: {e}")
        return {'updated': 0, 'created': 0}
    
    created = 0
    updated = 0
    
    with get_session() as s:
        # Load existing candidates linked to Kanboard tasks (shared across users)
        existing_candidates = s.query(EmailCandidate).filter(
            EmailCandidate.kanboard_task_id.isnot(None)
        ).all()
        
        # Map task_id -> candidate for quick lookup
        existing_map = {c.kanboard_task_id: c for c in existing_candidates}
        
        for task in tasks:
            tid = int(task['id'])
            parsed = task.get('parsed_data', {})
            tags = task.get('tags', [])

            if tid in existing_map:
                # VMS is the source of truth: push the local candidate's state into
                # the Kanboard task instead of overwriting VMS from Kanboard (which
                # used to wipe fields Kanboard has no column for, e.g. email/name).
                # Push only when something diverged (idempotent) and swallow a
                # single task's failure so the rest still sync (best-effort).
                candidate = existing_map[tid]
                plan = kanboard_client.plan_task_push(candidate.to_dict(), task)
                if plan is not None:
                    try:
                        kanboard_client.update_task(user_id, tid, **plan)
                        updated += 1
                    except Exception as e:
                        print(f"Error pushing candidate to Kanboard task {tid}: {e}")
            else:
                # New, unlinked Kanboard task -> pull it in as a new candidate.
                # This stays the entry channel for fresh requests.
                datum = kanboard_client.kanboard_date_due_to_iso(task.get('date_due', ''))
                # Ein Verleih ohne Datum darf nicht entstehen. Statt den Task zu
                # verwerfen, kommt er als Anfrage herein -- dort ist datumslos
                # erlaubt und das Datum wird beim Bearbeiten nachgepflegt.
                status = (CandidateStatus.PROCESSED if parse_flexible_date(datum)
                          else CandidateStatus.PENDING)
                try:
                    candidate = EmailCandidate(
                        user_id=user_id,
                        kanboard_task_id=tid,
                        subject=task.get('title'),
                        raw_content=task.get('description'),
                        status=status.value,
                        vorname_nachname=parsed.get('vorname_nachname'),
                        anschrift=parsed.get('rechnungsanschrift', ''),
                        email_address=parsed.get('email_address'),
                        telefon=parsed.get('telefon'),
                        veranstaltungsname=task.get('title'),
                        veranstaltungsart=parsed.get('veranstaltungsart'),
                        veranstaltungsort=parsed.get('veranstaltungsort'),
                        veranstaltungsbereich=parsed.get('veranstaltungsbereich'),
                        personenzahl=parsed.get('personenzahl'),
                        datum=datum,
                        material=parsed.get('material'),
                        sonstiges=parsed.get('sonstiges'),
                        rahmenbedingungen=parsed.get('rahmenbedingungen'),
                        tags=tags,
                        email_id=f"kb_{tid}",
                    )
                    s.add(candidate)
                    created += 1
                except Exception as e:
                    print(f"Error creating candidate from task {tid}: {e}")
    
    return {'updated': updated, 'created': created}


def _parse_calendar_date(value):
    """Parse a stored date into a date object (or None). Thin alias for the
    canonical parser so the calendar and the write paths stay in sync."""
    return parse_flexible_date(value)


def get_calendar_events(range_start=None, range_end=None):
    """Get calendar events for dashboard (shared across all users).

    range_start / range_end are the ISO date/datetime strings FullCalendar sends
    for the visible window; when given, only events overlapping [start, end) are
    returned. Candidates whose date cannot be parsed are logged and skipped
    (instead of being dropped silently)."""
    candidates = get_candidates('ALL')
    events = []
    today = datetime.now().date()

    win_start = _parse_calendar_date(range_start)
    win_end = _parse_calendar_date(range_end)

    for c in candidates:
        event_date = _parse_calendar_date(c.get('datum'))
        if event_date is None:
            # Keep these visible in the logs so undated/misformatted requests can
            # be found and fixed, rather than vanishing from the calendar unnoticed.
            print(f"[calendar] skipping candidate {c.get('id')} "
                  f"({c.get('veranstaltungsname') or c.get('subject')!r}): "
                  f"unparseable datum {c.get('datum')!r}")
            continue

        end_date = _parse_calendar_date(c.get('end_date'))
        # FullCalendar treats all-day `end` as exclusive, so add one day so a
        # multi-day loan spans through its last day.
        span_end = (end_date if end_date and end_date >= event_date else event_date)

        # Skip events entirely outside the requested window (if one was given).
        if win_start and span_end < win_start:
            continue
        if win_end and event_date >= win_end:
            continue

        status = c.get('status', 'pending')
        if span_end < today:
            color = '#6c757d'  # Gray (Past)
        elif status in ACTIVE_STATUSES:
            color = '#10b981'  # Green (processed/done)
        else:
            color = '#f59e0b'  # Amber/Yellow (pending)

        event = {
            'title': c.get('veranstaltungsname') or c.get('subject'),
            'start': event_date.strftime('%Y-%m-%d'),
            'url': f"/emails?highlight={c['id']}",
            'backgroundColor': color,
            'borderColor': color,
            'extendedProps': {
                'status': status,
                # Rohwert bleibt für die Einfärbung, die Vorschau zeigt das Label.
                'status_label': status_label(status),
                'location': c.get('veranstaltungsort'),
                'persons': c.get('personenzahl'),
                'name': c.get('vorname_nachname'),
                'tags': c.get('tags') if isinstance(c.get('tags'), list) else []
            }
        }
        if end_date and end_date > event_date:
            event['end'] = (end_date + timedelta(days=1)).strftime('%Y-%m-%d')
        events.append(event)

    return events


def get_archived_candidates(page: int = 1, limit: int = 10,
                            search_query: str = None, date_filter: str = None,
                            tag_filter: str = None) -> Dict:
    """Archivierte Vorgänge (abgeschlossen oder verfallen) mit Paginierung.

    Was ins Archiv gehört, entscheidet allein `vorgang.zielliste`: abgeschlossen
    (returned/invoiced) oder eine verfallene Anfrage, deren Termin verstrichen
    ist. Vorher stand hier ein zweiter, abweichender Datumsvergleich in SQL --
    zwei strikte Regexe, die die von `parse_flexible_date` akzeptierten Formate
    nicht kannten und dadurch andere Zeilen trafen als jede andere Liste. Er zog
    außerdem laufende Verleihe und offene Rechnungen mit ins Archiv.

    SQL grenzt nur noch auf die überhaupt archivierbaren Stati ein und wendet die
    Such-/Datums-/Tag-Filter an; Zielliste, Sortierung und Paginierung entscheiden
    danach in Python -- wie in den übrigen Listen dieses Moduls.
    """
    from vms.domain.vorgang import Zielliste, zielliste

    offset = (page - 1) * limit

    try:
        if page <= 0 or limit <= 0:
            raise ValueError(f"Ungültige Paginierung: page={page}, limit={limit}")

        heute = datetime.now().date()

        with get_session() as s:
            # Grobfilter: nur diese Stati können überhaupt im Archiv landen.
            archivierbar = [st.value for st in TERMINAL_STATUSES]
            archivierbar.append(CandidateStatus.PENDING.value)
            q = s.query(EmailCandidate).filter(EmailCandidate.status.in_(archivierbar))

            # Apply filters
            if search_query:
                term = f"%{search_query}%"
                q = q.filter(or_(
                    EmailCandidate.vorname_nachname.ilike(term),
                    EmailCandidate.subject.ilike(term),
                    EmailCandidate.veranstaltungsname.ilike(term)
                ))
                
            if date_filter:
                try:
                    # Input is YYYY-MM-DD
                    d = datetime.strptime(date_filter, '%Y-%m-%d')
                    filter_de = d.strftime('%d.%m.%Y')
                    filter_iso = date_filter
                    q = q.filter(or_(
                        EmailCandidate.datum == filter_de,
                        EmailCandidate.datum == filter_iso
                    ))
                except Exception:
                    # Ein unbrauchbarer Filter darf nicht still zu "kein Filter"
                    # degradieren (sonst käme das ganze Archiv zurück) -- er kann
                    # per Definition auf nichts matchen.
                    logger.warning("Datumsfilter %r unbrauchbar; liefere leeres "
                                   "Ergebnis statt ungefiltert", date_filter, exc_info=True)
                    q = q.filter(false())
                    
            if tag_filter:
                q = q.filter(EmailCandidate.tags.contains([tag_filter]))

            # Die eigentliche Archiv-Entscheidung, zusammen mit dem Sortier-
            # schlüssel: `datum` wird unten für die Anzeige überschrieben.
            archiviert = [
                (parse_flexible_date(row.datum), row.to_dict())
                for row in q.all()
                if zielliste(row.status, row.datum, row.end_date, heute)
                is Zielliste.ARCHIV
            ]

            # Neueste zuerst, Undatierte ans Ende.
            archiviert.sort(key=lambda p: (p[0] is not None, p[0] or date.min),
                            reverse=True)

            total = len(archiviert)

            results = []
            for _, d in archiviert[offset:offset + limit]:
                if d.get('tags') and isinstance(d['tags'], str):
                    try:
                        d['tags'] = json.loads(d['tags'])
                    except:
                        d['tags'] = []
                elif not d.get('tags'):
                    d['tags'] = []

                for key in ['received_at', 'created_at', 'returned_at']:
                    if d.get(key) and hasattr(d[key], 'isoformat'):
                        d[key] = d[key].isoformat()
                d['datum'] = format_de_date(d.get('datum'))
                d['end_date_display'] = format_de_date(d.get('end_date'))
                results.append(d)

            return {
                'items': results,
                'total': total,
                'page': page,
                'limit': limit,
                'pages': (total + limit - 1) // limit if limit > 0 else 0
            }
    except Exception as e:
        print(f"Error fetching archived candidates: {e}")
        # Return empty safe result instead of crashing
        return {
            'items': [],
            'total': 0,
            'page': page,
            'limit': limit,
            'pages': 0,
            'error': str(e)
        }

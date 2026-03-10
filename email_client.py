"""
Email Client for importing loan requests from IMAP mailboxes.
Supports per-user configuration.
Uses SQLAlchemy with PostgreSQL.
"""
import imaplib
import email
from email.header import decode_header
from email.utils import parsedate_to_datetime
import os
import re
from datetime import datetime, timezone
import base64
from typing import List, Dict, Optional, Any
from security import decrypt_value
from database import get_session, get_user_settings
import json
import kanboard_client
from sqlalchemy import func, or_, and_, text, desc, case

# ... existing imports ...
from models import EmailCandidate, EmailSyncState
from database import get_session, get_user_settings


def get_imap_connection(settings, email_address=None):
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


def get_last_sync(user_id) -> Optional[datetime]:
    """Get timestamp of last email sync for user."""
    with get_session() as s:
        row = s.query(EmailSyncState).filter_by(user_id=user_id).first()
        if row and row.last_sync:
            # Ensure it is treated as UTC (assuming DB stores naive UTC)
            if row.last_sync.tzinfo is None:
                return row.last_sync.replace(tzinfo=timezone.utc)
            return row.last_sync
    return None


def update_last_sync(user_id: int, reset_to_start_of_year: bool = False):
    """Update last sync timestamp for user."""
    if reset_to_start_of_year:
        current_year = datetime.now().year
        timestamp = datetime(current_year, 1, 1, tzinfo=timezone.utc)
    else:
        timestamp = datetime.now(timezone.utc)
    
    with get_session() as s:
        row = s.query(EmailSyncState).filter_by(user_id=user_id).first()
        if row:
            row.last_sync = timestamp
        else:
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
    last_sync = get_last_sync(user_id)
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
                        except:
                            received_at = datetime.now(timezone.utc)
                            
                        # Convert both to UTC for accurate comparison
                        received_utc = received_at.astimezone(timezone.utc) if received_at.tzinfo else received_at.replace(tzinfo=timezone.utc)
                        last_sync_utc = last_sync.astimezone(timezone.utc) if last_sync.tzinfo else last_sync.replace(tzinfo=timezone.utc)
                        if last_sync and received_utc < last_sync_utc:
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
                    datum=email_data.get('datum'),
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


def sync_emails(user_id: int) -> int:
    """Sync emails for specific user."""
    emails = fetch_emails_for_user(user_id)
    count = save_candidates(emails, user_id)
    update_last_sync(user_id)
    return count


def get_candidates(status_filter='pending', user_id=None):
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
            result.append(d)
        
        return result


def mark_candidate_processed(candidate_id, user_id=None):
    with get_session() as s:
        row = s.query(EmailCandidate).filter_by(id=candidate_id).first()
        if row:
            row.status = 'processed'
            row.contract_created = False
            return True
    return False


def mark_candidate_done(candidate_id, user_id=None):
    with get_session() as s:
        row = s.query(EmailCandidate).filter_by(id=candidate_id).first()
        if row:
            row.status = 'done'
            row.contract_created = True
            return True
    return False


def mark_candidate_pending(candidate_id, user_id=None):
    with get_session() as s:
        row = s.query(EmailCandidate).filter_by(id=candidate_id).first()
        if row:
            row.status = 'pending'
            row.contract_created = False


def delete_candidate(candidate_id, user_id=None):
    with get_session() as s:
        row = s.query(EmailCandidate).filter_by(id=candidate_id).first()
        if row:
            s.delete(row)
            return True
    return False


def update_candidate(candidate_id, form_data: Dict, user_id: int = None):
    valid_fields = [
        'subject', 'sender', 'vorname_nachname', 'anschrift', 'email_address',
        'telefon', 'veranstaltungsname', 'veranstaltungsart', 'veranstaltungsort',
        'veranstaltungsbereich', 'personenzahl', 'datum', 'material',
        'sonstiges', 'rahmenbedingungen', 'raw_content', 'contract_created',
        'kanboard_task_id', 'end_date', 'tags', 'status',
        'return_note', 'returned_at'
    ]
    
    with get_session() as s:
        row = s.query(EmailCandidate).filter_by(id=candidate_id).first()
        if not row:
            return False
        
        for key in valid_fields:
            if key in form_data:
                setattr(row, key, form_data[key])
        
        return True


def get_candidate_by_id(candidate_id, user_id=None):
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


def save_kanboard_task_id(candidate_id, task_id, user_id=None):
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
            
            # Convert Kanboard date_due (Unix timestamp) to DD.MM.YYYY
            datum = ''
            date_due = task.get('date_due', '')
            if date_due and date_due != '0':
                try:
                    from datetime import datetime as dt_cls
                    try:
                        from zoneinfo import ZoneInfo
                    except ImportError:
                        # Fallback for Python < 3.9
                        from dateutil.tz import gettz as ZoneInfo
                        
                    ts = int(date_due)
                    # Use Europe/Berlin to interpret the midnight timestamp correctly
                    # Kanboard midnight (CET/CEST) -> Correct Date
                    tz = ZoneInfo('Europe/Berlin')
                    datum = dt_cls.fromtimestamp(ts, tz=tz).strftime('%d.%m.%Y')
                except (ValueError, OSError):
                    datum = date_due  # Use as-is if not a timestamp

            if tid in existing_map:
                # Update existing candidate if needed
                candidate = existing_map[tid]
                changed = False
                
                # Check for tag changes
                current_tags = candidate.tags or []
                # Ensure we compare list contents, irrelevant of order if we treat them as a set of tags
                if set(current_tags) != set(tags):
                    candidate.tags = tags
                    changed = True

                # Check and update other fields
                fields_to_update = {
                    'subject': task.get('title'),
                    'veranstaltungsname': task.get('title'),
                    'raw_content': task.get('description'),
                    'datum': datum,
                    'vorname_nachname': parsed.get('vorname_nachname'),
                    'anschrift': parsed.get('rechnungsanschrift', ''),
                    'email_address': parsed.get('email_address'),
                    'telefon': parsed.get('telefon'),
                    'veranstaltungsart': parsed.get('veranstaltungsart'),
                    'veranstaltungsort': parsed.get('veranstaltungsort'),
                    'veranstaltungsbereich': parsed.get('veranstaltungsbereich'),
                    'personenzahl': parsed.get('personenzahl'),
                    'material': parsed.get('material'),
                    'sonstiges': parsed.get('sonstiges'),
                    'rahmenbedingungen': parsed.get('rahmenbedingungen'),
                }

                for field, value in fields_to_update.items():
                    if getattr(candidate, field) != value:
                        setattr(candidate, field, value)
                        changed = True
                
                # Update status if needed (optional logic could go here)
                
                if changed:
                    updated += 1
            else:
                # Create new candidate
                try:
                    candidate = EmailCandidate(
                        user_id=user_id,
                        kanboard_task_id=tid,
                        subject=task.get('title'),
                        raw_content=task.get('description'),
                        status='processed',
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


def get_calendar_events(user_id=None):
    """Get calendar events for dashboard (shared across all users)."""
    candidates = get_candidates('ALL')
    events = []
    
    for c in candidates:
        if c.get('datum'):
            try:
                # Convert German date to ISO
                dt = datetime.strptime(c['datum'], '%d.%m.%Y')
                iso_date = dt.strftime('%Y-%m-%d')
                
                # Determine color based on date and status
                event_date = dt.date()
                today = datetime.now().date()
                status = c.get('status', 'pending')
                
                color = '#3788d8' # Fallback
                
                if event_date < today:
                    color = '#6c757d' # Gray (Past)
                elif status in ('processed', 'done'):
                    color = '#10b981' # Green (processed/done)
                else:
                    color = '#f59e0b' # Amber/Yellow (pending)
                
                events.append({
                    'title': c.get('veranstaltungsname') or c.get('subject'),
                    'start': iso_date,
                    'url': f"/emails?highlight={c['id']}",
                    'backgroundColor': color,
                    'borderColor': color,
                    'extendedProps': {
                        'status': c.get('status', 'pending'),
                        'location': c.get('veranstaltungsort'),
                        'persons': c.get('personenzahl'),
                        'name': c.get('vorname_nachname'),
                        'tags': c.get('tags') if isinstance(c.get('tags'), list) else []
                    }
                })
            except:
                pass
    return events


def get_archived_candidates(user_id: int = None, page: int = 1, limit: int = 10, 
                          search_query: str = None, date_filter: str = None, 
                          tag_filter: str = None) -> Dict:
    """Get archived (past) candidates with pagination/filtering (shared across all users)."""
    offset = (page - 1) * limit
    
    try:
        with get_session() as s:
            # Base query: all candidates (shared)
            q = s.query(EmailCandidate)
            
            # SAFE DATE PARSING using string manipulation
            # We normalize everything to 'YYYY-MM-DD' string for comparison/sorting
            
            # Expression for DD.MM.YYYY -> YYYY-MM-DD
            # substr(d, 7, 4) || '-' || substr(d, 4, 2) || '-' || substr(d, 1, 2)
            de_to_iso = func.concat(
                func.substr(EmailCandidate.datum, 7, 4), '-',
                func.substr(EmailCandidate.datum, 4, 2), '-',
                func.substr(EmailCandidate.datum, 1, 2)
            )
            
            # CASE to select the normalized ISO string
            # Check for DD.MM.YYYY (length 10, dots at 3 and 6) 
            # or just regex. Regex is safest for format check.
            iso_date_expr = case(
                (EmailCandidate.datum.op('~')(r'^\d{2}\.\d{2}\.\d{4}$'), de_to_iso),
                (EmailCandidate.datum.op('~')(r'^\d{4}-\d{2}-\d{2}$'), EmailCandidate.datum),
                else_=None
            )
            
            # Filter for past dates OR returned/problem status
            current_date_str = func.to_char(func.current_date(), 'YYYY-MM-DD')
            q = q.filter(or_(
                iso_date_expr < current_date_str,
                EmailCandidate.status.in_(['returned'])
            ))
            
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
                except:
                    pass
                    
            if tag_filter:
                q = q.filter(EmailCandidate.tags.contains([tag_filter]))
                
            # Get total count
            total = q.count()
            
            # Order by normalized date string descending
            q = q.order_by(iso_date_expr.desc().nulls_last())
            q = q.offset(offset).limit(limit)
            
            results = []
            for row in q.all():
                d = row.to_dict()
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

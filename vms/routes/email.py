"""E-Mail- und Kandidaten-Routen.

Aus app.py herausgelöst (Block B der Graph-Analyse): app.py verteilte 63 Knoten
über 10 Communities und hatte damit die höchste Fragmentierung im Repo. Diese
14 Routen bilden davon die größte zusammenhängende Gruppe -- der Import- und
Datenfluss bleibt unverändert, nur die Zuordnung stimmt jetzt.
"""
from datetime import date, datetime

from flask import Blueprint, jsonify, render_template, request

import vms.clients.kanboard_client as kanboard_client
from vms.auth import login_required, current_user
from vms.domain.models import (
    ACTIVE_STATUSES, TERMINAL_STATUSES, CandidateStatus,
    format_de_date, parse_flexible_date, to_iso_date, to_local,
)

email_bp = Blueprint('email', __name__)


def _decorate_candidate_dates(c, normalize_datum=False):
    """Parse datum/end_date eines Kandidaten-Dicts und setze die Anzeigefelder.

    Ersetzt fünf handkopierte Blöcke, die dieselbe Logik mit je leicht anderen
    Feldern nachbauten. Sie akzeptierten nur DD.MM.YYYY und ISO; die kanonische
    models.parse_flexible_date versteht zusätzlich DD.MM.YY, Datum in Fließtext
    und die deutsche Bereichs-Kurzform "13.-15.11.26" -- derselbe gespeicherte
    Wert parste vorher je nach Endpunkt unterschiedlich.

    `normalize_datum` schreibt c['datum'] zusätzlich auf die deutsche Anzeige um
    (das taten nur zwei der fünf Blöcke).

    Gibt (parsed_date, parsed_end_date) zurück.
    """
    parsed_date = parse_flexible_date(c.get('datum'))
    parsed_end = parse_flexible_date(c.get('end_date'))
    if normalize_datum and parsed_date:
        c['datum'] = format_de_date(c['datum'])
    if c.get('end_date'):
        c['end_date_display'] = format_de_date(c['end_date'])
    return parsed_date, parsed_end


def massgebliches_enddatum(parsed_date, parsed_end):
    """Der Tag, an dem ein Verleih endet -- maßgeblich für "vorbei?".

    Gibt es ein Enddatum, zählt das. Gibt es keines, zählt das Startdatum.
    Bewusst nicht pauschal eines von beiden: get_returns wertete vorher
    "Start ODER Ende vergangen" aus und zeigte dadurch mehrtägige Verleihe
    schon in den Rückgaben an, während sie noch liefen.

    Ein Enddatum vor dem Startdatum ist Datenmüll und wird ignoriert, damit ein
    Zahlendreher einen Vorgang nicht vorzeitig verschwinden lässt.
    """
    if parsed_end is not None and (parsed_date is None or parsed_end >= parsed_date):
        return parsed_end
    return parsed_date



@email_bp.route('/emails')
@login_required
def emails():
    """Serve the email import page."""
    from vms.clients.email_client import get_candidates, get_last_sync
    from datetime import datetime, date
    import json
    
    # Pass user_id explicitly. status_filter='ALL' to get processed ones too.
    all_candidates = get_candidates(status_filter='ALL')
    last_sync = get_last_sync()
    today = date.today()
    
    # Parse tags and dates for each candidate
    for c in all_candidates:
        # Parse tags
        if not c.get('tags'):
            c['tags'] = []
        
        # Parse date for filtering and sorting - support multiple formats
        c['parsed_date'], _ = _decorate_candidate_dates(c, normalize_datum=True)

    
    # Split into open and processed candidates
    open_candidates = []
    processed_candidates = []
    
    for c in all_candidates:
        # User requested to hide past requests from ALL tables
        # Keep if date is None (unknown) or >= today
        if c['parsed_date'] and c['parsed_date'] < today:
            continue

        if c['status'] in ACTIVE_STATUSES:
            processed_candidates.append(c)
        else:
            open_candidates.append(c)



    
    # Sort by date ascending (earliest first)
    open_candidates.sort(key=lambda x: x['parsed_date'] or date.max)
    processed_candidates.sort(key=lambda x: x['parsed_date'] or date.max)
    
    # Calculate conflicts: count open requests per date
    date_counts = {}
    for c in open_candidates:
        if c.get('datum'):
            date_counts[c['datum']] = date_counts.get(c['datum'], 0) + 1
    
    # Mark candidates with conflicts
    for c in open_candidates:
        c['has_conflict'] = c.get('datum') and date_counts.get(c['datum'], 0) > 1
    
    # Tag-Belegung aller Verleihe (offen und bearbeitet) für die Ausgrau-Logik
    # im Editor. Zeitraum in ISO, damit das Frontend Überschneidungen mit dem
    # gerade bearbeiteten Zeitraum rechnen kann.
    tag_usage = []
    for c in all_candidates:
        start = c.get('parsed_date')
        if not start or not c.get('tags'):
            continue
        end = start
        if c.get('end_date'):
            for fmt, raw in (('%Y-%m-%d', str(c['end_date'])[:10]), ('%d.%m.%Y', str(c['end_date']))):
                try:
                    end = datetime.strptime(raw, fmt).date()
                    break
                except ValueError:
                    continue
        if end < start:
            end = start
        tag_usage.append({
            'id': c.get('id'),
            'start': start.isoformat(),
            'end': end.isoformat(),
            'tags': list(c['tags']),
        })

    # Load available materials for tag selection from DB
    try:
        from vms.domain.models import InventoryItem
        from vms.domain.database import get_session
        with get_session() as s:
            items = s.query(InventoryItem).filter(InventoryItem.type == 'equipment').all()
            # Template accesses materials.materials, so keep the nested structure.
            materials = {'materials': {item.name: item.description or item.name for item in items}}
    except Exception as e:
        app.logger.warning('Failed to load materials for tag selection: %s', e)
        materials = {'materials': {}}

    # Active VMS users for the "Verantwortlich" dropdown in the edit modal.
    from vms.auth import User as AuthUser
    vms_users = [u for u in AuthUser.get_all() if u.get('is_active')]

    return render_template('emails.html',
                           user=current_user,
                           open_candidates=open_candidates,
                           processed_candidates=processed_candidates,
                           last_sync=to_local(last_sync).strftime('%d.%m.%Y %H:%M') if last_sync else None,
                           materials=materials,
                           vms_users=vms_users,
                           tag_usage=tag_usage)


@email_bp.route('/api/emails/list-html', methods=['GET'])
@login_required
def email_list_html():
    """Return rendered HTML fragment of the email list tables."""
    from vms.clients.email_client import get_candidates
    from datetime import datetime, date
    import json
    
    all_candidates = get_candidates(status_filter='ALL')
    today = date.today()
    
    # Parse tags and dates for each candidate
    for c in all_candidates:
        if not c.get('tags'):
            c['tags'] = []
        
        c['parsed_date'], _ = _decorate_candidate_dates(c, normalize_datum=True)

    open_candidates = []
    processed_candidates = []
    
    for c in all_candidates:
        if c['parsed_date'] and c['parsed_date'] < today:
            continue
        if c['status'] in ACTIVE_STATUSES:
            processed_candidates.append(c)
        else:
            open_candidates.append(c)

    open_candidates.sort(key=lambda x: x['parsed_date'] or date.max)
    processed_candidates.sort(key=lambda x: x['parsed_date'] or date.max)
    
    # Calculate conflicts
    date_counts = {}
    for c in open_candidates:
        if c.get('datum'):
            date_counts[c['datum']] = date_counts.get(c['datum'], 0) + 1
    for c in open_candidates:
        c['has_conflict'] = c.get('datum') and date_counts.get(c['datum'], 0) > 1

    return render_template('_email_list.html',
                           open_candidates=open_candidates,
                           processed_candidates=processed_candidates)


@email_bp.route('/api/emails/sync', methods=['POST'])
@login_required
def sync_emails():
    """Sync emails from IMAP server and Kanboard."""
    try:
        from vms.clients.email_client import sync_emails as do_sync, sync_with_kanboard, get_last_sync
        new_count = do_sync(current_user.id)
        
        # Also sync with Kanboard
        kanboard_result = {'created': 0, 'updated': 0}
        try:
            kanboard_result = sync_with_kanboard(current_user.id)
        except Exception as e:
            print(f"Kanboard sync error: {e}")
        
        # Get updated last_sync
        last_sync_dt = get_last_sync()
        last_sync_str = last_sync_dt.isoformat() if last_sync_dt else None

        return jsonify({
            'new': new_count,
            'kanboard_created': kanboard_result.get('created', 0),
            'kanboard_updated': kanboard_result.get('updated', 0),
            'last_sync': last_sync_str
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@email_bp.route('/api/emails/candidates', methods=['GET'])
@login_required
def get_email_candidates():
    """Get all email candidates."""
    from vms.clients.email_client import get_candidates, extract_form_section
    import json
    
    
    candidates = get_candidates(status_filter='ALL')
    for c in candidates:
        if not c.get('tags'):
            c['tags'] = []
        
        # Filter raw_content to only show form section
        if c.get('raw_content'):
            c['raw_content'] = extract_form_section(c['raw_content'])
    
    return jsonify(candidates)


@email_bp.route('/api/emails/archive', methods=['GET'])
@login_required
def get_archived_emails():
    """Get paginated archived emails (past dates OR returned/problem status)."""
    from vms.clients.email_client import get_archived_candidates
    
    page = request.args.get('page', 1, type=int)
    limit = request.args.get('limit', 10, type=int)
    search = request.args.get('q')
    date_filter = request.args.get('date')
    tag_filter = request.args.get('tag')
    
    result = get_archived_candidates(
        page=page,
        limit=limit,
        search_query=search,
        date_filter=date_filter,
        tag_filter=tag_filter
    )
    return jsonify(result)


@email_bp.route('/api/emails/candidates/paged', methods=['GET'])
@login_required
def get_paged_candidates():
    """Get paginated candidates for infinite scroll."""
    from vms.clients.email_client import get_candidates
    from datetime import datetime, date
    
    status_param = request.args.get('status', 'pending')
    limit = request.args.get('limit', 5, type=int)
    offset = request.args.get('offset', 0, type=int)
    direction = request.args.get('direction', 'future')
    
    statuses = [s.strip() for s in status_param.split(',')]
    all_candidates = get_candidates(status_filter='ALL')
    today = date.today()
    
    filtered = []
    for c in all_candidates:
        if c.get('status') not in statuses:
            continue
        if not c.get('tags'):
            c['tags'] = []
        
        c['parsed_date'], _ = _decorate_candidate_dates(c)
        
        if direction == 'future':
            if c['parsed_date'] and c['parsed_date'] < today:
                continue
        
        # Conflict detection for open candidates
        if 'pending' in statuses:
            c['has_conflict'] = False
        
        filtered.append(c)
    
    filtered.sort(key=lambda x: x.get('parsed_date') or date.max)
    
    # Conflict detection
    if 'pending' in statuses:
        date_counts = {}
        for c in filtered:
            if c.get('datum'):
                date_counts[c['datum']] = date_counts.get(c['datum'], 0) + 1
        for c in filtered:
            c['has_conflict'] = c.get('datum') and date_counts.get(c['datum'], 0) > 1
    
    total = len(filtered)
    page_items = filtered[offset:offset + limit]
    
    # Clean up non-serializable fields
    for c in page_items:
        if 'parsed_date' in c:
            del c['parsed_date']
    
    return jsonify({
        'items': page_items,
        'total': total,
        'has_more': offset + limit < total
    })


@email_bp.route('/api/emails/returns', methods=['GET'])
@login_required
def get_returns():
    """Get candidates ready for return (past date + processed/done status)."""
    from vms.clients.email_client import get_candidates
    from datetime import datetime, date
    
    limit = request.args.get('limit', 5, type=int)
    offset = request.args.get('offset', 0, type=int)
    
    all_candidates = get_candidates(status_filter='ALL')
    today = date.today()
    
    returns = []
    for c in all_candidates:
        if c.get('status') not in ACTIVE_STATUSES:
            continue
        if not c.get('tags'):
            c['tags'] = []
        
        parsed_date, parsed_end_date = _decorate_candidate_dates(c)
        
        ende = massgebliches_enddatum(parsed_date, parsed_end_date)
        if ende is not None and ende < today:
            returns.append(c)
    
    # Sort oldest first
    returns.sort(key=lambda x: x.get('datum', ''))
    
    total = len(returns)
    page_items = returns[offset:offset + limit]

    # Enrich the visible items with their assigned storage locations (Verleih).
    from vms.domain.database import get_session
    from vms.domain.models import StorageLocation
    if page_items:
        ids = [c['id'] for c in page_items]
        with get_session() as s:
            locs = s.query(StorageLocation).filter(StorageLocation.candidate_id.in_(ids)).all()
            by_candidate = {}
            for loc in locs:
                by_candidate.setdefault(loc.candidate_id, []).append(loc.name)
        for c in page_items:
            c['storage_locations'] = by_candidate.get(c['id'], [])

    return jsonify({
        'items': page_items,
        'total': total,
        'has_more': offset + limit < total
    })


@email_bp.route('/api/emails/candidates/<int:candidate_id>/return', methods=['POST'])
@login_required
def return_candidate(candidate_id):
    """Process a return action on a candidate."""
    from vms.clients.email_client import get_candidate_by_id, update_candidate
    from datetime import datetime, timezone
    
    data = request.get_json(silent=True) or {}
    action = data.get('action')
    note = data.get('note', '')
    laufende_nummer = data.get('laufende_nummer')
    nummer_typ = data.get('nummer_typ')
    
    if action not in ('returned', 'invoice'):
        return jsonify({'error': 'Ungültige Aktion'}), 400
    
    candidate = get_candidate_by_id(candidate_id)
    if not candidate:
        return jsonify({'error': 'Kandidat nicht gefunden'}), 404

    # Ein bereits abgeschlossener Vorgang darf nicht ein zweites Mal zurückgegeben
    # werden -- das würde returned_at/status/return_note überschreiben und Lagerplätze
    # erneut freigeben. Nur die echten Endzustände (RETURNED, INVOICED) sperren;
    # INVOICE_PENDING bleibt bewusst offen, weil über dieselbe Route noch die laufende
    # Nummer nachgetragen wird (s. test_sequential_number.py). Wie beim schon
    # zurückgegebenen Verleih: als Konflikt ablehnen.
    if candidate.get('status') in TERMINAL_STATUSES:
        return jsonify({'error': 'Vorgang wurde bereits zurückgegeben oder fakturiert'}), 409

    now = datetime.now(timezone.utc)
    
    from vms.domain.database import get_session
    from vms.domain.models import (
        EmailCandidate, StorageLocation, INVOICE_TYPES, NummerBereitsVergeben,
        claim_sequential_number,
    )

    if nummer_typ is not None and nummer_typ not in INVOICE_TYPES:
        return jsonify({'error': 'Ungültiger Typ'}), 400

    # Die Fehlerbehandlung liegt bewusst *außerhalb* des with-Blocks: get_session
    # committet bei normalem Blockende, ein return von innen würde die schon
    # gesetzten Felder festschreiben, obwohl die Antwort ein Fehler ist. Als
    # Exception verlässt der Fall den Block und löst das Rollback aus.
    try:
        with get_session() as s:
            row = s.query(EmailCandidate).filter_by(id=candidate_id).first()
            if not row:
                return jsonify({'error': 'Kandidat nicht gefunden'}), 404

            row.return_note = note if note else None
            row.returned_at = now

            if action == 'returned':
                row.status = CandidateStatus.RETURNED.value
            else:
                row.status = CandidateStatus.INVOICE_PENDING.value

            # Free any storage locations assigned to this loan (both actions end it).
            s.query(StorageLocation).filter_by(candidate_id=candidate_id).update({'candidate_id': None})

            # Save laufende nummer if provided. Reservierung und Zählerpflege
            # laufen über dieselbe gesperrte Vergabe wie beim Rechnungsversand,
            # statt die Logik hier ein zweites Mal (und ungeprüft) nachzubauen.
            if laufende_nummer and nummer_typ:
                vergeben = claim_sequential_number(s, nummer_typ, laufende_nummer)
                row.laufende_nummer = str(vergeben)
                row.nummer_typ = nummer_typ
    except NummerBereitsVergeben as e:
        return jsonify({'error': str(e)}), 409
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    # Reflect the new status in Kanboard (returned → close, invoice → move).
    kanboard_client.reconcile_candidate_by_id(current_user.id, candidate_id)

    return jsonify({'success': True})


@email_bp.route('/api/emails/candidates/<int:candidate_id>', methods=['PUT'])
@login_required
def update_email_candidate(candidate_id):
    """Update candidate details and sync to Kanboard if linked."""
    from vms.clients.email_client import update_candidate, get_candidate_by_id
    import json
    
    data = request.get_json()
    
    # Check if linked to Kanboard -> Update Kanboard Task
    candidate = get_candidate_by_id(candidate_id)
    if candidate and candidate.get('kanboard_task_id'):
        try:
            task_id = candidate['kanboard_task_id']
            # Prepare Kanboard update
            due_date = data.get('start_date')
            
            kanboard_client.update_task(
                user_id=current_user.id,
                task_id=task_id,
                title=data.get('veranstaltungsname') or candidate.get('subject'),
                description=data.get('description'), # raw_content is description
                due_date=due_date,
                tags=data.get('tags')
            )
        except Exception as e:
            print(f"Failed to update Kanboard task {candidate.get('kanboard_task_id')}: {e}")

    form_data = {
        'tags': data.get('tags'),
        'datum': data.get('start_date'),
        'end_date': data.get('end_date'),
        'raw_content': data.get('description'),
        'vorname_nachname': data.get('vorname_nachname'),
        'veranstaltungsname': data.get('veranstaltungsname'),
        'veranstaltungsort': data.get('veranstaltungsort'),
        'email_address': data.get('email_address'),
        'personenzahl': data.get('personenzahl'),
        'anschrift': data.get('anschrift')
    }
    
    form_data = {k: v for k, v in form_data.items() if v is not None}

    # Applied after the None-filter so clearing the responsible user actually sticks.
    if 'responsible_user_id' in data:
        form_data['responsible_user_id'] = data['responsible_user_id'] or None

    if update_candidate(candidate_id, form_data):
        return jsonify({'success': True})
    return jsonify({'error': 'Kandidat nicht gefunden oder keine Änderungen'}), 404


@email_bp.route('/api/emails/candidates/create', methods=['POST'])
@login_required
def create_manual_candidate_route():
    """Create a loan request manually: create the Kanboard task, then store
    the candidate locally as 'processed' so it lands in 'Erledigte Anfragen'."""
    from vms.clients.email_client import create_manual_candidate

    data = request.get_json() or {}
    tags = data.get('tags', []) or []
    description = data.get('description') or ''
    veranstaltungsname = data.get('veranstaltungsname')
    vorname_nachname = data.get('vorname_nachname')

    if not (veranstaltungsname or vorname_nachname):
        return jsonify({'error': 'Bitte mindestens Name oder Veranstaltung angeben'}), 400

    due_date = data.get('start_date')

    try:
        # Create the Kanboard task first so we never leave an orphan candidate.
        result = kanboard_client.create_task(
            user_id=current_user.id,
            title=f"{veranstaltungsname or vorname_nachname or 'Manuelle Anfrage'}",
            description=description,
            due_date=due_date,
            tags=tags,
            column_name='Leihanfrage'
        )

        form_data = {
            'tags': tags,
            'datum': due_date,
            'end_date': data.get('end_date'),
            'raw_content': description,
            'vorname_nachname': vorname_nachname,
            'veranstaltungsname': veranstaltungsname,
            'veranstaltungsort': data.get('veranstaltungsort'),
            'email_address': data.get('email_address'),
            'personenzahl': data.get('personenzahl'),
            'anschrift': data.get('anschrift'),
            'kanboard_task_id': result.get('id'),
            'responsible_user_id': data.get('responsible_user_id') or None,
        }

        candidate_id = create_manual_candidate(form_data, current_user.id)

        return jsonify({'success': True, 'task_id': result.get('id'), 'candidate_id': candidate_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@email_bp.route('/api/emails/candidates/<int:candidate_id>/mark-done', methods=['PUT'])
@login_required
def mark_candidate_done_route(candidate_id):
    """Mark a candidate as done (contract created)."""
    from vms.clients.email_client import mark_candidate_done

    if mark_candidate_done(candidate_id):
        kanboard_client.reconcile_candidate_by_id(current_user.id, candidate_id)
        return jsonify({'success': True})
    return jsonify({'error': 'Kandidat nicht gefunden'}), 404


@email_bp.route('/api/emails/candidates/<int:candidate_id>/mark-processed', methods=['PUT'])
@login_required
def mark_candidate_processed_route(candidate_id):
    """Mark a candidate as processed (revert from done)."""
    from vms.clients.email_client import mark_candidate_processed

    if mark_candidate_processed(candidate_id):
        kanboard_client.reconcile_candidate_by_id(current_user.id, candidate_id)
        return jsonify({'success': True})
    return jsonify({'error': 'Kandidat nicht gefunden'}), 404


@email_bp.route('/api/emails/candidates/for-contract', methods=['GET'])
@login_required
def get_candidates_for_contract():
    """Get processed candidates that are ready for contract creation."""
    from vms.clients.email_client import get_candidates
    from datetime import datetime, date
    import json
    
    all_candidates = get_candidates(CandidateStatus.PROCESSED.value)
    result = []
    today = date.today()
    
    for c in all_candidates:
        # Only include processed candidates (not done, not pending)
        if c.get('status') != CandidateStatus.PROCESSED:
            continue
        
        # Exclude candidates with contracts already created
        if c.get('contract_created'):
            continue
            
        # Parse tags
        if not c.get('tags'):
            c['tags'] = []
        
        # Parse dates for ISO format and check past dates
        parsed_date = parse_flexible_date(c.get('datum'))
        parsed_end = parse_flexible_date(c.get('end_date'))
        if c.get('datum'):
            # to_iso_date gibt den Rohwert zurück, wenn er nicht parsbar ist --
            # gleiches Verhalten wie der frühere except-Zweig.
            c['datum_iso'] = to_iso_date(c['datum'])
        ende = massgebliches_enddatum(parsed_date, parsed_end)
        if ende is not None and ende < today:
            continue
        
        result.append(c)
    
    return jsonify(result)


@email_bp.route('/api/emails/candidates/<int:candidate_id>', methods=['DELETE'])
@login_required
def delete_email_candidate(candidate_id):
    """Delete an email candidate."""
    from vms.clients.email_client import delete_candidate
    
    if delete_candidate(candidate_id):
        return jsonify({'success': True})
    return jsonify({'error': 'Kandidat nicht gefunden'}), 404

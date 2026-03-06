"""
VMS - Flask Application
Web interface for filling ODT templates and exporting to PDF.
With secure user authentication.
"""

import base64
import json
import os
import secrets
import tempfile
from datetime import datetime

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, send_file
from flask_wtf.csrf import CSRFProtect
from flask_mail import Mail

from odt_processor import convert_to_pdf, process_odt_template
import kanboard_client
from auth import init_auth, login_required, current_user, limiter
from settings_routes import settings_bp


# Load environment variables
load_dotenv()

# Load secrets from KMS if available (production), otherwise use .env (development)
_kms_secrets = {}
try:
    from kms import is_kms_available, load_secrets as kms_load_secrets
    if is_kms_available():
        _kms_secrets = kms_load_secrets()
        print("✓ KMS: Secrets loaded from encrypted store")
    else:
        print("ℹ KMS: Not configured, using .env values")
except Exception as e:
    print(f"⚠ KMS: {e} — falling back to .env")

def _get_secret(key: str, default: str = None) -> str:
    """Get a secret from KMS or environment."""
    return _kms_secrets.get(key) or os.environ.get(key, default)

app = Flask(__name__, static_folder='static', template_folder='templates')

# Security configuration
app.config['SECRET_KEY'] = _get_secret('SECRET_KEY', secrets.token_hex(32))
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('FLASK_ENV') == 'production'
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['WTF_CSRF_ENABLED'] = True

# Email configuration
app.config['MAIL_SERVER'] = os.environ.get('MAIL_SERVER', 'localhost')
app.config['MAIL_PORT'] = int(os.environ.get('MAIL_PORT', 587))
app.config['MAIL_USE_TLS'] = os.environ.get('MAIL_USE_TLS', 'true').lower() == 'true'
app.config['MAIL_USE_SSL'] = os.environ.get('MAIL_USE_SSL', 'false').lower() == 'true'
app.config['MAIL_USERNAME'] = _get_secret('MAIL_USERNAME') or os.environ.get('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = _get_secret('MAIL_PASSWORD') or os.environ.get('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_DEFAULT_SENDER', app.config['MAIL_USERNAME'])

# Initialize extensions
csrf = CSRFProtect(app)
mail = Mail(app)
app.register_blueprint(settings_bp)

from inventory_routes import inventory_bp
app.register_blueprint(inventory_bp)

# Initialize authentication
init_auth(app)

# Paths
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_PATH = os.path.join(BASE_DIR, 'template.odt')
MATERIAL_PATH = os.path.join(BASE_DIR, 'material.json')


@app.route('/health')
@limiter.exempt
def health_check():
    """Health check endpoint for Docker, exempt from rate limiting."""
    return jsonify({'status': 'ok'}), 200


@app.route('/')
@login_required
def index():
    """Serve the dashboard."""
    from auth import User
    from database import get_session
    from models import EmailCandidate
    
    # Get statistics for dashboard
    now = datetime.now()
    current_year = now.year
    
    # Count Leihanfragen for current year
    with get_session() as s:
        leihanfragen_count = s.query(EmailCandidate).filter(
            EmailCandidate.datum.ilike(f'%{current_year}%'),
            EmailCandidate.user_id == current_user.id
        ).count()
    
    stats = {
        'users': User.count(),
        'leihanfragen': leihanfragen_count,
        'year': current_year
    }
    return render_template('dashboard.html', user=current_user, stats=stats)


@app.route('/leihvertrag')
@login_required
def leihvertrag():
    """Serve the contract form page."""
    return render_template('index.html', user=current_user)


@app.route('/api/materials', methods=['GET'])
@login_required
def get_materials():
    """Return the available materials from DB (Items) and Bundles as packages."""
    from database import get_session
    from models import InventoryItem, Bundle, BundleItem
    
    try:
        with get_session() as s:
            items = s.query(InventoryItem).order_by(InventoryItem.name).all()
            bundles = s.query(Bundle).order_by(Bundle.name).all()
            
            # Format: { "materials": { "Name": "Description" } } (Backwards compatibility)
            # New format: { "equipment": { "Name": "Description" }, "cases": { "Name": "Description" } }
            materials_dict = {}
            equipment_dict = {}
            cases_dict = {}
            
            for item in items:
                desc = item.description or item.name
                materials_dict[item.name] = desc
                if item.type == 'case':
                    cases_dict[item.name] = desc
                else:
                    equipment_dict[item.name] = desc
            
            # Format: { "packages": { "BundleName": [ { "count": 1, "text": "Description" } ] } }
            packages_dict = {}
            for bundle in bundles:
                package_items = []
                for b_item in bundle.items:
                    # b_item is a BundleItem association
                    item_def = s.query(InventoryItem).get(b_item.item_id)
                    if item_def:
                        package_items.append({
                            'count': b_item.count,
                            'text': item_def.name,
                            'type': item_def.type or 'equipment'
                        })
                packages_dict[bundle.name] = package_items
                
            return jsonify({
                "materials": materials_dict,
                "equipment": equipment_dict,
                "cases": cases_dict,
                "packages": packages_dict
            })
            
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/materials/add', methods=['POST'])
@login_required
def add_material():
    """Add a new material to DB (legacy support for frontend)."""
    from database import get_session
    from models import InventoryItem
    from sqlalchemy.exc import IntegrityError
    
    try:
        data = request.get_json()
        name = data.get('name', '').strip()
        text = data.get('text', '').strip()
        
        if not name or not text:
            return jsonify({'error': 'Name und Beschreibung erforderlich'}), 400
        
        with get_session() as s:
            try:
                item = InventoryItem(name=name, description=text, type='equipment')
                s.add(item)
                s.commit()
                return jsonify({'success': True, 'name': name, 'text': text})
            except IntegrityError:
                return jsonify({'error': 'Existiert bereits'}), 409
                
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/kanboard/tasks', methods=['GET'])
@login_required
def get_kanboard_tasks():
    """Get all tasks from the Leihanfragen column."""
    try:
        tasks = kanboard_client.get_leihanfragen_tasks()
        return jsonify(tasks)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/kanboard/task/<int:task_id>', methods=['GET'])
@login_required
def get_kanboard_task(task_id):
    """Get detailed task info including tags."""
    try:
        task = kanboard_client.get_task_details(task_id)
        return jsonify(task)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/generate', methods=['POST'])
@login_required
def generate_pdf():
    """Generate PDF from template with provided data."""
    try:
        data = request.get_json()
        
        # Extract form data
        # Current date in German format for #HEUTE#
        heute = datetime.now().strftime('%d.%m.%Y')
        
        # Resolve Material Text
        # The frontend sends a string of material text (already looked up?) or a list of tags?
        # Looking at emails.html, it constructs a string from selected tags:
        # "Material: " + tags.join(", ") 
        # Wait, the current implementation in emails.html might be sending the KEYS (names) or VALUES (descriptions).
        # Let's check emails.html next. But assuming 'material' in data is the FINAL text to put in ODT.
        # If the user selects a Bundle in the frontend, the frontend might just send "Bundle Name".
        # We need to intercept this.
        
        # HOWEVER, the standard way (looking at existing code) is that the frontend sends `material` 
        # which is directly put into `#MATERIAL#`.
        
        # If we want Bundles to expand, we should probably do it here OR in the frontend.
        # Doing it here is safer if we want to ensure up-to-date descriptions.
        # But `data.get('material')` usually comes from a textarea where the user CAN EDIT the text.
        # So if the user selected a Bundle, the Frontend should have already expanded it into the Textarea.
        
        # Correct approach: Update Frontend to expand Bundles into the Textarea when selected.
        # Then `generate_pdf` just takes the text as is (user might have edited it manually).
        
        replacements = {
            '#VORNAME NACHNAME#': data.get('vorname_nachname', ''),
            '#PRIVATANSCHRIFT#': data.get('privatanschrift', ''),
            '#RECHNUNGSANSCHRIFT#': data.get('rechnungsanschrift', ''),
            '#ABHOLDATUM#': data.get('abholdatum', ''),
            '#RÜCKGABEDATUM#': data.get('rueckgabedatum', ''),
            '#VERANSTALTUNGSNAME#': data.get('veranstaltungsname', ''),
            '#VERANSTALTUNGSDATUM#': data.get('veranstaltungsdatum', ''),
            '#VERANSTALTUNGSORT#': data.get('veranstaltungsort', ''),
            '#MATERIAL#': data.get('material', ''),
            '#HEUTE#': heute,
            '#VERLEIHER#': current_user.display_name,
        }
        
        # Handle signature (base64 encoded PNG)
        signature_data = data.get('signature')
        signature_path = None
        
        with tempfile.TemporaryDirectory() as temp_dir:
            # Save signature if provided
            if signature_data:
                # Remove data URL prefix if present
                if ',' in signature_data:
                    signature_data = signature_data.split(',')[1]
                
                signature_path = os.path.join(temp_dir, 'signature.png')
                with open(signature_path, 'wb') as f:
                    f.write(base64.b64decode(signature_data))
            
            # Process template
            output_odt = os.path.join(temp_dir, 'output.odt')
            process_odt_template(
                TEMPLATE_PATH,
                output_odt,
                replacements,
                signature_path
            )
            
            # Convert to PDF
            pdf_path = convert_to_pdf(output_odt, temp_dir)
            
            # Generate filename with date
            name = data.get('vorname_nachname', 'Unbekannt').replace(' ', '_')
            # Remove special characters for filename
            name = ''.join(c for c in name if c.isalnum() or c == '_')
            filename = f"Leihvertrag_{name}_{datetime.now().strftime('%Y%m%d')}.pdf"
            
            # Read PDF into memory before temp dir is cleaned up
            with open(pdf_path, 'rb') as f:
                pdf_data = f.read()
            
            # Create response with PDF data from memory
            from flask import Response
            from io import BytesIO
            
            return send_file(
                BytesIO(pdf_data),
                mimetype='application/pdf',
                as_attachment=True,
                download_name=filename
            )
            
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# =============================================================================
# Email Import Routes
# =============================================================================

@app.route('/emails')
@login_required
def emails():
    """Serve the email import page."""
    from email_client import get_candidates, get_last_sync
    from datetime import datetime, date
    import json
    
    # Pass user_id explicitly. status_filter='ALL' to get processed ones too.
    all_candidates = get_candidates(status_filter='ALL', user_id=current_user.id)
    last_sync = get_last_sync(current_user.id)
    today = date.today()
    
    # Parse tags and dates for each candidate
    for c in all_candidates:
        # Parse tags
        if not c.get('tags'):
            c['tags'] = []
        
        # Parse date for filtering and sorting - support multiple formats
        c['parsed_date'] = None
        if c.get('datum'):
            datum = c['datum']
            # Try DD.MM.YYYY format
            try:
                c['parsed_date'] = datetime.strptime(datum, '%d.%m.%Y').date()
            except:
                pass
            # Try YYYY-MM-DD format
            if c['parsed_date'] is None:
                try:
                    c['parsed_date'] = datetime.strptime(datum[:10], '%Y-%m-%d').date()
                    # Convert to German format for display
                    c['datum'] = c['parsed_date'].strftime('%d.%m.%Y')
                except:
                    pass
        
        # Parse end_date for display
        if c.get('end_date'):
            end_date_str = c['end_date']
            # Try YYYY-MM-DD (standard from input)
            try:
                dt = datetime.strptime(end_date_str, '%Y-%m-%d')
                c['end_date_display'] = dt.strftime('%d.%m.%Y')
            except:
                # Fallback or already formatted
                c['end_date_display'] = end_date_str

    
    # Split into open and processed candidates
    open_candidates = []
    processed_candidates = []
    
    for c in all_candidates:
        # User requested to hide past requests from ALL tables
        # Keep if date is None (unknown) or >= today
        if c['parsed_date'] and c['parsed_date'] < today:
            continue

        if c['status'] in ('processed', 'done'):
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
    
    # Calculate used tags per date (for smart tag filtering in editor)
    # Include tags from ALL candidates (both open and processed)
    tags_by_date = {}
    for c in all_candidates:
        datum = c.get('datum')
        # Only use valid string dates
        if datum and isinstance(datum, str) and c.get('tags'):
            if datum not in tags_by_date:
                tags_by_date[datum] = set()
            tags_by_date[datum].update(c['tags'])
    
    # Convert sets to lists for JSON serialization
    tags_by_date = {str(k): list(v) for k, v in tags_by_date.items()}
    
    # Load available materials for tag selection
    try:
        with open(MATERIAL_PATH, 'r', encoding='utf-8') as f:
            materials = json.load(f)
    except:
        materials = {}
    
    return render_template('emails.html', 
                           user=current_user, 
                           open_candidates=open_candidates,
                           processed_candidates=processed_candidates,
                           last_sync=last_sync.isoformat() if last_sync else None,
                           materials=materials,
                           tags_by_date=tags_by_date)


@app.route('/api/emails/list-html', methods=['GET'])
@login_required
def email_list_html():
    """Return rendered HTML fragment of the email list tables."""
    from email_client import get_candidates
    from datetime import datetime, date
    import json
    
    all_candidates = get_candidates(status_filter='ALL', user_id=current_user.id)
    today = date.today()
    
    # Parse tags and dates for each candidate
    for c in all_candidates:
        if not c.get('tags'):
            c['tags'] = []
        
        c['parsed_date'] = None
        if c.get('datum'):
            datum = c['datum']
            try:
                c['parsed_date'] = datetime.strptime(datum, '%d.%m.%Y').date()
            except:
                pass
            if c['parsed_date'] is None:
                try:
                    c['parsed_date'] = datetime.strptime(datum[:10], '%Y-%m-%d').date()
                    c['datum'] = c['parsed_date'].strftime('%d.%m.%Y')
                except:
                    pass
        
        if c.get('end_date'):
            try:
                dt = datetime.strptime(c['end_date'], '%Y-%m-%d')
                c['end_date_display'] = dt.strftime('%d.%m.%Y')
            except:
                c['end_date_display'] = c['end_date']

    open_candidates = []
    processed_candidates = []
    
    for c in all_candidates:
        if c['parsed_date'] and c['parsed_date'] < today:
            continue
        if c['status'] in ('processed', 'done'):
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


@app.route('/api/emails/sync', methods=['POST'])
@login_required
def sync_emails():
    """Sync emails from IMAP server and Kanboard."""
    try:
        from email_client import sync_emails as do_sync, sync_with_kanboard, get_last_sync
        new_count = do_sync(current_user.id)
        
        # Also sync with Kanboard
        kanboard_result = {'created': 0, 'updated': 0}
        try:
            kanboard_result = sync_with_kanboard(current_user.id)
        except Exception as e:
            print(f"Kanboard sync error: {e}")
        
        # Get updated last_sync
        last_sync_dt = get_last_sync(current_user.id)
        last_sync_str = last_sync_dt.isoformat() if last_sync_dt else None

        return jsonify({
            'new': new_count,
            'kanboard_created': kanboard_result.get('created', 0),
            'kanboard_updated': kanboard_result.get('updated', 0),
            'last_sync': last_sync_str
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/emails/candidates', methods=['GET'])
@login_required
def get_email_candidates():
    """Get all email candidates."""
    from email_client import get_candidates, extract_form_section
    import json
    
    
    candidates = get_candidates(status_filter='ALL', user_id=current_user.id)
    for c in candidates:
        if not c.get('tags'):
            c['tags'] = []
        
        # Filter raw_content to only show form section
        if c.get('raw_content'):
            c['raw_content'] = extract_form_section(c['raw_content'])
    
    return jsonify(candidates)


@app.route('/api/calendar/events', methods=['GET'])
@login_required
def get_calendar_events():
    """Get events for the dashboard calendar."""
    from email_client import get_calendar_events
    events = get_calendar_events(current_user.id)
    return jsonify(events)
@app.route('/api/emails/archive', methods=['GET'])
@login_required
def get_archived_emails():
    """Get paginated archived emails."""
    from email_client import get_archived_candidates
    
    page = request.args.get('page', 1, type=int)
    limit = request.args.get('limit', 10, type=int)
    search = request.args.get('q')
    date_filter = request.args.get('date')
    tag_filter = request.args.get('tag')
    
    result = get_archived_candidates(
        user_id=current_user.id,
        page=page,
        limit=limit,
        search_query=search,
        date_filter=date_filter,
        tag_filter=tag_filter
    )
    return jsonify(result)

@app.route('/api/emails/candidates/<int:candidate_id>', methods=['PUT'])
@login_required
def update_email_candidate(candidate_id):
    """Update candidate details and sync to Kanboard if linked."""
    from email_client import update_candidate, get_candidate_by_id
    import json
    
    data = request.get_json()
    
    # Check if linked to Kanboard -> Update Kanboard Task
    candidate = get_candidate_by_id(candidate_id, current_user.id)
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
        'tags': json.dumps(data.get('tags')) if data.get('tags') is not None else None,
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

    if update_candidate(candidate_id, form_data, current_user.id):
        return jsonify({'success': True})
    return jsonify({'error': 'Kandidat nicht gefunden oder keine Änderungen'}), 404



@app.route('/api/emails/candidates/<int:candidate_id>/create-task', methods=['POST'])
@login_required
def create_task_from_candidate(candidate_id):
    """Create Kanboard task from email candidate."""
    from email_client import get_candidate_by_id as get_candidate, mark_candidate_processed, extract_form_section, save_kanboard_task_id, update_candidate
    import json
    
    candidate = get_candidate(candidate_id, current_user.id)
    if not candidate:
        return jsonify({'error': 'Kandidat nicht gefunden'}), 404
    
    data = request.get_json() or {}
    tags = data.get('tags', [])
    if not tags and candidate.get('tags'):
        tags = candidate['tags'] # Already parsed or list? get_candidate parses it.
    
    vorname_nachname = data.get('vorname_nachname')
    veranstaltungsname_edit = data.get('veranstaltungsname')
    description = data.get('description')
    if not description:
        raw = candidate.get('raw_content', '')
        description = extract_form_section(raw) if raw else ''
    
    due_date = data.get('start_date') or candidate.get('datum')
    
    try:
        # Create Task
        result = kanboard_client.create_task(
            user_id=current_user.id,
            title=f"{veranstaltungsname_edit or candidate.get('veranstaltungsname', 'Unbekannt')}",
            description=description,
            due_date=due_date,
            tags=tags,
            column_name='Leihanfrage'
        )
        
        kanboard_task_id = result.get('id')
        if kanboard_task_id:
            save_kanboard_task_id(candidate_id, kanboard_task_id, current_user.id)
        
        # Update candidate
        form_data = {
            'tags': json.dumps(tags),
            'datum': due_date,
            'end_date': data.get('end_date'),
            'raw_content': description,
            'vorname_nachname': vorname_nachname,
            'veranstaltungsname': veranstaltungsname_edit,
            'veranstaltungsort': data.get('veranstaltungsort'),
            'email_address': data.get('email_address'),
            'personenzahl': data.get('personenzahl'),
            'anschrift': data.get('anschrift')
        }
        form_data = {k: v for k, v in form_data.items() if v is not None}
        
        update_candidate(candidate_id, form_data, current_user.id)
        mark_candidate_processed(candidate_id, current_user.id)
        
        return jsonify({'success': True, 'task_id': result['id']})
    except Exception as e:
        return jsonify({'error': str(e)}), 500



@app.route('/api/emails/candidates/<int:candidate_id>/mark-done', methods=['PUT'])
@login_required
def mark_candidate_done_route(candidate_id):
    """Mark a candidate as done (contract created)."""
    from email_client import mark_candidate_done
    
    if mark_candidate_done(candidate_id, current_user.id):
        return jsonify({'success': True})
    return jsonify({'error': 'Kandidat nicht gefunden'}), 404


@app.route('/api/emails/candidates/<int:candidate_id>/mark-processed', methods=['PUT'])
@login_required
def mark_candidate_processed_route(candidate_id):
    """Mark a candidate as processed (revert from done)."""
    from email_client import mark_candidate_processed
    
    if mark_candidate_processed(candidate_id, current_user.id):
        return jsonify({'success': True})
    return jsonify({'error': 'Kandidat nicht gefunden'}), 404


@app.route('/api/emails/candidates/for-contract', methods=['GET'])
@login_required
def get_candidates_for_contract():
    """Get processed candidates that are ready for contract creation."""
    from email_client import get_candidates
    from datetime import datetime, date
    import json
    
    all_candidates = get_candidates('processed', user_id=current_user.id)
    result = []
    today = date.today()
    
    for c in all_candidates:
        # Only include processed candidates (not done, not pending)
        if c.get('status') != 'processed':
            continue
        
        # Exclude candidates with contracts already created
        if c.get('contract_created'):
            continue
            
        # Parse tags
        if not c.get('tags'):
            c['tags'] = []
        
        # Parse dates for ISO format and check past dates
        parsed_date = None
        if c.get('datum'):
            try:
                parsed_date = datetime.strptime(c['datum'], '%d.%m.%Y').date()
                c['datum_iso'] = parsed_date.strftime('%Y-%m-%d')
            except:
                c['datum_iso'] = c.get('datum', '')
        
        # Skip if date is in the past
        if parsed_date and parsed_date < today:
            continue
        
        result.append(c)
    
    return jsonify(result)


@app.route('/api/emails/candidates/<int:candidate_id>', methods=['DELETE'])
@login_required
def delete_email_candidate(candidate_id):
    """Delete an email candidate."""
    from email_client import delete_candidate
    
    if delete_candidate(candidate_id, current_user.id):
        return jsonify({'success': True})
    return jsonify({'error': 'Kandidat nicht gefunden'}), 404


# Exempt API routes from CSRF for AJAX calls (forms still protected)
@app.after_request
def csrf_exempt_api(response):
    """Exempt API routes from CSRF."""
    return response


if __name__ == '__main__':
    print("=" * 50)
    print("VMS - Leihvertrag Generator")
    print("=" * 50)
    print(f"Template: {TEMPLATE_PATH}")
    print(f"Material: {MATERIAL_PATH}")
    print()
    print("Starte Server auf http://localhost:5000")
    print("Drücke Ctrl+C zum Beenden")
    print("=" * 50)
    app.run(debug=True, port=5000)

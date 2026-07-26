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
from werkzeug.middleware.proxy_fix import ProxyFix
from flask_wtf.csrf import CSRFProtect
from flask_mail import Mail

from vms.infra.odt_processor import convert_to_pdf, process_odt_template
from vms.infra.template_store import load_template
import vms.clients.kanboard_client as kanboard_client
from vms.auth import init_auth, login_required, current_user, limiter
from vms.routes.settings import settings_bp


# Load environment variables
load_dotenv()

# Load secrets from KMS if available (production), otherwise use .env (development)
_kms_secrets = {}
try:
    from vms.infra.kms import is_kms_available, load_secrets as kms_load_secrets
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

# templates/ and static/ live at the repo root, one level above this package.
_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
app = Flask(
    __name__,
    static_folder=os.path.join(_ROOT_DIR, 'static'),
    template_folder=os.path.join(_ROOT_DIR, 'templates'),
)

# Make German date formatting (DD.MM.YYYY) available in all templates.
from vms.domain.models import format_de_date as _format_de_date
from vms.domain.models import to_local
from vms.domain.models import CandidateStatus, ACTIVE_STATUSES
from vms.domain.models import parse_flexible_date, to_iso_date, format_de_date
app.add_template_filter(_format_de_date, name='de_date')

# Trust X-Forwarded-* headers from reverse proxy (Nginx)
# This is required so Flask correctly sees HTTPS and client IPs
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Security configuration
_is_production = os.environ.get('FLASK_ENV') == 'production'
app.config['SECRET_KEY'] = _get_secret('SECRET_KEY', secrets.token_hex(32))
app.config['SESSION_COOKIE_SECURE'] = _is_production
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['REMEMBER_COOKIE_SECURE'] = _is_production
app.config['REMEMBER_COOKIE_HTTPONLY'] = True
app.config['REMEMBER_COOKIE_SAMESITE'] = 'Lax'
app.config['WTF_CSRF_ENABLED'] = True
# Upper bound for uploads (ODT templates are the largest thing accepted).
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024

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

from vms.routes.inventory import inventory_bp
app.register_blueprint(inventory_bp)

from vms.routes.invoice import invoice_bp
app.register_blueprint(invoice_bp)

from vms.routes.verleih import verleih_bp
app.register_blueprint(verleih_bp)

from vms.routes.email import email_bp
app.register_blueprint(email_bp)

from vms.routes.templates import doc_templates_bp
app.register_blueprint(doc_templates_bp)

# Exempt API blueprints from CSRF (they use X-CSRFToken header in JS)
csrf.exempt(settings_bp)
csrf.exempt(inventory_bp)
csrf.exempt(invoice_bp)
csrf.exempt(verleih_bp)
csrf.exempt(email_bp)
csrf.exempt(doc_templates_bp)

# Initialize authentication
init_auth(app)

# Log session configuration for diagnostics
_sk = app.config['SECRET_KEY']
print(f"ℹ Session: SECRET_KEY fingerprint={_sk[:8]}…, Secure={app.config['SESSION_COOKIE_SECURE']}, Production={_is_production}")

# Paths (bundled ODT templates live in <repo>/assets)
BASE_DIR = _ROOT_DIR
TEMPLATE_PATH = os.path.join(_ROOT_DIR, 'assets', 'template.odt')



@app.route('/health')
@limiter.exempt
def health_check():
    """Health check endpoint for Docker, exempt from rate limiting."""
    return jsonify({'status': 'ok'}), 200


@app.route('/')
@login_required
def index():
    """Serve the dashboard."""
    from vms.auth import User
    from vms.domain.database import get_session
    from vms.domain.models import EmailCandidate
    
    # Get statistics for dashboard
    now = datetime.now()
    current_year = now.year
    
    # Count Leihanfragen for current year
    with get_session() as s:
        leihanfragen_count = s.query(EmailCandidate).filter(
            EmailCandidate.datum.ilike(f'%{current_year}%')
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


@app.route('/api/kanboard/tasks', methods=['GET'])
@login_required
def get_kanboard_tasks():
    """Get all tasks from the Leihanfragen column."""
    try:
        tasks = kanboard_client.get_leihanfragen_tasks(current_user.id)
        return jsonify(tasks)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/kanboard/task/<int:task_id>', methods=['GET'])
@login_required
def get_kanboard_task(task_id):
    """Get detailed task info including tags."""
    try:
        task = kanboard_client.get_task_details(current_user.id, task_id)
        return jsonify(task)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/generate', methods=['POST'])
@login_required
def generate_pdf():
    """Generate PDF from template with provided data."""
    try:
        from vms.domain.models import format_de_date, format_de_datetime
        data = request.get_json(silent=True)
        if data is None:
            return jsonify({'error': 'Ungültige oder fehlende Anfragedaten'}), 400

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
            # Both carry a pickup/return time from the form, which must survive
            # the date normalization.
            '#ABHOLDATUM#': format_de_datetime(data.get('abholdatum', '')),
            '#RÜCKGABEDATUM#': format_de_datetime(data.get('rueckgabedatum', '')),
            '#VERANSTALTUNGSNAME#': data.get('veranstaltungsname', ''),
            '#VERANSTALTUNGSDATUM#': format_de_date(data.get('veranstaltungsdatum', '')),
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
            
            # Process template (active version from the template store)
            template_path = load_template('leihvertrag', temp_dir)
            output_odt = os.path.join(temp_dir, 'output.odt')
            process_odt_template(
                template_path,
                output_odt,
                replacements,
                signature_path
            )
            
            # Convert to PDF
            pdf_path = convert_to_pdf(output_odt, temp_dir)
            
            # Generate filename with date
            name = (data.get('vorname_nachname') or 'Unbekannt').replace(' ', '_')
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

@app.route('/api/calendar/events', methods=['GET'])
@login_required
def get_calendar_events():
    """Get events for the dashboard calendar."""
    from vms.clients.email_client import get_calendar_events
    # FullCalendar sends the visible window as ?start=...&end=... (ISO, may
    # include a time/offset). Pass it through so we only return that range.
    range_start = request.args.get('start')
    range_end = request.args.get('end')
    events = get_calendar_events(range_start, range_end)
    return jsonify(events)
@app.route('/api/emails/candidates/<int:candidate_id>/create-task', methods=['POST'])
@login_required
def create_task_from_candidate(candidate_id):
    """Create Kanboard task from email candidate."""
    from vms.clients.email_client import get_candidate_by_id as get_candidate, mark_candidate_processed, extract_form_section, save_kanboard_task_id, update_candidate
    import json
    
    candidate = get_candidate(candidate_id)
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

    # Hier wird aus einer Anfrage ein Verleih ('processed'), und ein Verleih
    # braucht ein Datum. Vor dem Kanboard-Aufruf geprüft, damit bei Ablehnung
    # kein verwaister Task entsteht.
    from vms.domain.vorgang import DatumErforderlich, require_datum
    from vms.domain.models import CandidateStatus
    try:
        require_datum(CandidateStatus.PROCESSED, due_date)
    except DatumErforderlich as e:
        return jsonify({'error': str(e)}), 400

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
            save_kanboard_task_id(candidate_id, kanboard_task_id)
        
        # Update candidate
        form_data = {
            'tags': tags,
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
        if 'responsible_user_id' in data:
            form_data['responsible_user_id'] = data['responsible_user_id'] or None

        update_candidate(candidate_id, form_data)
        mark_candidate_processed(candidate_id)
        
        return jsonify({'success': True, 'task_id': result['id']})
    except Exception as e:
        return jsonify({'error': str(e)}), 500



@app.route('/api/kanboard/reconcile', methods=['POST'])
@login_required
def reconcile_kanboard_route():
    """Manually trigger the Kanboard column reconciliation (also runs daily)."""
    moved = reconcile_all_rentals()
    return jsonify({'success': True, 'reconciled': moved})


# ─── Scheduler: move "done" rentals into the "Verliehen" column once their
#     rental period starts. Time-based, so it cannot hang off a user action. ───

_ADVISORY_LOCK_KEY = 815734  # arbitrary, shared across workers
_scheduler_started = False


def reconcile_all_rentals():
    """Reconcile the Kanboard column for every contract-created (done) candidate.

    Guarded by a Postgres advisory lock so that with multiple Gunicorn workers
    only one actually performs the work per run. Returns the number of
    candidates processed (0 if the lock was held by another worker)."""
    from vms.domain.database import get_engine
    from vms.domain.models import EmailCandidate
    from sqlalchemy import text

    engine = get_engine()
    with engine.connect() as conn:
        got_lock = conn.execute(
            text('SELECT pg_try_advisory_lock(:k)'), {'k': _ADVISORY_LOCK_KEY}
        ).scalar()
        if not got_lock:
            return 0
        try:
            from vms.domain.database import get_session
            processed = 0
            with get_session() as s:
                rows = s.query(EmailCandidate).filter(
                    EmailCandidate.status == CandidateStatus.DONE.value,
                    EmailCandidate.kanboard_task_id.isnot(None)
                ).all()
                candidates = [{
                    'kanboard_task_id': r.kanboard_task_id,
                    'status': r.status,
                    'datum': r.datum,
                    'user_id': r.user_id,
                } for r in rows]
            for c in candidates:
                try:
                    kanboard_client.reconcile_candidate(c['user_id'], c)
                    processed += 1
                except Exception:
                    pass
            return processed
        finally:
            conn.execute(text('SELECT pg_advisory_unlock(:k)'), {'k': _ADVISORY_LOCK_KEY})


def init_scheduler():
    """Start the daily reconciliation job (idempotent per process)."""
    global _scheduler_started
    if _scheduler_started:
        return
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        scheduler = BackgroundScheduler(timezone='Europe/Berlin')
        scheduler.add_job(
            reconcile_all_rentals,
            trigger=CronTrigger(hour=0, minute=5),
            id='reconcile_rentals',
            replace_existing=True,
            max_instances=1,
        )
        scheduler.start()
        _scheduler_started = True
        print("✓ Scheduler: daily Kanboard reconcile at 00:05 Europe/Berlin")
    except Exception as e:
        print(f"⚠ Scheduler konnte nicht gestartet werden: {e}")


# Exempt all /api/ routes from CSRF (they are called via AJAX, not form submissions)
# This iterates over all registered routes after they've been defined above.
for _rule in app.url_map.iter_rules():
    if _rule.rule.startswith('/api/'):
        _view = app.view_functions.get(_rule.endpoint)
        if _view:
            csrf.exempt(_view)




if __name__ == '__main__':
    print("=" * 50)
    print("VMS - Leihvertrag Generator")
    print("=" * 50)
    print(f"Template: {TEMPLATE_PATH}")
    print()
    print("Starte Server auf http://localhost:5000")
    print("Drücke Ctrl+C zum Beenden")
    print("=" * 50)
    init_scheduler()
    app.run(debug=True, port=5000)

from flask import Blueprint, render_template, jsonify, request
from flask_login import login_required, current_user
from models import EmailCandidate, InventoryItem, SequentialNumber, format_de_date
from database import get_session
from datetime import datetime
import os
import re
import tempfile

invoice_bp = Blueprint('invoice', __name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INVOICE_TEMPLATES = {
    'rechnung': os.path.join(BASE_DIR, 'template_rechnung.odt'),
    'umbuchung': os.path.join(BASE_DIR, 'template_umbuchung.odt'),
}

def parse_german_date(date_str):
    if not date_str:
        return None
    try:
        # Check if it's already YYYY-MM-DD
        if re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
            return datetime.strptime(date_str, '%Y-%m-%d')
        # Check for DD.MM.YYYY
        if re.match(r'^\d{2}\.\d{2}\.\d{4}$', date_str):
            return datetime.strptime(date_str, '%d.%m.%Y')
        # Check for DD.MM.YY (and assume 2000+)
        if re.match(r'^\d{2}\.\d{2}\.\d{2}$', date_str):
            return datetime.strptime(date_str, '%d.%m.%y')
        return None
    except ValueError:
        return None

@invoice_bp.route('/invoices')
@login_required
def invoices_page():
    return render_template('invoices.html', user=current_user)

@invoice_bp.route('/api/invoices/candidates', methods=['GET'])
@login_required
def api_get_invoice_candidates():
    with get_session() as s:
        # Only pending invoices
        query = s.query(EmailCandidate).filter_by(status='invoice_pending')
        candidates = query.all()
    
        # Sort by date (oldest first) using parsed dates
        def get_sort_key(c):
            parsed_date = parse_german_date(c.datum)
            return parsed_date if parsed_date else datetime.max
            
        sorted_candidates = sorted(candidates, key=get_sort_key)
        
        result = []
        for c in sorted_candidates:
            result.append({
                'id': c.id,
                'vorname_nachname': c.vorname_nachname,
                'veranstaltungsname': c.veranstaltungsname,
                'datum': format_de_date(c.datum),
                'end_date': format_de_date(c.end_date),
                'tags': c.tags,
                'email_address': c.email_address,
                'return_note': c.return_note
            })
            
    return jsonify(result)

@invoice_bp.route('/api/invoices/consumables', methods=['GET'])
@login_required
def api_get_invoice_consumables():
    with get_session() as s:
        consumables = s.query(InventoryItem).filter_by(type='consumable').all()
        
        result = []
        for item in consumables:
            result.append({
                'id': item.id,
                'name': item.name,
                'description': item.description,
                'price': float(item.price) if item.price is not None else 0.0,
                'unit': item.unit
            })
    return jsonify(result)


def _safe_filename_part(value):
    """Reduce a string to filename-safe alphanumerics/underscores."""
    value = (value or 'Unbekannt').replace(' ', '_')
    return ''.join(c for c in value if c.isalnum() or c == '_') or 'Unbekannt'


@invoice_bp.route('/api/invoices/send', methods=['POST'])
@login_required
def api_send_invoice():
    """Generate a Rechnung/Umbuchung PDF from its ODT template and email it to
    the recipient as an attachment, then persist status and the sequential
    number."""
    from odt_processor import process_odt_template, convert_to_pdf, format_money_de
    from email_client import get_candidate_by_id
    from auth import send_email_with_attachment

    data = request.get_json() or {}

    candidate_id = data.get('candidate_id')
    nummer_typ = data.get('nummer_typ')
    laufende_nummer = data.get('laufende_nummer')
    email = (data.get('email') or '').strip()
    adresse = data.get('adresse') or ''
    mail_text = data.get('mail_text') or ''
    items = data.get('items') or []

    # --- Validation -----------------------------------------------------------
    if nummer_typ not in INVOICE_TEMPLATES:
        return jsonify({'error': 'Ungültiger Typ'}), 400
    if not laufende_nummer:
        return jsonify({'error': 'Laufende Nummer fehlt'}), 400
    if not email:
        return jsonify({'error': 'Empfänger-E-Mail fehlt'}), 400
    if not isinstance(items, list) or len(items) == 0:
        return jsonify({'error': 'Mindestens ein Posten erforderlich'}), 400

    candidate = get_candidate_by_id(candidate_id, current_user.id)
    if not candidate:
        return jsonify({'error': 'Kandidat nicht gefunden'}), 404

    # Recompute the total server-side (do not trust the client).
    total = 0.0
    for item in items:
        try:
            total += float(item.get('price', 0)) * int(item.get('quantity', 0))
        except (TypeError, ValueError):
            pass

    template_path = INVOICE_TEMPLATES[nummer_typ]
    if not os.path.exists(template_path):
        return jsonify({'error': 'Vorlage nicht gefunden'}), 500

    now = datetime.now()
    replacements = {
        '#NUMMER#': str(laufende_nummer),
        '#JAHR#': str(now.year),
        '#HEUTE#': now.strftime('%d.%m.%Y'),
        '#GESAMTPREIS#': format_money_de(total),
        '#VERLEIHER#': current_user.display_name,
        '#VORNAME NACHNAME#': candidate.get('vorname_nachname') or '',
        '#ADRESSE#': adresse,
        '#VERANSTALTUNG#': candidate.get('veranstaltungsname') or '',
    }

    label = 'Rechnung' if nummer_typ == 'rechnung' else 'Umbuchung'

    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_odt = os.path.join(temp_dir, 'output.odt')
            process_odt_template(template_path, output_odt, replacements, row_items=items)
            pdf_path = convert_to_pdf(output_odt, temp_dir)
            with open(pdf_path, 'rb') as f:
                pdf_bytes = f.read()

        filename = f"{label}_{laufende_nummer}_{_safe_filename_part(candidate.get('vorname_nachname'))}.pdf"
        subject = f"{label} Nr. {laufende_nummer}"

        send_email_with_attachment(email, subject, mail_text, pdf_bytes, filename)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'Versand fehlgeschlagen: {e}'}), 500

    # --- Persist only after the mail was sent successfully --------------------
    with get_session() as s:
        row = s.query(EmailCandidate).filter_by(id=candidate_id).first()
        if row:
            row.laufende_nummer = str(laufende_nummer)
            row.nummer_typ = nummer_typ
            row.status = 'done'
            row.contract_created = True

            # Idempotently advance the sequential counter.
            try:
                num_val = int(laufende_nummer)
            except (ValueError, TypeError):
                num_val = 0
            seq = s.query(SequentialNumber).filter_by(number_type=nummer_typ).first()
            if seq:
                if num_val > seq.last_number:
                    seq.last_number = num_val
            else:
                s.add(SequentialNumber(number_type=nummer_typ, last_number=num_val))

    # Reflect the closed status in Kanboard (best effort).
    try:
        from app import _reconcile_kanboard
        _reconcile_kanboard(candidate_id, current_user.id)
    except Exception:
        pass

    return jsonify({'success': True})

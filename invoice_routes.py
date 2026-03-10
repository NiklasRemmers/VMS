from flask import Blueprint, render_template, jsonify, request
from flask_login import login_required, current_user
from models import EmailCandidate, InventoryItem
from database import get_session
from datetime import datetime
import re

invoice_bp = Blueprint('invoice', __name__)

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
            'datum': c.datum,
            'end_date': c.end_date,
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

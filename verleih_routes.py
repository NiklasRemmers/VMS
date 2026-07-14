import secrets

from flask import Blueprint, request, jsonify, render_template, url_for
from database import get_session
from models import StorageLocation, CodeShareLink, format_de_date
from auth import login_required, current_user
from sqlalchemy.exc import IntegrityError

verleih_bp = Blueprint('verleih', __name__)


def _get_or_create_share_token(candidate_id):
    """Return the (reused) public share token for a loan, creating one if needed."""
    with get_session() as s:
        link = s.query(CodeShareLink).filter_by(candidate_id=candidate_id).first()
        if link:
            return link.token
        token = secrets.token_urlsafe(32)
        s.add(CodeShareLink(token=token, candidate_id=candidate_id))
        return token


@verleih_bp.route('/verleih')
@login_required
def verleih_page():
    return render_template('verleih.html', user=current_user)


@verleih_bp.route('/api/verleih/locations', methods=['GET'])
@login_required
def get_locations():
    with get_session() as s:
        locations = s.query(StorageLocation).order_by(StorageLocation.name).all()
        return jsonify([loc.to_dict() for loc in locations])


@verleih_bp.route('/api/verleih/locations', methods=['POST'])
@login_required
def create_location():
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    code = data.get('code')

    if not name:
        return jsonify({'error': 'Name erforderlich'}), 400

    with get_session() as s:
        try:
            loc = StorageLocation(name=name, code=code)
            s.add(loc)
            s.commit()
            s.refresh(loc)
            return jsonify(loc.to_dict()), 201
        except IntegrityError:
            s.rollback()
            return jsonify({'error': 'Ein Lagerort mit diesem Namen existiert bereits'}), 409
        except Exception as e:
            s.rollback()
            return jsonify({'error': str(e)}), 500


@verleih_bp.route('/api/verleih/locations/<int:location_id>', methods=['PUT'])
@login_required
def update_location(location_id):
    data = request.get_json() or {}

    with get_session() as s:
        loc = s.query(StorageLocation).get(location_id)
        if not loc:
            return jsonify({'error': 'Lagerort nicht gefunden'}), 404

        if 'name' in data:
            loc.name = (data['name'] or '').strip()
        if 'code' in data:
            loc.code = data['code']

        try:
            s.commit()
            s.refresh(loc)
            return jsonify(loc.to_dict())
        except IntegrityError:
            s.rollback()
            return jsonify({'error': 'Name bereits vergeben'}), 409


@verleih_bp.route('/api/verleih/locations/<int:location_id>', methods=['DELETE'])
@login_required
def delete_location(location_id):
    with get_session() as s:
        loc = s.query(StorageLocation).get(location_id)
        if not loc:
            return jsonify({'error': 'Lagerort nicht gefunden'}), 404

        s.delete(loc)
        s.commit()
        return jsonify({'success': True})


@verleih_bp.route('/api/verleih/locations/<int:location_id>/assign', methods=['POST'])
@login_required
def assign_location(location_id):
    from email_client import get_candidate_by_id

    data = request.get_json() or {}
    candidate_id = data.get('candidate_id')
    if not candidate_id:
        return jsonify({'error': 'candidate_id erforderlich'}), 400

    candidate = get_candidate_by_id(candidate_id, current_user.id)
    if not candidate:
        return jsonify({'error': 'Verleih nicht gefunden'}), 404

    with get_session() as s:
        loc = s.query(StorageLocation).get(location_id)
        if not loc:
            return jsonify({'error': 'Lagerort nicht gefunden'}), 404
        loc.candidate_id = candidate_id
        s.commit()
        s.refresh(loc)
        return jsonify(loc.to_dict())


@verleih_bp.route('/api/verleih/locations/<int:location_id>/unassign', methods=['POST'])
@login_required
def unassign_location(location_id):
    with get_session() as s:
        loc = s.query(StorageLocation).get(location_id)
        if not loc:
            return jsonify({'error': 'Lagerort nicht gefunden'}), 404
        loc.candidate_id = None
        s.commit()
        s.refresh(loc)
        return jsonify(loc.to_dict())


@verleih_bp.route('/api/verleih/send-codes', methods=['POST'])
@login_required
def send_codes():
    """Email a public link (no codes in the mail) to reveal a loan's codes."""
    from email_client import get_candidate_by_id
    from auth import send_plain_email

    data = request.get_json() or {}
    candidate_id = data.get('candidate_id')
    email = (data.get('email') or '').strip()

    if not candidate_id:
        return jsonify({'error': 'candidate_id erforderlich'}), 400
    if not email:
        return jsonify({'error': 'E-Mail-Adresse erforderlich'}), 400

    candidate = get_candidate_by_id(candidate_id, current_user.id)
    if not candidate:
        return jsonify({'error': 'Verleih nicht gefunden'}), 404

    # Must have at least one assigned storage location to share.
    with get_session() as s:
        count = s.query(StorageLocation).filter_by(candidate_id=candidate_id).count()
    if count == 0:
        return jsonify({'error': 'Diesem Verleih ist kein Lagerort zugeordnet'}), 400

    token = _get_or_create_share_token(candidate_id)
    url = url_for('public_codes', token=token, _external=True)

    datum = candidate.get('datum') or ''
    event = candidate.get('veranstaltungsname') or 'deinen Verleih'
    body = f'''Hallo,

für {event} kannst du deine Abhol-Codes über den folgenden Link abrufen:

{url}

Die Codes werden erst am Tag des Verleihs ({datum}) sichtbar.

Viele Grüße'''

    try:
        send_plain_email(email, 'Deine Codes für den Verleih', body)
    except Exception as e:
        return jsonify({'error': f'E-Mail konnte nicht gesendet werden: {e}', 'url': url}), 500

    return jsonify({'success': True, 'url': url})


@verleih_bp.route('/api/verleih/loans', methods=['GET'])
@login_required
def get_assignable_loans():
    """Loans that can be assigned to a storage location (active/prepared)."""
    from email_client import get_candidates

    candidates = get_candidates(status_filter='ALL')
    loans = [{
        'id': c['id'],
        'vorname_nachname': c.get('vorname_nachname'),
        'veranstaltungsname': c.get('veranstaltungsname'),
        'datum': format_de_date(c.get('datum')),
    } for c in candidates if c.get('status') in ('processed', 'done')]
    return jsonify(loans)

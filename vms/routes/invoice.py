from flask import Blueprint, render_template, jsonify, request, current_app, Response
from flask_login import login_required, current_user
from vms.domain.models import (
    EmailCandidate, InventoryItem, format_de_date, to_local,
    INVOICE_TYPES, NummerBereitsVergeben, CandidateStatus,
    claim_sequential_number, release_sequential_number, parse_flexible_date,
)
from vms.domain.database import get_session
from vms.domain.rechnungsabbruch import (
    RechnungsabbruchUnzulaessig, plan_rechnungsabbruch,
)
from vms.infra.template_store import load_template
from datetime import datetime, timezone, date
import os
import tempfile

invoice_bp = Blueprint('invoice', __name__)

# The template files themselves now live in the template store (versioned in the
# DB); the whitelist of accepted invoice types lives in models.INVOICE_TYPES,
# damit der Rückgabe-Endpunkt in app.py dieselbe Liste prüft.

def format_anschrift(raw):
    """Normalize a stored address into one line per address part.

    Handles literal "\\n" sequences from imported mails and single-line,
    comma-separated addresses. Expected result: Straße / PLZ Ort / Land.
    """
    if not raw:
        return ''
    lines = [l.strip() for l in raw.replace('\\n', '\n').split('\n')]
    lines = [l for l in lines if l]
    if len(lines) == 1 and ',' in lines[0]:
        lines = [p.strip() for p in lines[0].split(',') if p.strip()]
    return '\n'.join(lines)

@invoice_bp.route('/invoices')
@login_required
def invoices_page():
    return render_template('invoices.html', user=current_user)

@invoice_bp.route('/api/invoices/candidates', methods=['GET'])
@login_required
def api_get_invoice_candidates():
    with get_session() as s:
        # Only pending invoices
        query = s.query(EmailCandidate).filter_by(status=CandidateStatus.INVOICE_PENDING.value)
        candidates = query.all()
    
        # Sort by date (oldest first) using parsed dates
        def get_sort_key(c):
            # parse_flexible_date liefert ein `date`, deshalb date.max als
            # Sammelbecken für Unparsbares (sortiert ans Ende).
            return parse_flexible_date(c.datum) or date.max
            
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
                'anschrift': format_anschrift(c.anschrift),
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


@invoice_bp.route('/api/invoices/download', methods=['POST'])
@login_required
def api_download_invoice():
    """Generate a Rechnung/Umbuchung PDF from its ODT template and return it as a
    download. Es geht bewusst KEINE E-Mail mehr raus (Versand erfolgt außerhalb
    von VMS). Der Vorgang wird dabei wie bisher abgeschlossen: laufende Nummer
    verbindlich vergeben, Status 'invoiced', Kanboard-Task geschlossen."""
    from vms.infra.odt_processor import process_odt_template, convert_to_pdf, format_money_de
    from vms.clients.email_client import get_candidate_by_id

    data = request.get_json() or {}

    candidate_id = data.get('candidate_id')
    nummer_typ = data.get('nummer_typ')
    # Optional: nur gesetzt, wenn der Nutzer die vorgeschlagene Nummer im
    # Formular überschrieben hat. Sonst vergibt der Server sie selbst.
    gewuenschte_nummer = data.get('laufende_nummer')
    adresse = data.get('adresse') or ''
    kostenstelle = (data.get('kostenstelle') or '').strip()
    items = data.get('items') or []

    # --- Validation -----------------------------------------------------------
    if nummer_typ not in INVOICE_TYPES:
        return jsonify({'error': 'Ungültiger Typ'}), 400
    if not isinstance(items, list) or len(items) == 0:
        return jsonify({'error': 'Mindestens ein Posten erforderlich'}), 400
    if nummer_typ == 'umbuchung' and not kostenstelle:
        return jsonify({'error': 'Kostenstelle fehlt'}), 400

    candidate = get_candidate_by_id(candidate_id)
    if not candidate:
        return jsonify({'error': 'Kandidat nicht gefunden'}), 404

    # Recompute the total server-side (do not trust the client). Ein Posten mit
    # unlesbarem Preis wurde früher stillschweigend übersprungen — die Rechnung
    # ging dann zu niedrig raus, obwohl die Position im PDF stand.
    total = 0.0
    for item in items:
        try:
            total += float(item.get('price', 0)) * int(item.get('quantity', 0))
        except (TypeError, ValueError):
            return jsonify({
                'error': f"Ungültiger Preis oder Menge bei Posten "
                         f"'{item.get('name') or 'ohne Namen'}'"
            }), 400

    # --- Nummer verbindlich reservieren --------------------------------------
    # Bewusst eine eigene, kurze Transaktion: die Sperre auf der Zählerzeile darf
    # nicht über die PDF-Erzeugung gehalten werden.
    try:
        with get_session() as s:
            laufende_nummer = claim_sequential_number(s, nummer_typ, gewuenschte_nummer)
    except NummerBereitsVergeben as e:
        return jsonify({'error': str(e)}), 409
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    now = to_local(datetime.now(timezone.utc))
    replacements = {
        '#NUMMER#': str(laufende_nummer),
        '#JAHR#': str(now.year),
        '#HEUTE#': now.strftime('%d.%m.%Y'),
        '#GESAMTPREIS#': format_money_de(total),
        '#VERLEIHER#': current_user.display_name,
        '#VERANSTALTUNG#': candidate.get('veranstaltungsname') or '',
    }
    if nummer_typ == 'umbuchung':
        # Die Umbuchung ist intern adressiert (kein Empfänger-Name/-Adresse),
        # nennt dafür aber die zu belastende Kostenstelle.
        replacements['#KOSTENSTELLE#'] = kostenstelle
    else:
        replacements['#VORNAME NACHNAME#'] = candidate.get('vorname_nachname') or ''
        replacements['#ADRESSE#'] = adresse

    label = 'Rechnung' if nummer_typ == 'rechnung' else 'Umbuchung'

    # --- PDF erzeugen ---------------------------------------------------------
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            # Active version from the template store
            template_path = load_template(nummer_typ, temp_dir)
            output_odt = os.path.join(temp_dir, 'output.odt')
            process_odt_template(template_path, output_odt, replacements, row_items=items)
            pdf_path = convert_to_pdf(output_odt, temp_dir)
            with open(pdf_path, 'rb') as f:
                pdf_bytes = f.read()
    except Exception:
        # Interne Details (Template-/Pfad-/Krypto-Fehler) nur ins Log, nicht in
        # die Antwort. Das Dokument wurde nie ausgeliefert: Nummer wieder
        # freigeben, damit der Nummernkreis keine Lücke bekommt.
        current_app.logger.exception(
            'Erstellung von %s Nr. %s für Kandidat %s fehlgeschlagen',
            label, laufende_nummer, candidate_id)
        with get_session() as s:
            release_sequential_number(s, nummer_typ, laufende_nummer)
        return jsonify({'error': 'Dokument konnte nicht erstellt werden'}), 500

    veranstaltung = (candidate.get('veranstaltungsname') or '').strip()
    if veranstaltung:
        filename = f"{label}_{_safe_filename_part(veranstaltung)}.pdf"
    else:
        filename = f"{label}_{laufende_nummer}_{_safe_filename_part(candidate.get('vorname_nachname'))}.pdf"

    # --- Vorgang abschließen (erst nach erfolgreicher PDF-Erzeugung) ----------
    with get_session() as s:
        row = s.query(EmailCandidate).filter_by(id=candidate_id).first()
        if row is None:
            # Der Vorgang existiert nicht mehr. Es ging nichts raus, also die
            # Nummer freigeben und mit Fehler antworten (kein PDF ausliefern).
            current_app.logger.error(
                'Kandidat %s ist zwischen PDF-Erzeugung und Speichern '
                'verschwunden; %s Nr. %s nicht persistiert',
                candidate_id, label, laufende_nummer)
            release_sequential_number(s, nummer_typ, laufende_nummer)
            return jsonify({
                'error': f'{label} konnte nicht gespeichert werden: '
                         f'Vorgang nicht mehr vorhanden'
            }), 500

        row.laufende_nummer = str(laufende_nummer)
        row.nummer_typ = nummer_typ
        # 'invoiced' beendet den Vorgang endgültig: taucht nicht mehr in den
        # Rückgaben auf und landet (wie 'returned') im Archiv.
        # contract_created wird hier bewusst NICHT gesetzt: der Rechnungsvorgang
        # erzeugt keinen Leihvertrag. Zu diesem Zeitpunkt sollte ohnehin schon
        # einer existieren -- fehlt er, ist das ein Ablauffehler und soll auffallen
        # statt nachträglich als "Vertrag vorhanden" gebucht zu werden.
        if not row.contract_created:
            current_app.logger.warning(
                'Vorgang %s wird als %s fakturiert, obwohl kein Leihvertrag '
                'erzeugt wurde', candidate_id, label)
        row.status = CandidateStatus.INVOICED.value

    # Reflect the closed status in Kanboard (best effort).
    import vms.clients.kanboard_client as kanboard_client
    kanboard_client.reconcile_candidate_by_id(current_user.id, candidate_id)

    # Das PDF als Download zurückgeben. Die tatsächlich vergebene Nummer steht im
    # Header — sie kann von der im Formular vorgeschlagenen abweichen, wenn
    # zwischenzeitlich jemand anders eine Nummer gezogen hat.
    return Response(
        pdf_bytes,
        mimetype='application/pdf',
        headers={
            'Content-Disposition': f'attachment; filename="{filename}"',
            'X-Laufende-Nummer': str(laufende_nummer),
        },
    )


@invoice_bp.route('/api/invoices/candidates/<int:candidate_id>/cancel', methods=['POST'])
@login_required
def cancel_invoice(candidate_id):
    """Nimm die Rechnungsabsicht zurück: der Vorgang gilt als bloß eingezählt.

    Die Regeln liegen in domain/rechnungsabbruch.py; hier wird nur übersetzt.
    Wie beim Rückgabe-Endpunkt liegt die Fehlerbehandlung *außerhalb* des
    with-Blocks: get_session committet bei normalem Blockende, ein return von
    innen würde eine abgelehnte Änderung festschreiben.
    """
    import vms.clients.kanboard_client as kanboard_client

    notiz = (request.get_json(silent=True) or {}).get('note')

    try:
        with get_session() as s:
            row = s.query(EmailCandidate).filter_by(id=candidate_id).first()
            if row is None:
                return jsonify({'error': 'Kandidat nicht gefunden'}), 404

            plan = plan_rechnungsabbruch(
                status=row.status,
                nummer_typ=row.nummer_typ,
                laufende_nummer=row.laufende_nummer,
                bestehende_notiz=row.return_note,
                notiz=notiz,
            )

            row.status = plan.status
            row.return_note = plan.return_note
            row.nummer_typ = plan.nummer_typ
            row.laufende_nummer = plan.laufende_nummer
            if plan.freizugebende_nummer is not None:
                release_sequential_number(s, *plan.freizugebende_nummer)
    except RechnungsabbruchUnzulaessig as e:
        return jsonify({'error': str(e)}), 409

    # 'returned' ist terminal -- der verknüpfte Task wird geschlossen.
    kanboard_client.reconcile_candidate_by_id(current_user.id, candidate_id)

    return jsonify({'success': True})


# Aus app.py hierher gezogen: der Vorschlag gehört zum Rechnungsvorgang,
# der die Nummer über claim_sequential_number auch verbindlich vergibt.
@invoice_bp.route('/api/sequential-number/<string:number_type>', methods=['GET'])
@login_required
def get_next_sequential_number(number_type):
    """Schlage die nächste laufende Nummer vor (unverbindlich!).

    Reserviert nichts: die verbindliche Vergabe passiert erst beim Absenden über
    models.claim_sequential_number unter Sperre. Der Wert hier ist nur ein
    Formular-Vorschlag und kann veraltet sein, wenn zwischenzeitlich jemand
    anders sendet.
    """
    from vms.domain.database import get_session
    from vms.domain.models import SequentialNumber, INVOICE_TYPES

    if number_type not in INVOICE_TYPES:
        return jsonify({'error': 'Ungültiger Typ'}), 400

    with get_session() as s:
        row = s.query(SequentialNumber).filter_by(number_type=number_type).first()
        if row:
            next_number = row.last_number + 1
        else:
            next_number = 1
    
    return jsonify({'next_number': next_number, 'type': number_type})

from flask import Blueprint, render_template, jsonify, request, send_file
from auth import login_required, current_user
from io import BytesIO

import template_store
from template_store import TEMPLATE_LABELS, REQUIRED_PLACEHOLDERS

doc_templates_bp = Blueprint('doc_templates', __name__)


@doc_templates_bp.route('/templates')
@login_required
def templates_page():
    return render_template('document_templates.html', user=current_user)


@doc_templates_bp.route('/api/templates', methods=['GET'])
@login_required
def api_list_templates():
    """All template types with their active version and full history."""
    result = []
    for template_type, label in TEMPLATE_LABELS.items():
        versions = template_store.list_versions(template_type)
        result.append({
            'template_type': template_type,
            'label': label,
            'active': next((v for v in versions if v['is_active']), None),
            'versions': versions,
            'required_placeholders': REQUIRED_PLACEHOLDERS[template_type],
        })
    return jsonify(result)


@doc_templates_bp.route('/api/templates/upload', methods=['POST'])
@login_required
def api_upload_template():
    template_type = (request.form.get('template_type') or '').strip()
    note = (request.form.get('note') or '').strip()

    if template_type not in TEMPLATE_LABELS:
        return jsonify({'error': 'Ungültiger Template-Typ'}), 400

    file = request.files.get('template')
    if not file or not file.filename:
        return jsonify({'error': 'Keine Datei ausgewählt'}), 400
    if not file.filename.lower().endswith('.odt'):
        return jsonify({'error': 'Nur .odt-Dateien sind erlaubt'}), 400

    content = file.read()
    if not content:
        return jsonify({'error': 'Die Datei ist leer'}), 400

    ok, errors, warnings = template_store.validate_template(content, template_type)
    if not ok:
        # 422: the upload was understood but the template is not usable.
        return jsonify({'error': 'Vorlage abgelehnt', 'errors': errors,
                        'warnings': warnings}), 422

    stored = template_store.store_new_version(
        template_type,
        file.filename,
        content,
        user_id=current_user.id,
        note=note,
    )
    return jsonify({'template': stored, 'warnings': warnings})


@doc_templates_bp.route('/api/templates/<int:template_id>/activate', methods=['POST'])
@login_required
def api_activate_template(template_id):
    row = template_store.activate(template_id)
    if not row:
        return jsonify({'error': 'Version nicht gefunden'}), 404
    return jsonify({'template': row})


@doc_templates_bp.route('/api/templates/<int:template_id>/download', methods=['GET'])
@login_required
def api_download_template(template_id):
    filename, content = template_store.get_content(template_id)
    if content is None:
        return jsonify({'error': 'Version nicht gefunden'}), 404
    return send_file(
        BytesIO(content),
        mimetype='application/vnd.oasis.opendocument.text',
        as_attachment=True,
        download_name=filename,
    )


@doc_templates_bp.route('/api/templates/<int:template_id>', methods=['DELETE'])
@login_required
def api_delete_template(template_id):
    ok, error = template_store.delete_version(template_id)
    if not ok:
        return jsonify({'error': error}), 400
    return jsonify({'success': True})

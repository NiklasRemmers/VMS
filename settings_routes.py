"""
Settings routes for VMS.
Handles user profile, email, and Kanboard configuration.
Uses SQLAlchemy with PostgreSQL.
"""
from flask import Blueprint, request, jsonify, render_template, redirect, url_for, flash, current_app
from flask_login import login_required, current_user
import os
import bcrypt
from datetime import datetime, timezone
from werkzeug.utils import secure_filename
from security import encrypt_value
from kms import encrypt_binary, decrypt_binary
from forms import ProfileForm, PasswordForm
from database import get_session
import base64
import io
from models import User as UserModel, UserSettings

settings_bp = Blueprint('settings', __name__)


# --- API Routes ---

@settings_bp.route('/api/settings', methods=['GET'])
@login_required
def get_settings():
    with get_session() as s:
        row = s.query(UserSettings).filter_by(user_id=current_user.id).first()

        if not row:
            return jsonify({
                'email_provider': 'unconfigured',
                'kanboard_configured': False
            })

        return jsonify(row.to_dict())


@settings_bp.route('/api/settings/email/manual', methods=['POST'])
@login_required
def update_email_manual():
    data = request.get_json()

    email_address = data.get('email_address')
    imap_server = data.get('imap_server')
    imap_port = data.get('imap_port')
    imap_user = email_address
    imap_password = data.get('imap_password')

    smtp_server = data.get('smtp_server')
    smtp_port = data.get('smtp_port')
    smtp_user = email_address
    smtp_password = data.get('smtp_password')

    if not all([email_address, imap_server, imap_port, imap_user, smtp_server, smtp_port, smtp_user]):
        return jsonify({'error': 'Missing required fields'}), 400

    encrypted_imap = encrypt_value(imap_password) if imap_password else None
    encrypted_smtp = encrypt_value(smtp_password) if smtp_password else None

    with get_session() as s:
        row = s.query(UserSettings).filter_by(user_id=current_user.id).first()

        if row:
            row.email_provider = 'custom'
            row.email_address = email_address
            row.auth_type = 'password'
            row.imap_server = imap_server
            row.imap_port = imap_port
            row.imap_user = imap_user
            row.smtp_server = smtp_server
            row.smtp_port = smtp_port
            row.smtp_user = smtp_user
            row.updated_at = datetime.now(timezone.utc)

            if encrypted_imap:
                row.encrypted_imap_password = encrypted_imap
            if encrypted_smtp:
                row.encrypted_smtp_password = encrypted_smtp
        else:
            if not imap_password or not smtp_password:
                return jsonify({'error': 'Password required for new configuration'}), 400

            row = UserSettings(
                user_id=current_user.id,
                email_provider='custom',
                email_address=email_address,
                auth_type='password',
                imap_server=imap_server,
                imap_port=imap_port,
                imap_user=imap_user,
                encrypted_imap_password=encrypted_imap,
                smtp_server=smtp_server,
                smtp_port=smtp_port,
                smtp_user=smtp_user,
                encrypted_smtp_password=encrypted_smtp,
            )
            s.add(row)

    return jsonify({'success': True})


@settings_bp.route('/api/settings/kanboard', methods=['POST'])
@login_required
def update_kanboard():
    data = request.get_json()

    url = data.get('kanboard_url')
    user = data.get('kanboard_user')
    token = data.get('kanboard_token')
    project_id = data.get('kanboard_project_id')
    try:
        project_id = int(project_id) if project_id else 25
    except:
        project_id = 25

    if not all([url, user]):
        return jsonify({'error': 'URL and User are required'}), 400

    encrypted_token = encrypt_value(token) if token else None

    with get_session() as s:
        row = s.query(UserSettings).filter_by(user_id=current_user.id).first()

        if row:
            row.kanboard_url = url
            row.kanboard_user = user
            row.kanboard_project_id = project_id
            row.updated_at = datetime.now(timezone.utc)
            if encrypted_token:
                row.encrypted_kanboard_token = encrypted_token
        else:
            if not token:
                return jsonify({'error': 'Token required for new configuration'}), 400
            row = UserSettings(
                user_id=current_user.id,
                kanboard_url=url,
                kanboard_user=user,
                kanboard_project_id=project_id,
                encrypted_kanboard_token=encrypted_token,
            )
            s.add(row)

    return jsonify({'success': True})


# --- Page Routes ---

@settings_bp.route('/settings', methods=['GET', 'POST'])
@login_required
def settings_page():
    profile_form = ProfileForm(obj=current_user)
    password_form = PasswordForm()

    # Handle Profile Update
    if 'update_profile' in request.form and profile_form.validate_on_submit():
        with get_session() as s:
            if profile_form.email.data != current_user.email:
                exists = s.query(UserModel).filter(
                    UserModel.email == profile_form.email.data,
                    UserModel.id != current_user.id
                ).first()
                if exists:
                    flash('E-Mail bereits verwendet', 'error')
                else:
                    user = s.query(UserModel).filter_by(id=current_user.id).first()
                    user.display_name = profile_form.display_name.data
                    user.email = profile_form.email.data
                    flash('Profil gespeichert', 'success')
                    return redirect(url_for('settings.settings_page'))
            else:
                user = s.query(UserModel).filter_by(id=current_user.id).first()
                user.display_name = profile_form.display_name.data
                flash('Profil gespeichert', 'success')
                return redirect(url_for('settings.settings_page'))

    # Handle Password Update
    if 'change_password' in request.form and password_form.validate_on_submit():
        with get_session() as s:
            user = s.query(UserModel).filter_by(id=current_user.id).first()
            if not bcrypt.checkpw(password_form.current_password.data.encode('utf-8'), user.password_hash.encode('utf-8')):
                flash('Aktuelles Passwort falsch', 'error')
            else:
                new_hash = bcrypt.hashpw(password_form.new_password.data.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
                user.password_hash = new_hash
                flash('Passwort geändert', 'success')
                return redirect(url_for('settings.settings_page'))

    # Prepare data for template
    email_settings = {}
    kanboard_settings = {}

    # Defaults from .env
    default_imap_server = os.environ.get('IMAP_SERVER', '')
    default_imap_port = os.environ.get('IMAP_PORT', 993)
    default_smtp_server = os.environ.get('MAIL_SERVER', '')
    default_smtp_port = os.environ.get('MAIL_PORT', 587)
    default_kanboard_url = os.environ.get('KANBOARD_URL', '')
    default_kanboard_project_id = os.environ.get('KANBOARD_PROJECT_ID', 25)

    with get_session() as s:
        row = s.query(UserSettings).filter_by(user_id=current_user.id).first()

        if row:
            email_settings = {
                'provider': row.email_provider or 'unconfigured',
                'address': row.email_address,
                'auth_type': row.auth_type,
                'has_password': bool(row.encrypted_imap_password),
                'imap_server': row.imap_server or default_imap_server,
                'smtp_server': row.smtp_server or default_smtp_server,
                'smtp_port': row.smtp_port or default_smtp_port,
                'imap_port': row.imap_port or default_imap_port,
            }
            kanboard_settings = {
                'url': row.kanboard_url or default_kanboard_url,
                'user': row.kanboard_user,
                'has_token': bool(row.encrypted_kanboard_token),
                'project_id': row.kanboard_project_id or default_kanboard_project_id,
            }
        else:
            email_settings = {
                'provider': 'unconfigured',
                'address': '',
                'imap_server': default_imap_server,
                'imap_port': default_imap_port,
                'smtp_server': default_smtp_server,
                'smtp_port': default_smtp_port,
            }
            kanboard_settings = {
                'url': default_kanboard_url,
                'project_id': default_kanboard_project_id,
                'user': '',
                'has_token': False,
            }

    has_signature = False
    try:
        with get_session() as s:
            settings = s.query(UserSettings).filter_by(user_id=current_user.id).first()
            if settings and settings.encrypted_signature:
                has_signature = True
    except Exception as e:
        print(f"Signature check error: {e}")

    return render_template('settings.html',
                          profile_form=profile_form,
                          password_form=password_form,
                          email_settings=email_settings,
                          kanboard_settings=kanboard_settings,
                          has_signature=has_signature)


@settings_bp.route('/settings/signature', methods=['POST'])
@login_required
def upload_signature():
    if 'signature' not in request.files:
        flash('Keine Datei ausgewählt', 'error')
        return redirect(url_for('settings.settings_page'))

    file = request.files['signature']
    if file.filename == '':
        flash('Keine Datei ausgewählt', 'error')
        return redirect(url_for('settings.settings_page'))

    if file and file.filename.endswith('.png'):
        try:
            # Read and encrypt file content
            file_content = file.read()
            encrypted_content = encrypt_binary(file_content)

            with get_session() as s:
                settings = s.query(UserSettings).filter_by(user_id=current_user.id).first()
                if not settings:
                    settings = UserSettings(user_id=current_user.id)
                    s.add(settings)
                
                settings.encrypted_signature = encrypted_content
            
            flash('Unterschrift hochgeladen (verschlüsselt gespeichert)', 'success')
        except Exception as e:
            flash(f'Fehler beim Speichern: {e}', 'error')
    else:
        flash('Nur PNG-Dateien erlaubt', 'error')

    return redirect(url_for('settings.settings_page'))


@settings_bp.route('/api/signature', methods=['DELETE'])
@login_required
def delete_signature():
    with get_session() as s:
        settings = s.query(UserSettings).filter_by(user_id=current_user.id).first()
        if settings and settings.encrypted_signature:
            settings.encrypted_signature = None
            return jsonify({'success': True})
    return jsonify({'error': 'No signature found'}), 404


@settings_bp.route('/api/signature/preview', methods=['GET'])
@login_required
def preview_signature():
    from flask import send_file, Response
    
    with get_session() as s:
        settings = s.query(UserSettings).filter_by(user_id=current_user.id).first()
        if settings and settings.encrypted_signature:
            try:
                decrypted_data = decrypt_binary(settings.encrypted_signature)
                return send_file(
                    io.BytesIO(decrypted_data),
                    mimetype='image/png'
                )
            except Exception as e:
                return jsonify({'error': str(e)}), 500
            
    return jsonify({'error': 'No signature found'}), 404


@settings_bp.route('/api/signature', methods=['GET'])
@login_required
def get_signature_api():
    """Get signature as base64 for frontend use."""
    with get_session() as s:
        settings = s.query(UserSettings).filter_by(user_id=current_user.id).first()
        
        if settings and settings.encrypted_signature:
            try:
                decrypted_data = decrypt_binary(settings.encrypted_signature)
                b64_data = base64.b64encode(decrypted_data).decode('utf-8')
                return jsonify({'signature': f"data:image/png;base64,{b64_data}"})
            except Exception as e:
                return jsonify({'error': str(e)}), 500

    return jsonify({'signature': None})

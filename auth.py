"""
Authentication Module for VMS
Secure user authentication with Flask-Login, bcrypt, and CSRF protection
Extended with user management and invitation system
Uses SQLAlchemy with PostgreSQL.
"""

import os
import base64
import secrets
from functools import wraps
from datetime import datetime, timedelta, timezone

import bcrypt
from flask import Blueprint, render_template, request, redirect, url_for, flash, g, current_app, jsonify
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, validators
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from database import get_session, init_db as db_init
from models import User as UserModel

# Blueprint for auth routes
auth_bp = Blueprint('auth', __name__)


class User(UserMixin):
    """User model for Flask-Login."""

    def __init__(self, id, username, display_name=None, email=None, is_active=True):
        self.id = id
        self.username = username
        self.display_name = display_name or username
        self.email = email
        self._is_active = is_active

    @property
    def is_active(self):
        return self._is_active

    @staticmethod
    def _from_model(row):
        """Create a User from a SQLAlchemy model instance."""
        if not row:
            return None
        return User(row.id, row.username, row.display_name, row.email, bool(row.is_active))

    @staticmethod
    def get_by_id(user_id):
        """Get user by ID."""
        with get_session() as s:
            row = s.query(UserModel).filter_by(id=user_id).first()
            return User._from_model(row)

    @staticmethod
    def get_by_username(username):
        """Get user by username. Returns (User, password_hash)."""
        with get_session() as s:
            row = s.query(UserModel).filter_by(username=username).first()
            if row:
                return User._from_model(row), row.password_hash
            return None, None

    @staticmethod
    def get_by_email(email):
        """Get user by email."""
        with get_session() as s:
            row = s.query(UserModel).filter_by(email=email).first()
            return User._from_model(row)

    @staticmethod
    def get_by_invitation_token(token):
        """Get user by invitation token."""
        with get_session() as s:
            row = s.query(UserModel).filter(
                UserModel.invitation_token == token,
                UserModel.invitation_expires > datetime.now(timezone.utc)
            ).first()
            return User._from_model(row)

    @staticmethod
    def get_all(search=None):
        """Get all users, optionally filtered by search."""
        with get_session() as s:
            q = s.query(UserModel)
            if search:
                term = f'%{search}%'
                q = q.filter(
                    (UserModel.username.ilike(term)) |
                    (UserModel.display_name.ilike(term)) |
                    (UserModel.email.ilike(term))
                )
            rows = q.order_by(UserModel.created_at.desc()).all()
            return [r.to_dict() for r in rows]

    @staticmethod
    def create(username, password, display_name=None, email=None, is_active=True):
        """Create a new user with hashed password."""
        password_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        try:
            with get_session() as s:
                user = UserModel(
                    username=username,
                    password_hash=password_hash,
                    display_name=display_name,
                    email=email,
                    is_active=is_active,
                )
                s.add(user)
                s.flush()
                return User(user.id, username, display_name, email, is_active)
        except Exception:
            return None

    @staticmethod
    def create_invitation(email):
        """Create an invitation for a new user."""
        token = secrets.token_urlsafe(32)
        expires = datetime.now(timezone.utc) + timedelta(days=7)
        try:
            with get_session() as s:
                user = UserModel(
                    email=email,
                    password_hash='',
                    invitation_token=token,
                    invitation_expires=expires,
                    is_active=False,
                )
                s.add(user)
            return token
        except Exception:
            return None

    def complete_invitation(self, username, password, display_name):
        """Complete invitation by setting username and password."""
        password_hash = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        try:
            with get_session() as s:
                row = s.query(UserModel).filter_by(id=self.id).first()
                if row:
                    row.username = username
                    row.password_hash = password_hash
                    row.display_name = display_name
                    row.is_active = True
                    row.invitation_token = None
                    row.invitation_expires = None
            self.username = username
            self.display_name = display_name
            self._is_active = True
            return True
        except Exception:
            return False

    def update_profile(self, display_name=None, email=None):
        """Update user profile."""
        with get_session() as s:
            row = s.query(UserModel).filter_by(id=self.id).first()
            if row:
                row.display_name = display_name or self.display_name
                row.email = email or self.email
        self.display_name = display_name or self.display_name
        self.email = email or self.email

    def update_password(self, new_password):
        """Update user password."""
        password_hash = bcrypt.hashpw(new_password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
        with get_session() as s:
            row = s.query(UserModel).filter_by(id=self.id).first()
            if row:
                row.password_hash = password_hash

    @staticmethod
    def delete(user_id):
        """Delete a user by ID."""
        with get_session() as s:
            row = s.query(UserModel).filter_by(id=user_id).first()
            if row:
                s.delete(row)

    @staticmethod
    def verify_password(stored_hash, password):
        """Verify password against stored hash."""
        if not stored_hash:
            return False
        return bcrypt.checkpw(password.encode('utf-8'), stored_hash.encode('utf-8'))

    @staticmethod
    def count():
        """Count total active users."""
        with get_session() as s:
            return s.query(UserModel).filter_by(is_active=True).count()


# Flask-WTF Forms
class LoginForm(FlaskForm):
    """Login form with CSRF protection."""
    username = StringField('Benutzername', [validators.DataRequired()])
    password = PasswordField('Passwort', [validators.DataRequired()])


class RegisterForm(FlaskForm):
    """Registration form."""
    username = StringField('Benutzername', [
        validators.DataRequired(),
        validators.Length(min=3, max=50)
    ])
    password = PasswordField('Passwort', [
        validators.DataRequired(),
        validators.Length(min=8, message='Passwort muss mindestens 8 Zeichen haben')
    ])
    confirm_password = PasswordField('Passwort bestätigen', [
        validators.DataRequired(),
        validators.EqualTo('password', message='Passwörter müssen übereinstimmen')
    ])
    display_name = StringField('Anzeigename', [validators.Optional()])
    email = StringField('E-Mail', [validators.Optional(), validators.Email()])


class ProfileForm(FlaskForm):
    """Profile update form."""
    display_name = StringField('Anzeigename', [validators.Optional()])
    email = StringField('E-Mail', [validators.Optional(), validators.Email()])


class PasswordForm(FlaskForm):
    """Password change form."""
    current_password = PasswordField('Aktuelles Passwort', [validators.DataRequired()])
    new_password = PasswordField('Neues Passwort', [
        validators.DataRequired(),
        validators.Length(min=8, message='Passwort muss mindestens 8 Zeichen haben')
    ])
    confirm_password = PasswordField('Neues Passwort bestätigen', [
        validators.DataRequired(),
        validators.EqualTo('new_password', message='Passwörter müssen übereinstimmen')
    ])


class InviteForm(FlaskForm):
    """Invitation form."""
    email = StringField('E-Mail-Adresse', [
        validators.DataRequired(),
        validators.Email(message='Bitte eine gültige E-Mail-Adresse eingeben')
    ])


class AcceptInviteForm(FlaskForm):
    """Accept invitation form."""
    username = StringField('Benutzername', [
        validators.DataRequired(),
        validators.Length(min=3, max=50)
    ])
    display_name = StringField('Anzeigename', [validators.DataRequired()])
    password = PasswordField('Passwort', [
        validators.DataRequired(),
        validators.Length(min=8, message='Passwort muss mindestens 8 Zeichen haben')
    ])
    confirm_password = PasswordField('Passwort bestätigen', [
        validators.DataRequired(),
        validators.EqualTo('password', message='Passwörter müssen übereinstimmen')
    ])


# Initialize Flask-Login
login_manager = LoginManager()
login_manager.login_view = 'auth.login'
login_manager.login_message = 'Bitte melde dich an, um auf diese Seite zuzugreifen.'
login_manager.login_message_category = 'info'


@login_manager.user_loader
def load_user(user_id):
    """Load user for Flask-Login."""
    return User.get_by_id(int(user_id))


# Initialize rate limiter
def _get_real_ip():
    """Get real client IP behind Nginx reverse proxy."""
    forwarded = request.headers.get('X-Forwarded-For')
    if forwarded:
        return forwarded.split(',')[0].strip()
    return request.remote_addr or '127.0.0.1'

limiter = Limiter(
    key_func=_get_real_ip,
    default_limits=["1000 per day", "300 per hour"]
)


def init_auth(app):
    """Initialize authentication for Flask app."""
    # Initialize database tables
    db_init()

    # Configure Flask-Login
    login_manager.init_app(app)

    # Handle unauthorized access: return 401 JSON for API, redirect for pages
    @login_manager.unauthorized_handler
    def unauthorized():
        if request.path.startswith('/api/') or request.is_json:
            return jsonify({'error': 'Nicht authentifiziert. Bitte erneut anmelden.'}), 401
        return redirect(url_for('auth.login', next=request.url))

    # Configure rate limiter
    limiter.init_app(app)

    # Register blueprint
    app.register_blueprint(auth_bp)


# Routes
@auth_bp.route('/login', methods=['GET', 'POST'])
@limiter.limit("10 per minute", methods=["POST"], error_message="Zu viele Login-Versuche. Bitte warte eine Minute.")
def login():
    """Login page."""
    # Check directly from DB (not app.config) to avoid multi-worker desync
    if User.count() == 0:
        return redirect(url_for('auth.setup'))

    if current_user.is_authenticated:
        return redirect(url_for('index'))

    form = LoginForm()

    if form.validate_on_submit():
        user, password_hash = User.get_by_username(form.username.data)

        if user and user.is_active and User.verify_password(password_hash, form.password.data):
            login_user(user, remember=True)
            next_page = request.args.get('next')
            return redirect(next_page or url_for('index'))
        else:
            flash('Ungültiger Benutzername oder Passwort', 'error')

    return render_template('login.html', form=form)


@auth_bp.route('/logout')
@login_required
def logout():
    """Logout and redirect to login."""
    logout_user()
    flash('Du wurdest erfolgreich abgemeldet.', 'success')
    return redirect(url_for('auth.login'))


@auth_bp.route('/setup', methods=['GET', 'POST'])
@limiter.exempt
def setup():
    """First-run setup to create admin user."""
    if User.count() > 0:
        return redirect(url_for('auth.login'))

    form = RegisterForm()

    if form.validate_on_submit():
        user = User.create(
            username=form.username.data,
            password=form.password.data,
            display_name=form.display_name.data or form.username.data,
            email=form.email.data,
            is_active=True
        )

        if user:
            login_user(user)
            flash('Admin-Benutzer erstellt! Willkommen!', 'success')
            return redirect(url_for('index'))
        else:
            flash('Fehler beim Erstellen des Benutzers', 'error')

    return render_template('setup.html', form=form)


# User Management Routes
@auth_bp.route('/users')
@login_required
def users():
    """User management page."""
    search = request.args.get('search', '')
    all_users = User.get_all(search if search else None)
    invite_form = InviteForm()
    return render_template('users.html', users=all_users, search=search, invite_form=invite_form, user=current_user)


@auth_bp.route('/users/invite', methods=['POST'])
@login_required
def invite_user():
    """Send invitation to new user."""
    form = InviteForm()

    if form.validate_on_submit():
        email = form.email.data

        existing = User.get_by_email(email)
        if existing:
            flash('Diese E-Mail-Adresse ist bereits registriert', 'error')
            return redirect(url_for('auth.users'))

        token = User.create_invitation(email)

        if token:
            invite_url = url_for('auth.accept_invite', token=token, _external=True)

            try:
                send_invitation_email(email, invite_url)
                flash(f'Einladung an {email} gesendet!', 'success')
            except Exception as e:
                 # In production, do not expose exact error to user, but log it. Here for dev it is ok.
                flash(f'E-Mail konnte nicht gesendet werden: {str(e)}. Einladungslink: {invite_url}', 'warning')
        else:
            flash('Fehler beim Erstellen der Einladung', 'error')
    else:
        for field, errors in form.errors.items():
            for error in errors:
                flash(f"{getattr(form, field).label.text}: {error}", 'error')

    return redirect(url_for('auth.users'))


@auth_bp.route('/invite/<token>', methods=['GET', 'POST'])
def accept_invite(token):
    """Accept invitation page."""
    user = User.get_by_invitation_token(token)

    if not user:
        flash('Ungültiger oder abgelaufener Einladungslink', 'error')
        return redirect(url_for('auth.login'))

    form = AcceptInviteForm()

    if form.validate_on_submit():
        success = user.complete_invitation(
            username=form.username.data,
            password=form.password.data,
            display_name=form.display_name.data
        )

        if success:
            flash('Konto erfolgreich erstellt! Du kannst dich jetzt anmelden.', 'success')
            return redirect(url_for('auth.login'))
        else:
            flash('Benutzername bereits vergeben', 'error')

    return render_template('accept_invite.html', form=form, email=user.email)


@auth_bp.route('/api/users/<int:user_id>', methods=['DELETE'])
@login_required
def delete_user(user_id):
    """Delete a user."""
    if user_id == current_user.id:
        return jsonify({'error': 'Du kannst dich nicht selbst löschen'}), 400

    User.delete(user_id)
    return jsonify({'success': True})


@auth_bp.route('/api/user')
@login_required
def get_current_user_api():
    """API endpoint to get current user data."""
    return {
        'id': current_user.id,
        'username': current_user.username,
        'display_name': current_user.display_name,
        'email': current_user.email
    }


def send_invitation_email(email_addr, invite_url):
    """Send invitation email using SMTP credentials from DB (KMS) or Flask-Mail fallback."""
    
    # 1. Try to fetch user-specific SMTP settings from DB
    smtp_settings = None
    if current_user.is_authenticated:
        try:
            from database import get_user_settings
            from security import decrypt_value
            
            settings = get_user_settings(current_user.id)
            if settings and settings.get('smtp_server') and settings.get('smtp_user'):
                password = decrypt_value(settings.get('encrypted_smtp_password'))
                if password:
                    smtp_settings = {
                        'server': settings.get('smtp_server'),
                        'port': settings.get('smtp_port') or 587,
                        'user': settings.get('smtp_user'),
                        'password': password
                    }
        except Exception as e:
            print(f"Error fetching SMTP settings from DB: {e}")

    # 2. Use Custom SMTP if available
    if smtp_settings:
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart

        msg = MIMEMultipart()
        msg['From'] = smtp_settings['user']
        msg['To'] = email_addr
        msg['Subject'] = 'Einladung zum Leihvertrag-System'

        body = f'''Du wurdest zum Leihvertrag-System eingeladen!

Klicke auf den folgenden Link, um dein Konto zu erstellen:

{invite_url}

Dieser Link ist 7 Tage gültig.

Falls du diese E-Mail nicht erwartet hast, kannst du sie ignorieren.
'''
        msg.attach(MIMEText(body, 'plain'))

        try:
            with smtplib.SMTP(smtp_settings['server'], smtp_settings['port']) as server:
                server.starttls()
                server.login(smtp_settings['user'], smtp_settings['password'])
                server.send_message(msg)
            return
        except Exception as e:
            # Fallback to global config if custom fails? Or raise?
            # User explicitly wants DB settings, so we should raise to show error.
            raise Exception(f"SMTP (DB) send failed: {str(e)}")

    # 3. Fallback to Global Flask-Mail
    try:
        from flask_mail import Message
        from flask import current_app

        mail = current_app.extensions.get('mail')
        if not mail:
            raise Exception('Flask-Mail nicht konfiguriert und keine Benutzereinstellungen gefunden')

        msg = Message(
            'Einladung zum Leihvertrag-System',
            recipients=[email_addr],
            body=f'''Du wurdest zum Leihvertrag-System eingeladen!

Klicke auf den folgenden Link, um dein Konto zu erstellen:

{invite_url}

Dieser Link ist 7 Tage gültig.

Falls du diese E-Mail nicht erwartet hast, kannst du sie ignorieren.
'''
        )
        mail.send(msg)
    except Exception as e:
        raise e

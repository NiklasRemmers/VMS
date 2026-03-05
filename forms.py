from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, SubmitField
from wtforms.validators import DataRequired, Email, EqualTo, Optional

class ProfileForm(FlaskForm):
    display_name = StringField('Anzeigename', validators=[DataRequired()])
    email = StringField('E-Mail', validators=[DataRequired(), Email()])
    submit = SubmitField('Speichern')

class PasswordForm(FlaskForm):
    current_password = PasswordField('Aktuelles Passwort', validators=[DataRequired()])
    new_password = PasswordField('Neues Passwort', validators=[DataRequired()])
    confirm_password = PasswordField('Bestätigen', validators=[DataRequired(), EqualTo('new_password')])
    submit = SubmitField('Passwort ändern')

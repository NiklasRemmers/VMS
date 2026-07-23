"""
SQLAlchemy Models for VMS.
Defines the database schema for PostgreSQL.
"""
import re
from datetime import datetime, timezone
from enum import Enum
from sqlalchemy import (
    Column, Integer, String, Text, Boolean, DateTime, ForeignKey, Index, LargeBinary, Numeric,
    UniqueConstraint
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, relationship

try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo('Europe/Berlin')
except Exception:  # pragma: no cover - zoneinfo/tzdata unavailable
    LOCAL_TZ = None


def to_local(value):
    """Convert a timestamp to local (Europe/Berlin) time for display.

    Timestamps are stored in UTC (the server runs on UTC). A naive value is
    assumed to be UTC. Returns an aware datetime in local time so that
    strftime shows the wall-clock time users expect, not the UTC time."""
    if value is None or not hasattr(value, 'tzinfo'):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    if LOCAL_TZ is not None:
        return value.astimezone(LOCAL_TZ)
    return value


def format_de_date(value):
    """Format a stored date as DD.MM.YYYY for display.

    Accepts a date/datetime object or a string in ISO (YYYY-MM-DD, optionally
    with a time part), German (DD.MM.YYYY) or short German (DD.MM.YY) form.
    Returns the original value unchanged if it is empty or cannot be parsed."""
    if not value:
        return value
    if hasattr(value, 'strftime'):
        return value.strftime('%d.%m.%Y')
    s = str(value).strip()
    for candidate in (s, s[:10]):
        for fmt in ('%d.%m.%Y', '%Y-%m-%d', '%d.%m.%y'):
            try:
                return datetime.strptime(candidate, fmt).strftime('%d.%m.%Y')
            except ValueError:
                continue
    return value


# Matches the time a contract form appends to a date, e.g. "18.07.2026, 14:00 Uhr",
# and the ISO "…T14:30" form. The leading comma/T is required: without it a short
# German date like "18.07.26" would have its "07.26" read as a time.
_TIME_SUFFIX_RE = re.compile(
    r'[,T]\s*(\d{1,2})[:.](\d{2})(?::\d{2})?\s*(?:Uhr)?\s*$', re.IGNORECASE
)


def format_de_datetime(value):
    """Format a stored date as DD.MM.YYYY, keeping a time part if one is present.

    `format_de_date` normalizes the date by parsing only its first ten characters,
    which silently drops the ", HH:MM Uhr" the Leihvertrag form sends along. This
    keeps that suffix and re-attaches it in a consistent "DD.MM.YYYY, HH:MM Uhr"."""
    if not value:
        return value
    if hasattr(value, 'strftime'):
        # A plain `date` has no time, and midnight reads as "no time given".
        if getattr(value, 'hour', 0) or getattr(value, 'minute', 0):
            return value.strftime('%d.%m.%Y, %H:%M Uhr')
        return format_de_date(value)
    s = str(value).strip()
    match = _TIME_SUFFIX_RE.search(s)
    if not match:
        return format_de_date(s)
    formatted_date = format_de_date(s[:match.start()].strip())
    return f"{formatted_date}, {int(match.group(1)):02d}:{match.group(2)} Uhr"


def parse_flexible_date(value):
    """Parse a date from many shapes into a `date` object, or None.

    Accepts a date/datetime object or a string in ISO (YYYY-MM-DD, optionally
    with a time part), German (DD.MM.YYYY), short German (DD.MM.YY), a value with
    a date embedded in extra text, or the German range shorthand "13.-15.11.26"
    (resolved to the start day). Returns None when no date can be found."""
    if not value:
        return None
    if hasattr(value, 'year') and not isinstance(value, str):
        return value.date() if hasattr(value, 'date') else value
    s = str(value).strip()
    # German range shorthand "DD.-DD.MM.YY(YY)": the first day shares the
    # month/year of the second, so resolve it to the start day.
    m = re.match(r'\s*(\d{1,2})\.\s*-\s*\d{1,2}\.(\d{1,2})\.(\d{2,4})', s)
    if not m:
        # First German-style token DD.MM.YYYY / DD.MM.YY.
        m = re.search(r'(\d{1,2})\.(\d{1,2})\.(\d{2,4})', s)
    if m:
        day, month, year = (int(g) for g in m.groups())
        if year < 100:
            year += 2000
        try:
            return datetime(year, month, day).date()
        except ValueError:
            return None
    # ISO token YYYY-MM-DD (also strips any trailing time part).
    m = re.search(r'(\d{4})-(\d{2})-(\d{2})', s)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
        except ValueError:
            return None
    return None


def to_iso_date(value):
    """Normalize a stored/incoming date to ISO 'YYYY-MM-DD' for persistence.

    Returns the original value unchanged if it is empty or cannot be parsed, so
    unexpected free text is preserved rather than silently dropped."""
    if not value:
        return value
    d = parse_flexible_date(value)
    return d.strftime('%Y-%m-%d') if d else value


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = 'users'

    id = Column(Integer, primary_key=True)
    username = Column(String(50), unique=True)
    email = Column(String(255), unique=True)
    password_hash = Column(String(255), nullable=False)
    display_name = Column(String(100))
    is_active = Column(Boolean, default=False)
    invitation_token = Column(String(64))
    invitation_expires = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    # Relationships
    settings = relationship('UserSettings', back_populates='user', uselist=False, cascade='all, delete-orphan')
    candidates = relationship('EmailCandidate', back_populates='user', cascade='all, delete-orphan',
                              foreign_keys='EmailCandidate.user_id')
    sync_state = relationship('EmailSyncState', back_populates='user', uselist=False, cascade='all, delete-orphan')

    def to_dict(self):
        return {
            'id': self.id,
            'username': self.username,
            'email': self.email,
            'display_name': self.display_name,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }


class UserSettings(Base):
    __tablename__ = 'user_settings'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, unique=True)

    # Email config
    email_provider = Column(String(20), default='unconfigured')
    email_address = Column(String(255))
    auth_type = Column(String(20))

    # IMAP
    imap_server = Column(String(255))
    imap_port = Column(Integer, default=993)
    imap_user = Column(String(255))
    encrypted_imap_password = Column(Text)

    # SMTP
    smtp_server = Column(String(255))
    smtp_port = Column(Integer, default=587)
    smtp_user = Column(String(255))
    encrypted_smtp_password = Column(Text)

    # Kanboard
    kanboard_url = Column(String(500))
    kanboard_user = Column(String(100))
    encrypted_kanboard_token = Column(Text)
    encrypted_signature = Column(LargeBinary)
    kanboard_project_id = Column(Integer, default=25)

    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    # Relationships
    user = relationship('User', back_populates='settings')

    def to_dict(self):
        """Return dict without sensitive fields."""
        return {
            'user_id': self.user_id,
            'email_provider': self.email_provider,
            'email_address': self.email_address,
            'auth_type': self.auth_type,
            'imap_server': self.imap_server,
            'imap_port': self.imap_port,
            'imap_user': self.imap_user,
            'smtp_server': self.smtp_server,
            'smtp_port': self.smtp_port,
            'smtp_user': self.smtp_user,
            'kanboard_url': self.kanboard_url,
            'kanboard_user': self.kanboard_user,
            'kanboard_project_id': self.kanboard_project_id,
            'has_imap_password': bool(self.encrypted_imap_password),
            'has_smtp_password': bool(self.encrypted_smtp_password),
            'has_kanboard_token': bool(self.encrypted_kanboard_token),
            'kanboard_configured': bool(self.kanboard_url and self.encrypted_kanboard_token),
        }


class CandidateStatus(str, Enum):
    """Lebenszyklus eines Vorgangs (Leihanfrage -> Verleih -> Abschluss).

    str-Enum, damit bestehende Vergleiche gegen Rohstrings und die
    JSON-Serialisierung unverändert funktionieren -- die Umstellung der ~41
    Literalstellen kann dadurch schrittweise erfolgen.
    """
    PENDING = 'pending'                  # importiert, noch nicht gesichtet
    PROCESSED = 'processed'              # gesichtet, Verleih vorbereitet
    DONE = 'done'                        # Leihvertrag erzeugt
    INVOICE_PENDING = 'invoice_pending'  # zurück, Rechnung/Umbuchung offen
    RETURNED = 'returned'                # zurück, ohne Rechnung erledigt
    INVOICED = 'invoiced'                # Rechnung/Umbuchung verschickt


# Ein laufender Verleih. Diese Paarung stand vorher an vier Stellen als
# ('processed', 'done') im Code, ohne je benannt zu werden.
ACTIVE_STATUSES = (CandidateStatus.PROCESSED, CandidateStatus.DONE)
# Endzustände: tauchen nicht mehr in Rückgaben auf, landen im Archiv.
TERMINAL_STATUSES = (CandidateStatus.RETURNED, CandidateStatus.INVOICED)


def set_candidate_status(session, candidate_id, status):
    """Setze den Status eines Vorgangs und pflege `contract_created` mit.

    Einzige Stelle, die diese Kopplung kennt. Sie gilt nur für zwei Übergänge:

      * -> DONE:      der Leihvertrag ist erzeugt worden  -> True
      * -> PROCESSED: der Vertrag wurde zurückgenommen    -> False
        (das ist der "Vertrag entfernen"-Pfad aus der Oberfläche)

    Alle anderen Übergänge lassen das Flag **unberührt**: ein erzeugter Vertrag
    ist eine historische Tatsache, die eine Rückgabe oder eine Rechnung nicht
    ungeschehen macht.

    Gibt das geänderte Objekt zurück oder None, wenn es den Vorgang nicht gibt.
    """
    status = CandidateStatus(status)
    row = session.query(EmailCandidate).filter_by(id=candidate_id).first()
    if row is None:
        return None

    row.status = status.value
    if status is CandidateStatus.DONE:
        row.contract_created = True
    elif status is CandidateStatus.PROCESSED:
        row.contract_created = False
    return row


class EmailCandidate(Base):
    __tablename__ = 'email_candidates'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    email_id = Column(String(500), unique=True)
    subject = Column(Text)
    sender = Column(String(500))
    received_at = Column(DateTime(timezone=True))

    # Parsed fields
    vorname_nachname = Column(String(200))
    anschrift = Column(Text)
    email_address = Column(String(255))
    telefon = Column(String(50))
    veranstaltungsname = Column(String(300))
    veranstaltungsart = Column(String(100))
    veranstaltungsort = Column(String(300))
    veranstaltungsbereich = Column(String(100))
    personenzahl = Column(String(50))
    datum = Column(String(20))
    end_date = Column(String(20))
    material = Column(Text)
    sonstiges = Column(Text)
    rahmenbedingungen = Column(Text)
    raw_content = Column(Text)

    tags = Column(JSONB, default=list)
    status = Column(String(30), default=CandidateStatus.PENDING.value)  # siehe CandidateStatus
    kanboard_task_id = Column(Integer)
    contract_created = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    # Return workflow
    return_note = Column(Text, nullable=True)
    returned_at = Column(DateTime(timezone=True), nullable=True)
    laufende_nummer = Column(String(20), nullable=True)
    nummer_typ = Column(String(30), nullable=True)  # 'rechnung' or 'umbuchung'

    # The VMS user in charge of this loan (not the importing user).
    responsible_user_id = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'), nullable=True)

    # Relationships
    user = relationship('User', back_populates='candidates', foreign_keys=[user_id])
    responsible = relationship('User', foreign_keys=[responsible_user_id])

    __table_args__ = (
        Index('idx_candidates_user_id', 'user_id'),
        Index('idx_candidates_status', 'status'),
        Index('idx_candidates_datum', 'datum'),
    )

    def to_dict(self):
        d = {col.name: getattr(self, col.name) for col in self.__table__.columns}
        # Resolve the responsible user to a display string for the frontend,
        # the same way DocumentTemplate exposes its uploader.
        d['responsible_name'] = (
            (self.responsible.display_name or self.responsible.username)
            if self.responsible else None
        )
        return d


class EmailSyncState(Base):
    __tablename__ = 'email_sync_state'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, unique=True)
    last_sync = Column(DateTime(timezone=True))

    # Relationships
    user = relationship('User', back_populates='sync_state')


class SequentialNumber(Base):
    """Tracks the last-used sequential number per type (rechnung, umbuchung)."""
    __tablename__ = 'sequential_numbers'

    id = Column(Integer, primary_key=True)
    number_type = Column(String(30), unique=True, nullable=False)  # 'rechnung' or 'umbuchung'
    last_number = Column(Integer, default=0, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


# Die einzige Quelle der Wahrheit für die erlaubten Nummernkreise. Vorher stand
# die Liste nur in invoice_routes, weshalb der Rückgabe-Endpunkt in app.py sie
# nicht geprüft hat und beliebige Typen anlegen konnte.
INVOICE_TYPES = ('rechnung', 'umbuchung')


class NummerBereitsVergeben(Exception):
    """Die gewünschte laufende Nummer ist in diesem Nummernkreis schon benutzt."""


def claim_sequential_number(session, number_type, requested=None):
    """Reserviere verbindlich eine laufende Nummer und gib sie zurück.

    Vorher war die Vergabe ein reines Peek (``last_number + 1``, kein
    Schreibvorgang) plus ein "advance-to-max" beim Speichern — zwei Clients, die
    vor dem Absenden peekten, bekamen dieselbe Nummer. Hier wird die Zählerzeile
    stattdessen gesperrt, sodass parallele Aufrufe sich serialisieren.

    ``requested`` erlaubt es, eine Nummer manuell vorzugeben (z. B. um eine
    Altrechnung nachzutragen); sie wird nur akzeptiert, wenn sie noch frei ist.
    Ohne Vorgabe wird ``last_number + 1`` vergeben.

    Der Aufrufer muss die Transaktion committen — die Sperre hält bis dahin.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    if number_type not in INVOICE_TYPES:
        raise ValueError(f'Unbekannter Nummernkreis: {number_type}')

    # Die Zeile muss existieren, damit es etwas zu sperren gibt. ON CONFLICT
    # DO NOTHING macht das auch dann rennfrei, wenn zwei Prozesse den ersten
    # Vorgang eines Typs gleichzeitig anlegen.
    session.execute(
        pg_insert(SequentialNumber.__table__)
        .values(number_type=number_type, last_number=0)
        .on_conflict_do_nothing(index_elements=['number_type'])
    )
    seq = (session.query(SequentialNumber)
           .filter_by(number_type=number_type)
           .with_for_update()
           .one())

    if requested is None or requested == '':
        nummer = seq.last_number + 1
    else:
        try:
            nummer = int(requested)
        except (TypeError, ValueError):
            raise ValueError(f'Laufende Nummer ist keine Zahl: {requested!r}')
        if nummer <= 0:
            # '0' ist truthy und kam früher als echte Rechnungsnummer durch.
            raise ValueError('Laufende Nummer muss größer als 0 sein')
        schon_vergeben = (session.query(EmailCandidate)
                          .filter_by(nummer_typ=number_type, laufende_nummer=str(nummer))
                          .first())
        if schon_vergeben is not None:
            raise NummerBereitsVergeben(
                f'{number_type.capitalize()} Nr. {nummer} ist bereits vergeben')

    if nummer > seq.last_number:
        seq.last_number = nummer
    return nummer


def release_sequential_number(session, number_type, nummer):
    """Gib eine reservierte Nummer wieder frei, wenn seither niemand weiterzählte.

    Wird nach einem fehlgeschlagenen Versand aufgerufen: die Nummer ist dann
    reserviert, aber nie auf einem Dokument gelandet. Nur der Rückbau des
    eigenen Schrittes ist sicher — hat inzwischen jemand eine höhere Nummer
    gezogen, bleibt die Lücke bestehen, statt eine fremde Nummer zu recyceln.
    """
    seq = (session.query(SequentialNumber)
           .filter_by(number_type=number_type)
           .with_for_update()
           .first())
    if seq is not None and seq.last_number == nummer:
        seq.last_number = nummer - 1


class InventoryItem(Base):
    __tablename__ = 'inventory_items'

    id = Column(Integer, primary_key=True)
    name = Column(String(200), unique=True, nullable=False)
    description = Column(Text)  # The text that goes into the contract
    type = Column(String(50), nullable=False)  # 'equipment' or 'consumable'

    # Consumable-specific fields
    price = Column(Numeric(10, 2), nullable=True)  # Price per unit
    unit = Column(String(50), nullable=True)  # e.g. Stück, Liter, kg
    
    # For bundles
    bundles = relationship('BundleItem', back_populates='item', cascade='all, delete-orphan')

    def to_dict(self):
        d = {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'type': self.type,
            'price': float(self.price) if self.price is not None else None,
            'unit': self.unit,
        }
        return d


class Bundle(Base):
    __tablename__ = 'bundles'

    id = Column(Integer, primary_key=True)
    name = Column(String(200), unique=True, nullable=False)
    
    # Relationships
    items = relationship('BundleItem', back_populates='bundle', cascade='all, delete-orphan')

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'items': [item.to_dict() for item in self.items]
        }


class BundleItem(Base):
    __tablename__ = 'bundle_items'

    id = Column(Integer, primary_key=True)
    bundle_id = Column(Integer, ForeignKey('bundles.id', ondelete='CASCADE'), nullable=False)
    item_id = Column(Integer, ForeignKey('inventory_items.id', ondelete='CASCADE'), nullable=False)
    count = Column(Integer, default=1)

    # Relationships
    bundle = relationship('Bundle', back_populates='items')
    item = relationship('InventoryItem', back_populates='bundles')

    def to_dict(self):
        return {
            'item_id': self.item_id,
            'item_name': self.item.name if self.item else None,
            'count': self.count
        }


class StorageLocation(Base):
    """A physical storage location (Schrank / Spind-Fach) with a code.

    Can be assigned to a loan (EmailCandidate) to show that its material is
    prepared there. Freed automatically when the loan is returned."""
    __tablename__ = 'storage_locations'

    id = Column(Integer, primary_key=True)
    name = Column(String(100), unique=True, nullable=False)  # e.g. "Schrank 5", "Spind 3"
    code = Column(String(100), nullable=True)  # shown on the Verleih page
    candidate_id = Column(Integer, ForeignKey('email_candidates.id', ondelete='SET NULL'), nullable=True)

    candidate = relationship('EmailCandidate')

    __table_args__ = (
        Index('idx_storage_candidate_id', 'candidate_id'),
    )

    def to_dict(self):
        candidate = None
        if self.candidate is not None:
            candidate = {
                'id': self.candidate.id,
                'name': self.candidate.vorname_nachname,
                'event': self.candidate.veranstaltungsname,
                'datum': format_de_date(self.candidate.datum),
                'email': self.candidate.email_address,
            }
        return {
            'id': self.id,
            'name': self.name,
            'code': self.code,
            'candidate_id': self.candidate_id,
            'candidate': candidate,
        }


class CodeShareLink(Base):
    """A public, tokenized link that reveals a loan's storage codes.

    One link per loan (candidate). The codes are only shown from the loan's
    start date onwards; the token grants access without login."""
    __tablename__ = 'code_share_links'

    id = Column(Integer, primary_key=True)
    token = Column(String(64), unique=True, nullable=False, index=True)
    candidate_id = Column(Integer, ForeignKey('email_candidates.id', ondelete='CASCADE'),
                          unique=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    candidate = relationship('EmailCandidate')

    def to_dict(self):
        return {
            'id': self.id,
            'token': self.token,
            'candidate_id': self.candidate_id,
        }


class DocumentTemplate(Base):
    """An uploaded ODT template. Exactly one version per template_type is active.

    Older versions are kept so a bad upload can be rolled back without needing
    the previous file at hand.
    """
    __tablename__ = 'document_templates'

    id = Column(Integer, primary_key=True)
    template_type = Column(String(30), nullable=False)  # leihvertrag | rechnung | umbuchung
    version = Column(Integer, nullable=False)
    filename = Column(String(255), nullable=False)
    encrypted_content = Column(LargeBinary, nullable=False)
    content_hash = Column(String(64), nullable=False)  # sha256 of the plaintext ODT
    size_bytes = Column(Integer, nullable=False)
    is_active = Column(Boolean, default=False, nullable=False)
    note = Column(Text)  # change note supplied by the uploader
    uploaded_by = Column(Integer, ForeignKey('users.id', ondelete='SET NULL'))
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    uploader = relationship('User')

    __table_args__ = (
        Index('idx_document_templates_type_active', 'template_type', 'is_active'),
        UniqueConstraint('template_type', 'version', name='uq_document_templates_type_version'),
    )

    def to_dict(self):
        return {
            'id': self.id,
            'template_type': self.template_type,
            'version': self.version,
            'filename': self.filename,
            'size_bytes': self.size_bytes,
            'is_active': self.is_active,
            'note': self.note,
            'uploaded_by': self.uploader.display_name if self.uploader else None,
            'created_at': to_local(self.created_at).strftime('%d.%m.%Y %H:%M') if self.created_at else None,
        }

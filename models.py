"""
SQLAlchemy Models for VMS.
Defines the database schema for PostgreSQL.
"""
from datetime import datetime, timezone
from sqlalchemy import (
    Column, Integer, String, Text, Boolean, DateTime, ForeignKey, Index, LargeBinary
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, relationship


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
    candidates = relationship('EmailCandidate', back_populates='user', cascade='all, delete-orphan')
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
    status = Column(String(20), default='pending')
    kanboard_task_id = Column(Integer)
    contract_created = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))

    # Relationships
    user = relationship('User', back_populates='candidates')

    __table_args__ = (
        Index('idx_candidates_user_id', 'user_id'),
        Index('idx_candidates_status', 'status'),
        Index('idx_candidates_datum', 'datum'),
    )

    def to_dict(self):
        return {col.name: getattr(self, col.name) for col in self.__table__.columns}


class EmailSyncState(Base):
    __tablename__ = 'email_sync_state'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id', ondelete='CASCADE'), nullable=False, unique=True)
    last_sync = Column(DateTime(timezone=True))

    # Relationships
    user = relationship('User', back_populates='sync_state')


class InventoryItem(Base):
    __tablename__ = 'inventory_items'

    id = Column(Integer, primary_key=True)
    name = Column(String(200), unique=True, nullable=False)
    description = Column(Text)  # The text that goes into the contract
    type = Column(String(50), nullable=False)  # 'equipment' or 'case'
    
    # For bundles
    bundles = relationship('BundleItem', back_populates='item', cascade='all, delete-orphan')

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'type': self.type
        }


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
            'item_type': self.item.type if self.item else 'equipment',
            'count': self.count
        }




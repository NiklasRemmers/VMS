"""
Centralized Database Management for Contract Maker.
Provides SQLAlchemy engine, session factory, and helper functions.
Loads DATABASE_URL from KMS (production) or .env (development).
"""
import os
from contextlib import contextmanager
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session

# Resolve DATABASE_URL
_database_url = None


def _get_database_url() -> str:
    """Get DATABASE_URL from KMS or environment."""
    global _database_url
    if _database_url:
        return _database_url

    # Try KMS first
    try:
        from kms import is_kms_available, get_secret
        if is_kms_available():
            url = get_secret('DATABASE_URL')
            if url:
                _database_url = url
                return _database_url
    except Exception:
        pass

    # Fallback to .env
    _database_url = os.environ.get(
        'DATABASE_URL',
        'postgresql://admin:admin@localhost:5432/contract_maker'
    )
    return _database_url


# Engine and session factory (lazy init)
_engine = None
_SessionFactory = None


def get_engine():
    """Get or create the SQLAlchemy engine."""
    global _engine
    if _engine is None:
        url = _get_database_url()
        _engine = create_engine(
            url,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
            echo=os.environ.get('SQL_ECHO', '').lower() == 'true',
        )
    return _engine


def get_session_factory():
    """Get or create the session factory."""
    global _SessionFactory
    if _SessionFactory is None:
        _SessionFactory = sessionmaker(bind=get_engine())
    return _SessionFactory


def get_scoped_session():
    """Get a thread-local scoped session."""
    return scoped_session(get_session_factory())


@contextmanager
def get_session():
    """Context manager for database sessions. Auto-commits or rolls back."""
    Session = get_session_factory()
    session = Session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db():
    """Create all tables if they don't exist."""
    from models import Base
    engine = get_engine()
    Base.metadata.create_all(engine)


def get_user_settings(user_id: int) -> dict:
    """Fetch settings for a specific user ID. Returns dict or None."""
    from models import UserSettings
    with get_session() as session:
        settings = session.query(UserSettings).filter_by(user_id=user_id).first()
        if settings:
            # Return a detached dict so it works outside the session
            return {col.name: getattr(settings, col.name) for col in settings.__table__.columns}
        return None

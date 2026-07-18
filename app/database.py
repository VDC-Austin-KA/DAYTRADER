"""Database engine and session management."""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

connect_args = {}
if settings.sqlalchemy_url.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_engine(
    settings.sqlalchemy_url,
    pool_pre_ping=True,
    connect_args=connect_args,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI dependency that yields a scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def rebind(url: str) -> None:
    """Point the engine and session factory at a different database.

    Use this instead of ``importlib.reload(database)``. A reload builds a
    brand-new ``Base``, orphaning every model class that was declared
    against the old one -- ``init_db()`` then creates tables from empty
    metadata and the next query fails with "no such table". Rebinding
    swaps only the engine, so the mappings stay intact.
    """
    global engine

    args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine.dispose()
    engine = create_engine(url, pool_pre_ping=True, connect_args=args)
    SessionLocal.configure(bind=engine)


def init_db() -> None:
    """Create tables if they do not already exist."""
    from . import models  # noqa: F401  (ensure models are registered)

    Base.metadata.create_all(bind=engine)

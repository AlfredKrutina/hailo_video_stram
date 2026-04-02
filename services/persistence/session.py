"""Engine a session factory."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from services.persistence.models import Base

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def get_database_url() -> str | None:
    return os.environ.get("DATABASE_URL")


def get_engine() -> Engine | None:
    global _engine
    url = get_database_url()
    if not url:
        return None
    if _engine is None:
        _engine = create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=10)
    return _engine


def init_db() -> bool:
    eng = get_engine()
    if not eng:
        return False
    Base.metadata.create_all(bind=eng)
    global _SessionLocal
    _SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=eng)
    return True


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    if _SessionLocal is None:
        raise RuntimeError("database not initialized")
    s = _SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()

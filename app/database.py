from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import get_settings

_db_url = get_settings().DATABASE_URL
_engine_kwargs = {"pool_pre_ping": True}
if not _db_url.startswith("sqlite"):
    _engine_kwargs.update(pool_size=10, max_overflow=20, pool_timeout=15, pool_recycle=1800)
engine = create_engine(_db_url, **_engine_kwargs)
SessionLocal = sessionmaker(bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

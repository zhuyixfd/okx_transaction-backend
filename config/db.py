from __future__ import annotations

from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from config.constant import config as db_config


# Create SQLAlchemy engine lazily (no connection attempt on import).
# When the first session is used, SQLAlchemy will connect using this URL.
engine = create_engine(
    db_config.mysql_url,
    pool_pre_ping=db_config.MYSQL_POOL_PRE_PING,
    pool_recycle=3600,
    future=True,
)

Base = declarative_base()

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
    class_=Session,
)


def get_db() -> Generator[Session, None, None]:
    """
    FastAPI dependency for getting a DB session.

    Usage:
        async def route(db: Session = Depends(get_db)):
            ...
    """
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """
    Create all tables declared under `config.db.Base`.

    Note: Call this after importing all model modules so their metadata is registered.
    """
    Base.metadata.create_all(bind=engine)


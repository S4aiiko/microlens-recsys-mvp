from __future__ import annotations

from collections.abc import Generator, Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


def create_database_engine(database_url: str, *, echo: bool = False) -> Engine:
    if not database_url:
        raise ValueError("database_url must be explicit")
    return create_engine(
        database_url,
        echo=echo,
        pool_pre_ping=True,
        future=True,
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, future=True)


def session_dependency(factory: sessionmaker[Session]):
    def dependency() -> Generator[Session, None, None]:
        with factory() as session:
            yield session

    return dependency


@contextmanager
def transactional_session(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Own one application transaction; repositories must never commit it."""

    with factory() as session, session.begin():
        yield session

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from apps.api.app.db.base import ensure_utc
from apps.api.app.db.models import AccountStatus, Item, OnlineStatus, User

from .domain import (
    AuthoritativeItem,
    ItemProjection,
    SearchPermissionDenied,
    SearchPrincipal,
    SearchQuery,
)


class SqlAlchemyPostgresSearchAuthority:
    """Current-item and fallback queries for the existing PostgreSQL schema.

    Item-level ACLs do not exist in the current schema, so the present permission
    boundary is an enabled user loaded by id from PostgreSQL. If ACLs are introduced,
    their predicates must be added to both `authorize_hits` and `fallback_search`.
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self.session_factory = session_factory

    def iter_online_documents(self, *, batch_size: int) -> Iterable[tuple[ItemProjection, ...]]:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        with self.session_factory() as session:
            rows = session.scalars(
                select(Item)
                .where(Item.online_status == OnlineStatus.ONLINE)
                .order_by(Item.id)
                .execution_options(yield_per=batch_size)
            )
            batch: list[ItemProjection] = []
            for item in rows:
                batch.append(_projection(item))
                if len(batch) == batch_size:
                    yield tuple(batch)
                    batch.clear()
            if batch:
                yield tuple(batch)

    def current_items(self, item_ids: tuple[str, ...]) -> dict[str, ItemProjection]:
        if not item_ids:
            return {}
        with self.session_factory() as session:
            rows = session.scalars(select(Item).where(Item.id.in_(item_ids)))
            return {item.id: _projection(item) for item in rows}

    def authorize_hits(
        self,
        query: SearchQuery,
        principal: SearchPrincipal,
        item_ids: tuple[str, ...],
    ) -> tuple[dict[str, AuthoritativeItem], int]:
        with self.session_factory() as session:
            self._require_enabled_principal(session, principal)
            if not item_ids:
                return {}, 0
            rows = session.scalars(
                select(Item).where(
                    Item.id.in_(item_ids),
                    Item.online_status == OnlineStatus.ONLINE,
                    _matches(query),
                )
            )
            allowed = {item.id: _authoritative(item) for item in rows}
            # The current schema has no per-item ACL, so filtered hits are stale/missing,
            # not permission-filtered. The explicit count keeps the port ACL-ready.
            return allowed, 0

    def fallback_search(
        self,
        query: SearchQuery,
        principal: SearchPrincipal,
        *,
        exclude_item_ids: tuple[str, ...],
        limit: int,
    ) -> list[AuthoritativeItem]:
        if limit < 0 or limit > 100:
            raise ValueError("fallback limit must be between 0 and 100")
        if limit == 0:
            return []
        with self.session_factory() as session:
            self._require_enabled_principal(session, principal)
            statement: Select[tuple[Item]] = select(Item).where(
                Item.online_status == OnlineStatus.ONLINE,
                _matches(query),
            )
            if exclude_item_ids:
                statement = statement.where(Item.id.not_in(exclude_item_ids))
            statement = statement.order_by(
                func.coalesce(Item.likes_snapshot, 0).desc(),
                func.coalesce(Item.views_snapshot, 0).desc(),
                Item.id,
            ).limit(limit)
            return [_authoritative(item) for item in session.scalars(statement)]

    def fallback_ready(self) -> bool:
        try:
            with self.session_factory() as session:
                session.execute(select(1)).scalar_one()
        except Exception:
            return False
        return True

    @staticmethod
    def _require_enabled_principal(session: Session, principal: SearchPrincipal) -> User:
        user = session.get(User, principal.user_id)
        if user is None or user.status != AccountStatus.ENABLED:
            raise SearchPermissionDenied("current PostgreSQL principal is not enabled")
        return user


def _matches(query: SearchQuery):
    escaped = query.text.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"
    return or_(
        func.lower(Item.id).like(pattern, escape="\\"),
        func.lower(Item.title).like(pattern, escape="\\"),
    )


def _projection(item: Item) -> ItemProjection:
    return ItemProjection(
        item_id=item.id,
        title=item.title,
        likes_snapshot=item.likes_snapshot,
        views_snapshot=item.views_snapshot,
        online=item.online_status == OnlineStatus.ONLINE,
        state_version=item.state_version,
        updated_at=ensure_utc(item.updated_at),
    )


def _authoritative(item: Item) -> AuthoritativeItem:
    return AuthoritativeItem(
        item_id=item.id,
        title=item.title,
        likes_snapshot=item.likes_snapshot,
        views_snapshot=item.views_snapshot,
        state_version=item.state_version,
        updated_at=ensure_utc(item.updated_at),
    )

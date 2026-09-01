from __future__ import annotations

from .domain import (
    READ_ALIAS,
    AuthorityUnavailable,
    ProjectionUnavailable,
    SearchPrincipal,
    SearchQuery,
    SearchResponse,
    SearchResultItem,
)
from .ports import PostgresSearchAuthority, SearchProjection


class AuthoritativeSearchService:
    """Use Elasticsearch for candidates and PostgreSQL for every delivered item."""

    def __init__(
        self,
        projection: SearchProjection,
        authority: PostgresSearchAuthority,
        *,
        oversample_factor: int = 3,
        maximum_candidates: int = 300,
    ) -> None:
        if oversample_factor < 1:
            raise ValueError("oversample_factor must be positive")
        if maximum_candidates < 1:
            raise ValueError("maximum_candidates must be positive")
        self.projection = projection
        self.authority = authority
        self.oversample_factor = oversample_factor
        self.maximum_candidates = maximum_candidates

    def search(self, query: SearchQuery, principal: SearchPrincipal) -> SearchResponse:
        try:
            targets = self.projection.alias_targets(READ_ALIAS)
            if len(targets) != 1:
                raise ProjectionUnavailable("read alias does not resolve to one index")
            index_name = targets[0]
            hits = self.projection.search(
                READ_ALIAS,
                query,
                candidate_limit=min(
                    self.maximum_candidates,
                    query.limit * self.oversample_factor,
                ),
            )
        except ProjectionUnavailable:
            fallback = self._fallback(query, principal, exclude=(), limit=query.limit)
            return SearchResponse(
                items=tuple(
                    SearchResultItem(
                        item=item,
                        retrieval_source="postgresql",
                        projection_score=None,
                    )
                    for item in fallback
                ),
                source="postgresql_fallback",
                degraded=True,
                projection_index=None,
                stale_hits_filtered=0,
                permission_hits_filtered=0,
            )

        unique_hits = []
        seen: set[str] = set()
        for hit in hits:
            if hit.item_id in seen:
                continue
            seen.add(hit.item_id)
            unique_hits.append(hit)
        try:
            allowed, permission_filtered = self.authority.authorize_hits(
                query,
                principal,
                tuple(hit.item_id for hit in unique_hits),
            )
        except PermissionError:
            raise
        except Exception as exc:
            # Returning unverified Elasticsearch documents would violate the authority boundary.
            raise AuthorityUnavailable("PostgreSQL could not authorize projection hits") from exc

        selected: list[SearchResultItem] = []
        for hit in unique_hits:
            current = allowed.get(hit.item_id)
            if current is None:
                continue
            selected.append(
                SearchResultItem(
                    item=current,
                    retrieval_source="elasticsearch_verified",
                    projection_score=hit.score,
                )
            )
            if len(selected) == query.limit:
                break
        filtered = len(unique_hits) - len(allowed)
        stale_filtered = max(0, filtered - permission_filtered)

        if len(selected) < query.limit:
            backfill = self._fallback(
                query,
                principal,
                exclude=tuple(result.item.item_id for result in selected),
                limit=query.limit - len(selected),
            )
            selected.extend(
                SearchResultItem(
                    item=item,
                    retrieval_source="postgresql_backfill",
                    projection_score=None,
                )
                for item in backfill
            )

        has_backfill = any(item.retrieval_source == "postgresql_backfill" for item in selected)
        degraded = stale_filtered > 0 or permission_filtered > 0 or has_backfill
        source = "elasticsearch_verified"
        if has_backfill:
            source = "elasticsearch_with_postgresql_backfill"
        return SearchResponse(
            items=tuple(selected[: query.limit]),
            source=source,
            degraded=degraded,
            projection_index=index_name,
            stale_hits_filtered=stale_filtered,
            permission_hits_filtered=permission_filtered,
        )

    def _fallback(
        self,
        query: SearchQuery,
        principal: SearchPrincipal,
        *,
        exclude: tuple[str, ...],
        limit: int,
    ):
        try:
            return self.authority.fallback_search(
                query,
                principal,
                exclude_item_ids=exclude,
                limit=limit,
            )
        except PermissionError:
            raise
        except Exception as exc:
            raise AuthorityUnavailable("PostgreSQL fallback search failed") from exc

from __future__ import annotations

from .domain import READ_ALIAS, IndexHealth, SearchHealthReport
from .ports import PostgresSearchAuthority, SearchIndexRegistry, SearchProjection


class SearchHealthService:
    def __init__(
        self,
        projection: SearchProjection,
        authority: PostgresSearchAuthority,
        registry: SearchIndexRegistry,
    ) -> None:
        self.projection = projection
        self.authority = authority
        self.registry = registry

    def report(self) -> SearchHealthReport:
        reasons: list[str] = []
        try:
            fallback_ready = self.authority.fallback_ready()
        except Exception:
            fallback_ready = False
            reasons.append("postgresql_fallback_unavailable")
        else:
            if not fallback_ready:
                reasons.append("postgresql_fallback_unavailable")

        try:
            reachable = self.projection.ping()
        except Exception:
            reachable = False
        physical_index = None
        if not reachable:
            reasons.append("elasticsearch_unreachable")
        else:
            try:
                targets = self.projection.alias_targets(READ_ALIAS)
            except Exception:
                targets = ()
                reasons.append("alias_lookup_failed")
            if len(targets) == 1:
                physical_index = targets[0]
                try:
                    manifest = self.registry.get_build(physical_index)
                except Exception:
                    manifest = None
                    reasons.append("index_registry_unavailable")
                if manifest is None:
                    if "index_registry_unavailable" not in reasons:
                        reasons.append("active_index_missing_authoritative_build_record")
            elif not targets:
                reasons.append("read_alias_missing")
            else:
                reasons.append("read_alias_has_multiple_targets")

        if reachable and physical_index is not None and not reasons and fallback_ready:
            status = IndexHealth.HEALTHY
        elif fallback_ready:
            status = IndexHealth.DEGRADED
        else:
            status = IndexHealth.UNAVAILABLE
        try:
            last_watermark = self.registry.last_source_watermark()
        except Exception:
            last_watermark = None
            if "index_registry_unavailable" not in reasons:
                reasons.append("index_registry_unavailable")
            if status == IndexHealth.HEALTHY:
                status = IndexHealth.DEGRADED
        return SearchHealthReport(
            status=status,
            projection_reachable=reachable,
            fallback_ready=fallback_ready,
            alias=READ_ALIAS,
            physical_index=physical_index,
            reasons=tuple(reasons),
            last_source_watermark=last_watermark,
        )

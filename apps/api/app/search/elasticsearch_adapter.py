from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any, Protocol
from urllib.parse import urlsplit

from .domain import (
    READ_ALIAS,
    BulkResult,
    IndexBuildConflict,
    ItemProjection,
    ProjectionHit,
    ProjectionUnavailable,
    SearchQuery,
    validate_physical_index,
)


class _IndicesClient(Protocol):
    def create(self, **kwargs: Any) -> Any: ...

    def exists(self, **kwargs: Any) -> Any: ...

    def get_alias(self, **kwargs: Any) -> Any: ...

    def refresh(self, **kwargs: Any) -> Any: ...

    def update_aliases(self, **kwargs: Any) -> Any: ...


class ElasticsearchClient(Protocol):
    indices: _IndicesClient

    def options(self, **kwargs: Any) -> ElasticsearchClient: ...

    def ping(self, **kwargs: Any) -> Any: ...

    def bulk(self, **kwargs: Any) -> Any: ...

    def count(self, **kwargs: Any) -> Any: ...

    def search(self, **kwargs: Any) -> Any: ...


class ElasticsearchSearchProjection:
    """Official-client adapter for the disposable item search projection.

    Only item identifiers, projection versions and scores leave this boundary.
    Callers must still authorize and reload every result from PostgreSQL.
    """

    def __init__(self, client: ElasticsearchClient) -> None:
        self._client = client

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        request_timeout_seconds: float = 5.0,
        api_key: str | None = None,
        basic_auth: tuple[str, str] | None = None,
        verify_certs: bool = True,
    ) -> ElasticsearchSearchProjection:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Elasticsearch URL must be an explicit HTTP(S) endpoint")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("Elasticsearch credentials must not be embedded in the URL")
        if parsed.query or parsed.fragment:
            raise ValueError("Elasticsearch URL must not contain a query or fragment")
        if (
            isinstance(request_timeout_seconds, bool)
            or not isinstance(request_timeout_seconds, int | float)
            or not math.isfinite(request_timeout_seconds)
            or request_timeout_seconds <= 0
        ):
            raise ValueError("Elasticsearch request timeout must be finite and positive")
        if api_key is not None and basic_auth is not None:
            raise ValueError("configure either API key or basic authentication, not both")
        if api_key is not None and (not isinstance(api_key, str) or not api_key):
            raise ValueError("Elasticsearch API key must be a non-empty string")
        if basic_auth is not None and (
            not isinstance(basic_auth, tuple)
            or len(basic_auth) != 2
            or any(not isinstance(value, str) or not value for value in basic_auth)
        ):
            raise ValueError("Elasticsearch basic authentication must contain two strings")
        if not isinstance(verify_certs, bool):
            raise ValueError("verify_certs must be boolean")

        # Imported lazily so dependency-light contract tooling can import the project.
        from elasticsearch import Elasticsearch

        client = Elasticsearch(
            url,
            api_key=api_key,
            basic_auth=basic_auth,
            verify_certs=verify_certs,
            request_timeout=request_timeout_seconds,
            max_retries=1,
            retry_on_timeout=False,
            sniff_on_start=False,
            sniff_before_requests=False,
            sniff_on_node_failure=False,
        )
        return cls(client)

    def ping(self) -> bool:
        try:
            response = _body(self._timed(2.0).ping())
        except Exception as exc:
            raise _unavailable("health check", exc) from exc
        if not isinstance(response, bool):
            raise ProjectionUnavailable("Elasticsearch health check returned malformed data")
        return response

    def alias_targets(self, alias: str) -> tuple[str, ...]:
        _require_read_alias(alias)
        try:
            response = self._timed(5.0).indices.get_alias(
                name=alias,
                allow_no_indices=True,
                ignore_unavailable=True,
            )
        except Exception as exc:
            if _status_code(exc) == 404:
                return ()
            raise _unavailable("alias lookup", exc) from exc
        body = _mapping_body(response, operation="alias lookup")
        targets: list[str] = []
        for index_name, value in body.items():
            if not isinstance(index_name, str) or not isinstance(value, Mapping):
                raise ProjectionUnavailable("Elasticsearch alias lookup returned malformed data")
            try:
                validate_physical_index(index_name)
            except ValueError as exc:
                raise ProjectionUnavailable(
                    "Elasticsearch alias resolved outside the project namespace"
                ) from exc
            aliases = value.get("aliases")
            if not isinstance(aliases, Mapping) or alias not in aliases:
                raise ProjectionUnavailable("Elasticsearch alias lookup returned malformed data")
            targets.append(index_name)
        return tuple(sorted(targets))

    def index_exists(self, physical_index: str) -> bool:
        validate_physical_index(physical_index)
        try:
            response = _body(self._timed(5.0).indices.exists(index=physical_index))
        except Exception as exc:
            raise _unavailable("index existence check", exc) from exc
        if not isinstance(response, bool):
            raise ProjectionUnavailable(
                "Elasticsearch index existence check returned malformed data"
            )
        return response

    def create_index(
        self,
        physical_index: str,
        *,
        settings: dict[str, Any],
        mappings: dict[str, Any],
    ) -> None:
        validate_physical_index(physical_index)
        try:
            response = self._timed(10.0).indices.create(
                index=physical_index,
                settings=settings,
                mappings=mappings,
            )
        except Exception as exc:
            raise _unavailable("index creation", exc) from exc
        _require_acknowledged(response, operation="index creation")

    def bulk_apply(
        self,
        target: str,
        *,
        upserts: tuple[ItemProjection, ...],
        deletes: tuple[str, ...],
    ) -> BulkResult:
        physical_index = self._resolve_target(target)
        upsert_ids = [document.item_id for document in upserts]
        if len(upsert_ids) != len(set(upsert_ids)):
            raise ValueError("bulk upsert item ids must be unique")
        if len(deletes) != len(set(deletes)):
            raise ValueError("bulk delete item ids must be unique")
        if set(upsert_ids).intersection(deletes):
            raise ValueError("an item cannot be upserted and deleted in one bulk request")
        if any(not item_id or len(item_id) > 255 for item_id in deletes):
            raise ValueError("bulk delete item ids must contain 1..255 characters")
        expected = [("index", document.item_id) for document in upserts]
        expected.extend(("delete", item_id) for item_id in deletes)
        if not expected:
            return BulkResult(succeeded=0)

        operations: list[dict[str, Any]] = []
        for document in upserts:
            operations.append({"index": {"_index": physical_index, "_id": document.item_id}})
            operations.append(document.as_document())
        for item_id in deletes:
            operations.append({"delete": {"_index": physical_index, "_id": item_id}})
        try:
            response = self._timed(30.0).bulk(
                operations=operations,
                refresh=False,
            )
        except Exception as exc:
            raise _unavailable("bulk projection update", exc) from exc

        body = _mapping_body(response, operation="bulk projection update")
        items = body.get("items")
        if not isinstance(items, Sequence) or isinstance(items, str | bytes):
            raise ProjectionUnavailable("Elasticsearch bulk update returned malformed data")
        if len(items) != len(expected):
            raise ProjectionUnavailable("Elasticsearch bulk update returned an invalid item count")

        succeeded = 0
        failures: list[str] = []
        for expected_action, response_item in zip(expected, items, strict=True):
            action_name, expected_item_id = expected_action
            if not isinstance(response_item, Mapping) or set(response_item) != {action_name}:
                raise ProjectionUnavailable("Elasticsearch bulk update returned malformed data")
            action = response_item[action_name]
            if not isinstance(action, Mapping):
                raise ProjectionUnavailable("Elasticsearch bulk update returned malformed data")
            response_index = action.get("_index")
            response_item_id = action.get("_id")
            status = action.get("status")
            if response_index != physical_index or response_item_id != expected_item_id:
                raise ProjectionUnavailable("Elasticsearch bulk update crossed its target boundary")
            if isinstance(status, bool) or not isinstance(status, int):
                raise ProjectionUnavailable("Elasticsearch bulk update returned malformed data")
            error = action.get("error")
            idempotent_missing_delete = (
                action_name == "delete"
                and status == 404
                and action.get("result") == "not_found"
                and error is None
            )
            if (200 <= status < 300 and error is None) or idempotent_missing_delete:
                succeeded += 1
            else:
                failures.append(expected_item_id)
        return BulkResult(succeeded=succeeded, failed_item_ids=tuple(failures))

    def refresh(self, target: str) -> None:
        physical_index = self._resolve_target(target)
        try:
            self._timed(10.0).indices.refresh(index=physical_index)
        except Exception as exc:
            raise _unavailable("index refresh", exc) from exc

    def count(self, target: str) -> int:
        physical_index = self._resolve_target(target)
        try:
            response = self._timed(10.0).count(index=physical_index)
        except Exception as exc:
            raise _unavailable("index count", exc) from exc
        body = _mapping_body(response, operation="index count")
        count = body.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ProjectionUnavailable("Elasticsearch index count returned malformed data")
        return count

    def seal(self, target: str) -> int:
        """Make accepted writes visible and return the exact visible document count."""

        self.refresh(target)
        return self.count(target)

    def switch_alias(
        self,
        alias: str,
        *,
        new_index: str,
        expected_old_index: str | None,
    ) -> None:
        _require_read_alias(alias)
        validate_physical_index(new_index)
        if expected_old_index is not None:
            validate_physical_index(expected_old_index)
        current_targets = self.alias_targets(alias)
        current = current_targets[0] if len(current_targets) == 1 else None
        if len(current_targets) > 1 or current != expected_old_index:
            raise IndexBuildConflict("search alias precondition failed")
        actions: list[dict[str, Any]] = []
        if current is not None:
            actions.append({"remove": {"index": current, "alias": alias}})
        actions.append({"add": {"index": new_index, "alias": alias}})
        try:
            response = self._timed(10.0).indices.update_aliases(
                actions=actions,
            )
        except Exception as exc:
            raise _unavailable("alias switch", exc) from exc
        _require_acknowledged(response, operation="alias switch")
        if self.alias_targets(alias) != (new_index,):
            raise IndexBuildConflict("search alias switch did not produce one requested target")

    def search(
        self,
        alias: str,
        query: SearchQuery,
        *,
        candidate_limit: int,
    ) -> list[ProjectionHit]:
        _require_read_alias(alias)
        if isinstance(candidate_limit, bool) or not isinstance(candidate_limit, int):
            raise ValueError("candidate_limit must be an integer")
        if candidate_limit < 1 or candidate_limit > 1000:
            raise ValueError("candidate_limit must be between 1 and 1000")
        targets = self.alias_targets(alias)
        if len(targets) != 1:
            raise ProjectionUnavailable("Elasticsearch read alias is not ready")
        expected_index = targets[0]
        try:
            response = self._timed(5.0).search(
                index=alias,
                size=candidate_limit,
                track_total_hits=False,
                source_includes=["item_id", "state_version"],
                query={
                    "multi_match": {
                        "query": query.text,
                        "fields": ["title^3", "item_id"],
                        "operator": "and",
                    }
                },
            )
        except Exception as exc:
            raise _unavailable("candidate search", exc) from exc
        body = _mapping_body(response, operation="candidate search")
        hits_wrapper = body.get("hits")
        if not isinstance(hits_wrapper, Mapping):
            raise ProjectionUnavailable("Elasticsearch search returned malformed data")
        raw_hits = hits_wrapper.get("hits")
        if not isinstance(raw_hits, Sequence) or isinstance(raw_hits, str | bytes):
            raise ProjectionUnavailable("Elasticsearch search returned malformed data")
        if len(raw_hits) > candidate_limit:
            raise ProjectionUnavailable("Elasticsearch search exceeded the candidate limit")

        hits: list[ProjectionHit] = []
        for raw_hit in raw_hits:
            if not isinstance(raw_hit, Mapping):
                raise ProjectionUnavailable("Elasticsearch search returned malformed data")
            index_name = raw_hit.get("_index")
            item_id = raw_hit.get("_id")
            score = raw_hit.get("_score")
            source = raw_hit.get("_source")
            if index_name != expected_index:
                raise ProjectionUnavailable("Elasticsearch search crossed its target boundary")
            if not isinstance(item_id, str) or not isinstance(source, Mapping):
                raise ProjectionUnavailable("Elasticsearch search returned malformed data")
            if source.get("item_id") != item_id:
                raise ProjectionUnavailable("Elasticsearch search returned inconsistent identity")
            state_version = source.get("state_version")
            if isinstance(state_version, bool) or not isinstance(state_version, int):
                raise ProjectionUnavailable("Elasticsearch search returned malformed data")
            if isinstance(score, bool) or not isinstance(score, int | float):
                raise ProjectionUnavailable("Elasticsearch search returned malformed data")
            numeric_score = float(score)
            if not math.isfinite(numeric_score):
                raise ProjectionUnavailable("Elasticsearch search returned a non-finite score")
            try:
                hits.append(
                    ProjectionHit(
                        item_id=item_id,
                        score=numeric_score,
                        indexed_state_version=state_version,
                        index_name=index_name,
                    )
                )
            except ValueError as exc:
                raise ProjectionUnavailable(
                    "Elasticsearch search returned an invalid projection hit"
                ) from exc
        return hits

    def _resolve_target(self, target: str) -> str:
        if target == READ_ALIAS:
            targets = self.alias_targets(target)
            if len(targets) != 1:
                raise ProjectionUnavailable("Elasticsearch read alias is not ready")
            return targets[0]
        validate_physical_index(target)
        return target

    def _timed(self, seconds: float) -> ElasticsearchClient:
        return self._client.options(request_timeout=seconds)


def _require_read_alias(alias: str) -> None:
    if alias != READ_ALIAS:
        raise ValueError("only the configured project read alias is permitted")


def _body(response: Any) -> Any:
    return getattr(response, "body", response)


def _mapping_body(response: Any, *, operation: str) -> Mapping[str, Any]:
    body = _body(response)
    if not isinstance(body, Mapping):
        raise ProjectionUnavailable(f"Elasticsearch {operation} returned malformed data")
    return body


def _require_acknowledged(response: Any, *, operation: str) -> None:
    body = _mapping_body(response, operation=operation)
    if body.get("acknowledged") is not True:
        raise ProjectionUnavailable(f"Elasticsearch {operation} was not acknowledged")


def _status_code(exc: Exception) -> int | None:
    meta = getattr(exc, "meta", None)
    status = getattr(meta, "status", None)
    if isinstance(status, int) and not isinstance(status, bool):
        return status
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and not isinstance(status, bool):
        return status
    return None


def _unavailable(operation: str, _exc: Exception) -> ProjectionUnavailable:
    # Never copy exception text: transport errors can contain endpoint credentials.
    return ProjectionUnavailable(f"Elasticsearch {operation} is unavailable")

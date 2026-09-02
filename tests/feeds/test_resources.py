from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.api.app.cache.redis_adapter import RedisPyCacheBackend
from apps.api.app.db import Base
from apps.api.app.db.models import Item, OnlineStatus, User
from apps.api.app.db.seed import seed_demo_users
from apps.api.app.feeds.resources import (
    ProcessedRecommendationLoader,
    derive_feed_cursor_secret,
    sync_serving_resource,
)
from apps.api.app.runtime import AtomicRuntimeModelSlot, RuntimeContext
from recsys.data.artifacts import JsonLinesCodec
from recsys.data.common import canonical_json_bytes

DATA_VERSION = "data-v1"
MODEL_VERSION = "model-v1"


def _sessions():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


def _fixture(
    root: Path,
    *,
    history_override: list[dict[str, object]] | None = None,
    descriptor_row_delta: int = 0,
) -> tuple[SimpleNamespace, str]:
    codec = JsonLinesCodec()
    version = root / DATA_VERSION
    version.mkdir(parents=True)
    items = [
        {
            "item_id": str(index),
            "title": f"Item {index}",
            "likes_snapshot": index,
            "views_snapshot": index * 10,
            "cover_ref": None,
            "metadata_status": "complete",
        }
        for index in range(1, 6)
    ]
    histories = {
        "1": ((100, "1"), (101, "2")),
        "2": ((100, "1"), (101, "3")),
        "3": ((100, "2"), (101, "4")),
        "4": ((100, "3"), (101, "5")),
    }
    train = [
        {"user_id": user_id, "item_id": item_id, "timestamp": timestamp}
        for user_id, rows in histories.items()
        for timestamp, item_id in rows
    ]
    history = history_override or [
        {
            "user_id": user_id,
            "ordered_item_ids": [item_id for _timestamp, item_id in rows],
            "ordered_timestamps": [timestamp for timestamp, _item_id in rows],
            "split_cutoffs": {"validation_timestamp": None, "test_timestamp": None},
        }
        for user_id, rows in histories.items()
    ]
    tables = {"items": items, "train": train, "user_history": history}
    artifacts = []
    for name, rows in tables.items():
        path = version / f"{name}{codec.suffix}"
        count = codec.write_rows(path, rows)
        artifacts.append(
            {
                "path": path.name,
                "size_bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "rows": count + (descriptor_row_delta if name == "train" else 0),
            }
        )
    manifest = {
        "schema_version": "1.0",
        "data_version": DATA_VERSION,
        "output_schema": {"storage_format": codec.format_name},
        "artifacts": artifacts,
    }
    payload = canonical_json_bytes(manifest) + b"\n"
    (version / "manifest.json").write_bytes(payload)
    checksum = hashlib.sha256(payload).hexdigest()
    bundle = SimpleNamespace(
        model_version=MODEL_VERSION,
        data_version=DATA_VERSION,
        manifest={"data_manifest_checksum": checksum},
        user_ids=tuple(histories),
    )
    return bundle, checksum


def _load(root: Path, bundle: object, checksum: str):
    return ProcessedRecommendationLoader(root, JsonLinesCodec()).load(
        model_version=MODEL_VERSION,
        data_version=DATA_VERSION,
        data_manifest_checksum=checksum,
        bundle=bundle,
    )


def test_loader_builds_exact_train_only_histories_and_cosine(tmp_path: Path) -> None:
    bundle, checksum = _fixture(tmp_path)
    resource = _load(tmp_path, bundle, checksum)
    assert resource.source_histories["1"] == ("1", "2")
    assert resource.item_item_index.source_histories == resource.source_histories
    assert [row[0] for row in resource.item_item_index.neighbors["1"]] == ["2", "3"]
    assert [row[1] for row in resource.item_item_index.neighbors["1"]] == pytest.approx([0.5, 0.5])
    assert [row.item_id for row in resource.item_item_index.recall(("1",), top_n=10)] == [
        "2",
        "3",
    ]
    with pytest.raises(TypeError):
        resource.source_histories["5"] = ("1",)  # type: ignore[index]


@pytest.mark.parametrize("data_version", ["latest", "../escape", "nested/version"])
def test_loader_rejects_mutable_or_unsafe_data_versions(tmp_path: Path, data_version: str) -> None:
    bundle, checksum = _fixture(tmp_path)
    with pytest.raises(ValueError):
        ProcessedRecommendationLoader(tmp_path, JsonLinesCodec()).load(
            model_version=MODEL_VERSION,
            data_version=data_version,
            data_manifest_checksum=checksum,
            bundle=bundle,
        )


def test_loader_rejects_symlink_checksum_descriptor_and_row_mismatches(tmp_path: Path) -> None:
    real = tmp_path / "real"
    bundle, checksum = _fixture(real)
    (tmp_path / DATA_VERSION).symlink_to(real / DATA_VERSION, target_is_directory=True)
    with pytest.raises(ValueError, match="missing or unsafe"):
        _load(tmp_path, bundle, checksum)

    wrong_checksum = "f" * 64
    wrong_bundle = SimpleNamespace(
        **{**vars(bundle), "manifest": {"data_manifest_checksum": wrong_checksum}}
    )
    with pytest.raises(ValueError, match="manifest checksum"):
        _load(real, wrong_bundle, wrong_checksum)

    train = real / DATA_VERSION / "train.jsonl"
    train.write_bytes(train.read_bytes() + b"{}\n")
    with pytest.raises(ValueError, match="size mismatch"):
        _load(real, bundle, checksum)

    row_root = tmp_path / "row"
    row_bundle, row_checksum = _fixture(row_root, descriptor_row_delta=1)
    with pytest.raises(ValueError, match="row count mismatch"):
        _load(row_root, row_bundle, row_checksum)


def test_loader_rejects_history_not_exactly_reconstructed_from_train(tmp_path: Path) -> None:
    bad_history = [
        {
            "user_id": str(index),
            "ordered_item_ids": ["1", "2"] if index != 1 else ["1", "3"],
            "ordered_timestamps": [100, 101],
            "split_cutoffs": {"validation_timestamp": None, "test_timestamp": None},
        }
        for index in range(1, 5)
    ]
    bundle, checksum = _fixture(tmp_path, history_override=bad_history)
    with pytest.raises(ValueError, match="exactly match train"):
        _load(tmp_path, bundle, checksum)


def test_catalog_and_demo_mapping_are_idempotent_and_preserve_operations(tmp_path: Path) -> None:
    bundle, checksum = _fixture(tmp_path)
    resource = _load(tmp_path, bundle, checksum)
    engine, sessions = _sessions()
    try:
        with sessions.begin() as session:
            seed_demo_users(
                session,
                password="valid-demo-password",
                hash_password=lambda _password: "hash",
                normalize_username=lambda value: value.strip().casefold(),
            )
            session.add(
                Item(
                    id="1",
                    title="stale",
                    likes_snapshot=0,
                    views_snapshot=0,
                    metadata_status="partial",
                    online_status=OnlineStatus.OFFLINE,
                    state_version=7,
                )
            )
        with sessions.begin() as session:
            first = sync_serving_resource(session, resource)
        assert first.inserted_items == 4
        assert first.refreshed_items == 1
        assert first.mapped_demo_users == 3
        with sessions.begin() as session:
            second = sync_serving_resource(session, resource)
        assert second.inserted_items == 0
        assert second.refreshed_items == 0
        assert second.mapped_demo_users == 0
        with sessions() as session:
            offline = session.get(Item, "1")
            assert offline.online_status == OnlineStatus.OFFLINE
            assert offline.state_version == 7
            assert offline.title == "Item 1"
            users = {
                user.username_normalized: user
                for user in session.scalars(select(User).order_by(User.username_normalized))
            }
            mappings = [
                users[name].source_user_id for name in ("demo_user_a", "demo_user_b", "demo_user_c")
            ]
            assert len(set(mappings)) == 3
            assert users["admin"].source_user_id is None
            assert users["operator"].source_user_id is None

        with sessions.begin() as session:
            demo_a = session.scalar(select(User).where(User.username_normalized == "demo_user_a"))
            demo_a.source_user_id = "conflict"
        with sessions() as session, pytest.raises(ValueError, match="conflicting"):
            sync_serving_resource(session, resource)
    finally:
        engine.dispose()


def test_cursor_secret_is_stable_and_domain_separated() -> None:
    secret = "jwt-secret-at-least-thirty-two-bytes-long"
    first = derive_feed_cursor_secret(secret)
    assert first == derive_feed_cursor_secret(secret)
    assert first != hashlib.sha256(secret.encode()).digest()
    assert first != secret.encode()


def test_atomic_slot_switches_the_entire_serving_generation(tmp_path: Path) -> None:
    bundle, checksum = _fixture(tmp_path)
    resource_a = _load(tmp_path, bundle, checksum)
    resource_b = replace(resource_a, model_version="model-v2")
    slot = AtomicRuntimeModelSlot()
    slot.swap(model_version=resource_a.model_version, staged_bundle=resource_a)
    assert slot.snapshot() == (MODEL_VERSION, resource_a)
    slot.swap(model_version=resource_b.model_version, staged_bundle=resource_b)
    assert slot.snapshot() == ("model-v2", resource_b)


def test_runtime_closes_sync_async_redis_and_database_exactly_once() -> None:
    class Client:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    class AsyncClient:
        def __init__(self) -> None:
            self.close_calls = 0

        async def aclose(self) -> None:
            self.close_calls += 1

    class Engine:
        def __init__(self) -> None:
            self.dispose_calls = 0

        def dispose(self) -> None:
            self.dispose_calls += 1

    sync_client = Client()
    async_client = AsyncClient()
    engine = Engine()
    runtime = RuntimeContext(
        settings=object(),  # type: ignore[arg-type]
        engine=engine,  # type: ignore[arg-type]
        sessions=object(),  # type: ignore[arg-type]
        redis=async_client,
        redis_cache_backend=RedisPyCacheBackend(sync_client),
    )
    asyncio.run(runtime.close())
    asyncio.run(runtime.close())
    assert sync_client.close_calls == 1
    assert async_client.close_calls == 1
    assert engine.dispose_calls == 1

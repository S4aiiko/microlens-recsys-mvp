from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path
from typing import Any

from .common import artifact_descriptor
from .errors import DataQualityError
from .models import Interaction, Item

PAIR_FILE = "MicroLens-50k_pairs.csv"
TITLE_FILE = "MicroLens-50k_titles.csv"
SNAPSHOT_FILE = "MicroLens-50k_likes_and_views.txt"


def _required_path(raw_dir: Path, name: str) -> Path:
    path = raw_dir / name
    if not path.is_file():
        raise DataQualityError(f"required source file is missing: {path}")
    return path


def parse_pairs(
    path: Path, *, duplicate_policy: str = "reject"
) -> tuple[list[Interaction], dict[str, int]]:
    rows: list[Interaction] = []
    seen: set[tuple[str, str, int]] = set()
    duplicate_count = 0
    with path.open("r", newline="", encoding="ascii") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != ["user", "item", "timestamp"]:
            raise DataQualityError(f"unexpected pairs header: {reader.fieldnames!r}")
        for line_number, row in enumerate(reader, start=2):
            user_id = (row.get("user") or "").strip()
            item_id = (row.get("item") or "").strip()
            timestamp_text = (row.get("timestamp") or "").strip()
            if not user_id or not item_id or not timestamp_text:
                raise DataQualityError(f"pairs line {line_number}: null/empty required value")
            try:
                timestamp = int(timestamp_text)
            except ValueError as exc:
                raise DataQualityError(f"pairs line {line_number}: invalid timestamp") from exc
            if timestamp < 0:
                raise DataQualityError(f"pairs line {line_number}: negative timestamp")
            key = (user_id, item_id, timestamp)
            if key in seen:
                duplicate_count += 1
                if duplicate_policy == "reject":
                    raise DataQualityError(f"pairs line {line_number}: duplicate interaction")
                if duplicate_policy != "drop_exact":
                    raise DataQualityError(f"unsupported duplicate policy: {duplicate_policy}")
                continue
            seen.add(key)
            rows.append(Interaction(user_id, item_id, timestamp))
    if not rows:
        raise DataQualityError("pairs file has no interactions")
    return rows, {"exact_duplicate_rows": duplicate_count}


def parse_titles(path: Path) -> tuple[dict[str, str], dict[str, int]]:
    titles: dict[str, str] = {}
    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != ["item", "title"]:
            raise DataQualityError(f"unexpected titles header: {reader.fieldnames!r}")
        for line_number, row in enumerate(reader, start=2):
            item_id = (row.get("item") or "").strip()
            title = (row.get("title") or "").strip()
            if not item_id or not title:
                raise DataQualityError(f"titles line {line_number}: null/empty required value")
            if item_id in titles:
                raise DataQualityError(f"titles line {line_number}: duplicate item")
            titles[item_id] = title
    if not titles:
        raise DataQualityError("titles file has no items")
    return titles, {"duplicate_items": 0, "empty_titles": 0}


def parse_snapshots(path: Path) -> tuple[dict[str, tuple[int, int]], dict[str, int]]:
    snapshots: dict[str, tuple[int, int]] = {}
    with path.open("r", newline="", encoding="ascii") as handle:
        reader = csv.reader(handle, delimiter="\t")
        for line_number, row in enumerate(reader, start=1):
            if len(row) != 3:
                raise DataQualityError(f"likes/views line {line_number}: expected 3 tab fields")
            item_id = row[0].strip()
            if not item_id:
                raise DataQualityError(f"likes/views line {line_number}: empty item")
            try:
                likes, views = int(row[1]), int(row[2])
            except ValueError as exc:
                raise DataQualityError(
                    f"likes/views line {line_number}: non-integer count"
                ) from exc
            if likes < 0 or views < 0:
                raise DataQualityError(f"likes/views line {line_number}: negative count")
            if item_id in snapshots:
                raise DataQualityError(f"likes/views line {line_number}: duplicate item")
            snapshots[item_id] = (likes, views)
    if not snapshots:
        raise DataQualityError("likes/views file has no items")
    return snapshots, {"duplicate_items": 0, "negative_counts": 0}


def load_official(
    raw_dir: Path,
    *,
    duplicate_policy: str = "reject",
    orphan_policy: str = "reject",
) -> tuple[list[Interaction], list[Item], dict[str, Any]]:
    pair_path = _required_path(raw_dir, PAIR_FILE)
    title_path = _required_path(raw_dir, TITLE_FILE)
    snapshot_path = _required_path(raw_dir, SNAPSHOT_FILE)
    interactions, pair_quality = parse_pairs(pair_path, duplicate_policy=duplicate_policy)
    titles, title_quality = parse_titles(title_path)
    snapshots, snapshot_quality = parse_snapshots(snapshot_path)
    title_ids, snapshot_ids = set(titles), set(snapshots)
    if title_ids != snapshot_ids:
        missing_snapshot = sorted(title_ids - snapshot_ids)
        missing_title = sorted(snapshot_ids - title_ids)
        raise DataQualityError(
            "title/snapshot item mismatch: "
            f"missing_snapshot={missing_snapshot[:3]} missing_title={missing_title[:3]}"
        )
    interaction_ids = {row.item_id for row in interactions}
    orphan_ids = sorted(interaction_ids - title_ids)
    if orphan_ids and orphan_policy == "reject":
        raise DataQualityError(f"orphan item in interactions: {orphan_ids[0]}")
    if orphan_ids and orphan_policy != "drop":
        raise DataQualityError(f"unsupported orphan policy: {orphan_policy}")
    if orphan_ids:
        interactions = [row for row in interactions if row.item_id not in set(orphan_ids)]
    items = [
        Item(item_id, titles[item_id], snapshots[item_id][0], snapshots[item_id][1])
        for item_id in sorted(title_ids, key=lambda value: (len(value), value))
    ]
    users = Counter(row.user_id for row in interactions)
    quality = {
        "pairs": pair_quality,
        "titles": title_quality,
        "likes_views": snapshot_quality,
        "orphan_interactions": len(orphan_ids),
        "unused_catalog_items": len(title_ids - {row.item_id for row in interactions}),
        "user_interactions": {"minimum": min(users.values()), "maximum": max(users.values())},
    }
    return interactions, items, quality


def inspect_official_files(raw_dir: str | Path) -> dict[str, Any]:
    root = Path(raw_dir)
    interactions, items, quality = load_official(root)
    paths = {
        "pairs": _required_path(root, PAIR_FILE),
        "titles": _required_path(root, TITLE_FILE),
        "likes_views": _required_path(root, SNAPSHOT_FILE),
    }
    return {
        "pairs": {
            **artifact_descriptor(paths["pairs"], rows=len(interactions)),
            "encoding": "ascii",
            "delimiter": ",",
            "header": ["user", "item", "timestamp"],
        },
        "titles": {
            **artifact_descriptor(paths["titles"], rows=len(items)),
            "encoding": "utf-8",
            "delimiter": ",",
            "header": ["item", "title"],
        },
        "likes_views": {
            **artifact_descriptor(paths["likes_views"], rows=len(items)),
            "encoding": "ascii",
            "delimiter": "\\t",
            "header": None,
        },
        "quality": quality,
    }

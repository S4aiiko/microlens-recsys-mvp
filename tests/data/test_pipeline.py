from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from recsys.data import (
    DataQualityError,
    JsonLinesCodec,
    build_official_dataset,
    inspect_official_files,
    popularity_aware_sample,
    uniform_sample,
)
from recsys.data.models import Interaction
from recsys.data.pipeline import split_interactions


def _write_raw(root: Path, *, future_item: str = "5") -> None:
    root.mkdir(parents=True, exist_ok=True)
    with (root / "MicroLens-50k_pairs.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["user", "item", "timestamp"])
        writer.writerows(
            [
                ["1", "1", 1000],
                ["1", "2", 2000],
                ["1", "3", 3000],
                ["1", future_item, 4000],
                ["2", "2", 1000],
                ["2", "3", 2000],
                ["2", "4", 3000],
                ["2", "5", 4000],
                ["3", "3", 1000],
                ["3", "4", 1000],
            ]
        )
    with (root / "MicroLens-50k_titles.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["item", "title"])
        writer.writerows([[str(item), f"  Title   {item}  "] for item in range(1, 6)])
    with (root / "MicroLens-50k_likes_and_views.txt").open(
        "w", newline="", encoding="ascii"
    ) as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerows([[str(item), item * 10, item * 100] for item in range(1, 6)])


def _config() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "mode": "full",
        "seed": 17,
        "source_urls": ["https://recsys.westlake.edu.cn/MicroLens-50K-Dataset/"],
        "split": {"min_train_interactions": 1, "low_interaction": "train_only"},
        "quality": {"duplicate_policy": "reject", "orphan_policy": "reject"},
        "negative_sampling": {
            "strategies": ["uniform", "popularity_aware"],
            "popularity_alpha": 0.75,
        },
        "time_decay": {"enabled": True, "half_life_seconds": 1},
    }


class OfficialPipelineTests(unittest.TestCase):
    def test_timestamp_ties_and_low_interaction_boundaries(self) -> None:
        rows = [
            Interaction("eval", "0", 1000),
            Interaction("eval", "1", 1000),
            Interaction("eval", "2", 2000),
            Interaction("eval", "3", 2000),
            Interaction("eval", "4", 3000),
            Interaction("eval", "5", 3000),
            Interaction("two-times", "1", 1000),
            Interaction("two-times", "2", 2000),
            Interaction("short-train", "1", 1000),
            Interaction("short-train", "2", 2000),
            Interaction("short-train", "3", 3000),
        ]
        train, validation, test, quality = split_interactions(rows, min_train_interactions=2)
        self.assertEqual([row.timestamp for row in validation], [2000, 2000])
        self.assertEqual([row.timestamp for row in test], [3000, 3000])
        self.assertEqual(
            [row.timestamp for row in train if row.user_id == "two-times"], [1000, 2000]
        )
        self.assertEqual(
            [row.timestamp for row in train if row.user_id == "short-train"],
            [1000, 2000, 3000],
        )
        self.assertEqual(quality["train_only_users"], 2)

    def test_inspection_and_deterministic_build(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw = root / "raw"
            _write_raw(raw)
            inspection = inspect_official_files(raw)
            self.assertEqual(inspection["pairs"]["rows"], 10)
            self.assertEqual(inspection["pairs"]["delimiter"], ",")
            self.assertEqual(inspection["likes_views"]["delimiter"], "\\t")

            codec = JsonLinesCodec()
            first = build_official_dataset(_config(), raw, root / "out-a", codec=codec)
            second = build_official_dataset(_config(), raw, root / "out-b", codec=codec)
            self.assertEqual(first.data_version, second.data_version)
            self.assertEqual(first.manifest, second.manifest)
            self.assertEqual(first.manifest_checksum, second.manifest_checksum)
            self.assertEqual(first.manifest["record_counts"]["train"], 6)
            self.assertEqual(first.manifest["record_counts"]["validation"], 2)
            self.assertEqual(first.manifest["record_counts"]["test"], 2)

            train = codec.read_rows(first.path / "train.jsonl")
            validation = codec.read_rows(first.path / "validation.jsonl")
            test = codec.read_rows(first.path / "test.jsonl")
            for user_id in {row["user_id"] for row in validation + test}:
                train_ts = [row["timestamp"] for row in train if row["user_id"] == user_id]
                val_ts = [row["timestamp"] for row in validation if row["user_id"] == user_id]
                test_ts = [row["timestamp"] for row in test if row["user_id"] == user_id]
                self.assertLess(max(train_ts), min(val_ts))
                self.assertLess(max(val_ts), min(test_ts))

            low_user = [row for row in train if row["user_id"] == "3"]
            self.assertEqual(len(low_user), 2)
            self.assertFalse(any(row["user_id"] == "3" for row in validation + test))

    def test_duplicate_and_orphan_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw = root / "raw"
            _write_raw(raw)
            pairs = raw / "MicroLens-50k_pairs.csv"
            with pairs.open("a", encoding="utf-8", newline="") as handle:
                csv.writer(handle).writerow(["1", "1", 1000])
            with self.assertRaisesRegex(DataQualityError, "duplicate interaction"):
                build_official_dataset(_config(), raw, root / "out", codec=JsonLinesCodec())

            _write_raw(raw)
            with pairs.open("a", encoding="utf-8", newline="") as handle:
                csv.writer(handle).writerow(["9", "unknown", 5000])
            with self.assertRaisesRegex(DataQualityError, "orphan item"):
                build_official_dataset(_config(), raw, root / "out-2", codec=JsonLinesCodec())

    def test_train_only_sampling_and_future_exclusion_leakage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw_a = root / "raw-a"
            raw_b = root / "raw-b"
            _write_raw(raw_a, future_item="5")
            _write_raw(raw_b, future_item="4")
            codec = JsonLinesCodec()
            a = build_official_dataset(_config(), raw_a, root / "a", codec=codec)
            b = build_official_dataset(_config(), raw_b, root / "b", codec=codec)

            pop_a = codec.read_rows(a.path / "train_popularity.jsonl")
            pop_b = codec.read_rows(b.path / "train_popularity.jsonl")
            self.assertEqual(pop_a, pop_b, "test-window changes must not change train statistics")

            train = codec.read_rows(a.path / "train.jsonl")
            all_items = [str(item) for item in range(1, 6)]
            train_seen = {row["item_id"] for row in train if row["user_id"] == "1"}
            # Item 5 appears only in user 1's test row and therefore remains a legal
            # train-time negative candidate; validation/test behavior is not an exclusion set.
            uniform = uniform_sample(all_items, train_seen, 5, seed=2)
            self.assertEqual(set(uniform), {"3", "4", "5"})
            self.assertEqual(uniform, uniform_sample(all_items, train_seen, 5, seed=2))
            popularity = {row["item_id"]: row["count"] for row in pop_a}
            first = popularity_aware_sample(all_items, train_seen, popularity, 3, seed=7)
            second = popularity_aware_sample(all_items, train_seen, popularity, 3, seed=7)
            self.assertEqual(first, second)
            self.assertTrue(set(first).isdisjoint(train_seen))

    def test_title_membership_and_snapshot_fields_are_not_features(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw = root / "raw"
            _write_raw(raw)
            codec = JsonLinesCodec()
            result = build_official_dataset(_config(), raw, root / "out", codec=codec)
            corpus = codec.read_rows(result.path / "title_corpus.jsonl")
            self.assertEqual(corpus[0]["normalized_title"], "Title 1")
            self.assertIn("item_split_membership", corpus[0])
            items = codec.read_rows(result.path / "items.jsonl")
            self.assertEqual(
                items[0]["metadata_status"], "complete_snapshot_unusable_as_of_feature"
            )
            self.assertEqual(
                result.manifest["output_schema"]["leakage_exclusions"],
                ["likes_snapshot", "views_snapshot"],
            )

    def test_no_decay_uses_raw_train_counts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw = root / "raw"
            _write_raw(raw)
            config = _config()
            config["time_decay"] = {"enabled": False, "half_life_seconds": None}
            codec = JsonLinesCodec()
            result = build_official_dataset(config, raw, root / "out", codec=codec)
            popularity = codec.read_rows(result.path / "train_popularity.jsonl")
            self.assertTrue(all(row["time_decayed_count"] == row["count"] for row in popularity))
            self.assertEqual(
                result.manifest["time_decay"],
                {
                    "enabled": False,
                    "reference_time_utc": None,
                    "half_life_seconds": None,
                    "statistics_split": "train",
                },
            )


if __name__ == "__main__":
    unittest.main()

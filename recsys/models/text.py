from __future__ import annotations

import hashlib
import math
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from recsys.data.common import canonical_json_bytes, sha256_bytes
from recsys.data.pipeline import normalize_title


def _ngrams(title: str, minimum: int, maximum: int) -> list[str]:
    normalized = normalize_title(title)
    wrapped = f"^{normalized}$"
    output: list[str] = []
    for size in range(minimum, maximum + 1):
        output.extend(wrapped[index : index + size] for index in range(len(wrapped) - size + 1))
    return output or ["<empty>"]


@dataclass(frozen=True, slots=True)
class EncodedTitle:
    token_ids: tuple[int, ...]
    weights: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class TitleHashEncoder:
    bucket_count: int
    ngram_min: int
    ngram_max: int
    document_count: int
    document_frequency: tuple[int, ...]
    fitted_item_ids: tuple[str, ...]

    @classmethod
    def fit(
        cls,
        train_titles: Mapping[str, str],
        *,
        bucket_count: int,
        ngram_min: int = 1,
        ngram_max: int = 2,
    ) -> TitleHashEncoder:
        if bucket_count < 8:
            raise ValueError("title bucket_count must be at least 8")
        if not 1 <= ngram_min <= ngram_max <= 4:
            raise ValueError("title ngram range must satisfy 1 <= min <= max <= 4")
        if not train_titles:
            raise ValueError("title encoder requires train-interaction item titles")
        frequencies = [0] * bucket_count
        for item_id in sorted(train_titles):
            buckets = {
                cls._bucket(token, bucket_count)
                for token in _ngrams(train_titles[item_id], ngram_min, ngram_max)
            }
            for bucket in buckets:
                frequencies[bucket] += 1
        return cls(
            bucket_count=bucket_count,
            ngram_min=ngram_min,
            ngram_max=ngram_max,
            document_count=len(train_titles),
            document_frequency=tuple(frequencies),
            fitted_item_ids=tuple(sorted(train_titles)),
        )

    @staticmethod
    def _bucket(token: str, bucket_count: int) -> int:
        return (
            int.from_bytes(hashlib.sha256(token.encode("utf-8")).digest()[:8], "big") % bucket_count
        )

    def transform(self, title: str) -> EncodedTitle:
        counts = Counter(
            self._bucket(token, self.bucket_count)
            for token in _ngrams(title, self.ngram_min, self.ngram_max)
        )
        token_ids: list[int] = []
        weights: list[float] = []
        for bucket, count in sorted(counts.items()):
            inverse_document_frequency = (
                math.log((1 + self.document_count) / (1 + self.document_frequency[bucket])) + 1.0
            )
            token_ids.append(bucket + 1)  # zero is reserved for EmbeddingBag padding
            weights.append(float(count) * inverse_document_frequency)
        scale = sum(weights) or 1.0
        return EncodedTitle(tuple(token_ids), tuple(weight / scale for weight in weights))

    def transform_many(self, titles: Mapping[str, str]) -> dict[str, EncodedTitle]:
        return {item_id: self.transform(titles[item_id]) for item_id in sorted(titles)}

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": "1.0",
            "kind": "train_only_character_hash_tfidf",
            "bucket_count": self.bucket_count,
            "ngram_min": self.ngram_min,
            "ngram_max": self.ngram_max,
            "document_count": self.document_count,
            "document_frequency": list(self.document_frequency),
            "fitted_item_ids": list(self.fitted_item_ids),
        }

    @property
    def checksum(self) -> str:
        return sha256_bytes(canonical_json_bytes(self.as_dict()))

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> TitleHashEncoder:
        expected = {
            "schema_version",
            "kind",
            "bucket_count",
            "ngram_min",
            "ngram_max",
            "document_count",
            "document_frequency",
            "fitted_item_ids",
        }
        if set(value) != expected:
            raise ValueError("title encoder has unknown or missing fields")
        if value.get("schema_version") != "1.0" or value.get("kind") != (
            "train_only_character_hash_tfidf"
        ):
            raise ValueError("unsupported title encoder")
        integer_fields = ("bucket_count", "ngram_min", "ngram_max", "document_count")
        if any(
            isinstance(value.get(field), bool) or not isinstance(value.get(field), int)
            for field in integer_fields
        ):
            raise ValueError("title encoder dimensions and counts must be integers")
        bucket_count = value["bucket_count"]
        ngram_min = value["ngram_min"]
        ngram_max = value["ngram_max"]
        document_count = value["document_count"]
        if bucket_count < 8 or not 1 <= ngram_min <= ngram_max <= 4 or document_count < 1:
            raise ValueError("title encoder dimensions or counts are outside valid bounds")
        frequencies = value["document_frequency"]
        if (
            not isinstance(frequencies, list)
            or len(frequencies) != bucket_count
            or any(
                isinstance(row, bool) or not isinstance(row, int) or not 0 <= row <= document_count
                for row in frequencies
            )
        ):
            raise ValueError("title encoder document frequencies are invalid")
        fitted_item_ids = value["fitted_item_ids"]
        if (
            not isinstance(fitted_item_ids, list)
            or len(fitted_item_ids) != document_count
            or any(not isinstance(row, str) or not 1 <= len(row) <= 255 for row in fitted_item_ids)
            or len(set(fitted_item_ids)) != len(fitted_item_ids)
            or fitted_item_ids != sorted(fitted_item_ids)
        ):
            raise ValueError("title encoder fitted item IDs are invalid")
        encoder = cls(
            bucket_count=bucket_count,
            ngram_min=ngram_min,
            ngram_max=ngram_max,
            document_count=document_count,
            document_frequency=tuple(frequencies),
            fitted_item_ids=tuple(fitted_item_ids),
        )
        return encoder


def sparse_cosine(left: EncodedTitle, right: EncodedTitle) -> float:
    left_map = dict(zip(left.token_ids, left.weights, strict=True))
    right_map = dict(zip(right.token_ids, right.weights, strict=True))
    dot = sum(weight * right_map.get(token, 0.0) for token, weight in left_map.items())
    left_norm = math.sqrt(sum(weight * weight for weight in left_map.values()))
    right_norm = math.sqrt(sum(weight * weight for weight in right_map.values()))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def merge_encoded_titles(rows: Iterable[EncodedTitle]) -> EncodedTitle:
    accumulated: Counter[int] = Counter()
    for row in rows:
        accumulated.update(dict(zip(row.token_ids, row.weights, strict=True)))
    if not accumulated:
        return EncodedTitle((0,), (0.0,))
    scale = sum(accumulated.values()) or 1.0
    return EncodedTitle(
        tuple(sorted(accumulated)),
        tuple(accumulated[token] / scale for token in sorted(accumulated)),
    )

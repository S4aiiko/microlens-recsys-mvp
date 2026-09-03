from __future__ import annotations

import random
import unittest
from collections import Counter
from unittest import mock

from recsys.models.sampling import (
    MAX_POPULARITY_REJECTIONS_PER_OUTPUT,
    TrainOnlyNegativeSampler,
    stable_seed,
)
from recsys.models.text import TitleHashEncoder


class TrainOnlyTextAndSamplingTests(unittest.TestCase):
    @staticmethod
    def _sampler(item_count: int = 20) -> TrainOnlyNegativeSampler:
        titles = {f"i{index}": f"title {index}" for index in range(item_count)}
        encoder = TitleHashEncoder.fit(titles, bucket_count=64)
        return TrainOnlyNegativeSampler(
            train_item_ids=tuple(titles),
            popularity={item_id: index + 1 for index, item_id in enumerate(titles)},
            title_features=encoder.transform_many(titles),
        )

    def test_validation_and_test_titles_never_change_fit_state(self) -> None:
        encoder = TitleHashEncoder.fit(
            {"train-a": "red cat", "train-b": "blue dog"}, bucket_count=32
        )
        checksum = encoder.checksum
        fitted = encoder.fitted_item_ids
        transformed = encoder.transform_many(
            {"train-a": "red cat", "validation-only": "secret val", "test-only": "secret test"}
        )
        self.assertEqual(encoder.checksum, checksum)
        self.assertEqual(encoder.fitted_item_ids, fitted)
        self.assertEqual(set(transformed), {"train-a", "validation-only", "test-only"})

    def test_leaky_fit_has_observable_different_contract(self) -> None:
        clean = TitleHashEncoder.fit({"train": "alpha"}, bucket_count=32)
        leaky = TitleHashEncoder.fit({"train": "alpha", "validation-only": "beta"}, bucket_count=32)
        self.assertNotEqual(clean.checksum, leaky.checksum)
        self.assertNotIn("validation-only", clean.fitted_item_ids)

    def test_all_negative_strategies_are_deterministic_and_train_only(self) -> None:
        encoder = TitleHashEncoder.fit(
            {"i1": "red cat", "i2": "red cats", "i3": "ocean", "i4": "forest"},
            bucket_count=32,
        )
        encoded = encoder.transform_many(
            {"i1": "red cat", "i2": "red cats", "i3": "ocean", "i4": "forest", "i5": "test"}
        )
        sampler = TrainOnlyNegativeSampler(
            train_item_ids=("i1", "i2", "i3", "i4"),
            popularity={"i2": 100.0, "i3": 2.0, "i4": 1.0},
            title_features=encoded,
        )
        for strategy in ("uniform", "popularity_aware", "train_only_hard"):
            first = sampler.sample(
                user_id="u1",
                positive_item_id="i1",
                seen_item_ids={"i1"},
                count=2,
                seed=7,
                strategy=strategy,
            )
            second = sampler.sample(
                user_id="u1",
                positive_item_id="i1",
                seen_item_ids={"i1"},
                count=2,
                seed=7,
                strategy=strategy,
            )
            self.assertEqual(first, second)
            self.assertTrue(set(first) <= {"i2", "i3", "i4"})
            self.assertNotIn("i5", first)
        hard = sampler.sample(
            user_id="u1",
            positive_item_id="i1",
            seen_item_ids={"i1"},
            count=1,
            seed=7,
            strategy="train_only_hard",
        )
        self.assertEqual(hard, ["i2"])

    def test_uniform_sampling_matches_previous_full_candidate_algorithm(self) -> None:
        sampler = self._sampler()
        seen = {"i1", "i4", "i12", "not-in-catalog"}
        expected_candidates = [
            item_id for item_id in sampler.train_item_ids if item_id != "i7" and item_id not in seen
        ]
        expected = random.Random(stable_seed(19, "u3", "i7", "uniform")).sample(
            expected_candidates, 6
        )
        actual = sampler.sample(
            user_id="u3",
            positive_item_id="i7",
            seen_item_ids=seen,
            count=6,
            seed=19,
            strategy="uniform",
        )
        self.assertEqual(actual, expected)

    def test_popularity_sampling_is_biased_without_leaking_seen_items(self) -> None:
        sampler = self._sampler(8)
        counts: Counter[str] = Counter()
        for seed in range(500):
            sampled = sampler.sample(
                user_id="u",
                positive_item_id="i0",
                seen_item_ids={"i0", "i6"},
                count=1,
                seed=seed,
                strategy="popularity_aware",
            )
            counts.update(sampled)
        self.assertNotIn("i0", counts)
        self.assertNotIn("i6", counts)
        self.assertGreater(counts["i7"], counts["i1"])

    def test_popularity_rejection_has_a_fixed_attempt_cap_and_one_pass_fallback(self) -> None:
        sampler = self._sampler(12)

        class AlwaysFirstRandom:
            calls = 0

            def __init__(self, _seed: int) -> None:
                pass

            def random(self) -> float:
                type(self).calls += 1
                return 0.0

        with mock.patch("recsys.models.sampling.random.Random", AlwaysFirstRandom):
            sampled = sampler.sample(
                user_id="u",
                positive_item_id="i0",
                seen_item_ids={"i0"},
                count=3,
                seed=1,
                strategy="popularity_aware",
            )
        self.assertEqual(len(sampled), 3)
        self.assertEqual(len(set(sampled)), 3)
        self.assertNotIn("i0", sampled)
        self.assertEqual(AlwaysFirstRandom.calls, MAX_POPULARITY_REJECTIONS_PER_OUTPUT)


if __name__ == "__main__":
    unittest.main()

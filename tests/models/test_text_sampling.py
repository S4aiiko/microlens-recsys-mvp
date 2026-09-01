from __future__ import annotations

import unittest

from recsys.models.sampling import TrainOnlyNegativeSampler
from recsys.models.text import TitleHashEncoder


class TrainOnlyTextAndSamplingTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()

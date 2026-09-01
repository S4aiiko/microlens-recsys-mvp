from __future__ import annotations

import math
import unittest

from recsys.models.metrics import (
    aggregate_ranking_metrics,
    binary_auc,
    hit_rate_at_k,
    ndcg_at_k,
    recall_at_k,
)


class RankingMetricTests(unittest.TestCase):
    def test_hand_calculated_ranking_metrics(self) -> None:
        ranked = ["a", "b", "c", "d"]
        relevant = {"b", "d"}
        self.assertEqual(recall_at_k(ranked, relevant, 2), 0.5)
        self.assertEqual(hit_rate_at_k(ranked, relevant, 1), 0.0)
        expected_ndcg = (1 / math.log2(3)) / (1 + 1 / math.log2(3))
        self.assertAlmostEqual(ndcg_at_k(ranked, relevant, 2), expected_ndcg)

    def test_aggregate_uses_exact_user_average(self) -> None:
        metrics = aggregate_ranking_metrics(
            {"u1": ["a", "b"], "u2": ["c", "d"]},
            {"u1": {"a"}, "u2": {"x"}},
            [1],
        )
        self.assertEqual(metrics, {"recall@1": 0.5, "ndcg@1": 0.5, "hit_rate@1": 0.5})

    def test_auc_is_tie_aware(self) -> None:
        self.assertEqual(binary_auc([0, 1], [0.1, 0.9]), 1.0)
        self.assertEqual(binary_auc([0, 1], [0.5, 0.5]), 0.5)
        self.assertAlmostEqual(binary_auc([1, 0, 1, 0], [0.9, 0.8, 0.7, 0.1]), 0.75)

    def test_invalid_auc_fails(self) -> None:
        with self.assertRaises(ValueError):
            binary_auc([1, 1], [0.2, 0.3])


if __name__ == "__main__":
    unittest.main()

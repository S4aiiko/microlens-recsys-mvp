from __future__ import annotations

import math
import unittest

import torch

from recsys.models.deepfm import DeepFMRanker
from recsys.models.early_stopping import EarlyStopper
from recsys.models.state import (
    decode_optimizer_state,
    decode_state_dict,
    encode_optimizer_state,
    encode_state_dict,
)
from recsys.models.two_tower import TwoTowerModel


def _two_tower(*, title_enabled: bool) -> TwoTowerModel:
    model = TwoTowerModel(
        user_count=1,
        item_count=2,
        title_bucket_count=4,
        item_title_tokens=torch.tensor([[1], [2]]),
        item_title_weights=torch.ones((2, 1)),
        user_title_tokens=torch.tensor([[1]]),
        user_title_weights=torch.ones((1, 1)),
        embedding_dim=2,
        title_dim=2,
        hidden_dims=[],
        output_dim=2,
        dropout=0.0,
        temperature=0.2,
        title_enabled=title_enabled,
    )
    with torch.no_grad():
        model.user_embedding.weight.zero_()
        model.item_embedding.weight.zero_()
        model.title_embedding.weight.zero_()
        model.title_embedding.weight[1] = torch.tensor([1.0, 0.0])
        model.title_embedding.weight[2] = torch.tensor([0.0, 1.0])
        item_linear = model.item_tower[0]
        item_linear.weight.zero_()
        item_linear.bias.zero_()
        item_linear.weight[:, 2:] = torch.eye(2)
    return model


class ModelArchitectureTests(unittest.TestCase):
    def test_title_feature_really_changes_item_representation(self) -> None:
        enabled = _two_tower(title_enabled=True)
        disabled = _two_tower(title_enabled=False)
        enabled_vectors = enabled.encode_items(torch.tensor([0, 1]))
        disabled_vectors = disabled.encode_items(torch.tensor([0, 1]))
        self.assertFalse(torch.equal(enabled_vectors[0], enabled_vectors[1]))
        self.assertTrue(torch.equal(disabled_vectors[0], disabled_vectors[1]))

    def test_dssm_loss_and_deepfm_forward_are_finite(self) -> None:
        dssm = _two_tower(title_enabled=True)
        loss = dssm.sampled_loss(
            torch.tensor([0]),
            torch.tensor([0]),
            torch.tensor([[1]]),
            torch.tensor([1.0]),
        )
        self.assertTrue(math.isfinite(float(loss.detach())))
        ranker = DeepFMRanker(
            user_count=1,
            item_count=2,
            source_count=1,
            dense_feature_count=6,
            embedding_dim=2,
            hidden_dims=[4],
            dropout=0.0,
        )
        output = ranker(
            torch.tensor([0, 0]),
            torch.tensor([0, 1]),
            torch.tensor([0, 0]),
            torch.zeros((2, 6)),
        )
        self.assertEqual(tuple(output.shape), (2,))
        self.assertTrue(torch.isfinite(output).all())

    def test_safe_state_roundtrip_never_uses_pickle(self) -> None:
        model = _two_tower(title_enabled=True)
        encoded = encode_state_dict(model.state_dict())
        decoded = decode_state_dict(encoded)
        for name, tensor in model.state_dict().items():
            self.assertTrue(torch.equal(tensor, decoded[name]))

    def test_adam_checkpoint_state_roundtrip(self) -> None:
        model = _two_tower(title_enabled=True)
        optimizer = torch.optim.Adam(model.parameters(), lr=0.01)
        model.pair_scores(torch.tensor([0]), torch.tensor([0])).sum().backward()
        optimizer.step()
        encoded = encode_optimizer_state(optimizer)
        restored = torch.optim.Adam(_two_tower(title_enabled=True).parameters(), lr=0.5)
        restored.load_state_dict(decode_optimizer_state(encoded))
        self.assertEqual(restored.param_groups[0]["lr"], 0.01)
        self.assertTrue(restored.state)

    def test_early_stopping_records_best_epoch_and_reason(self) -> None:
        stopper = EarlyStopper(patience=2, min_delta=0.01)
        self.assertFalse(stopper.observe(epoch=0, metric=0.5))
        self.assertFalse(stopper.observe(epoch=1, metric=0.505))
        self.assertTrue(stopper.observe(epoch=2, metric=0.49))
        self.assertEqual(stopper.best_epoch, 0)
        self.assertEqual(stopper.reason, "validation_metric_no_improvement_for_2_epochs")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from collections.abc import Sequence

import torch
from torch import nn


class DeepFMRanker(nn.Module):
    """DeepFM over user/item/source fields plus train-only dense candidate features."""

    def __init__(
        self,
        *,
        user_count: int,
        item_count: int,
        source_count: int,
        dense_feature_count: int,
        embedding_dim: int,
        hidden_dims: Sequence[int],
        dropout: float,
    ) -> None:
        super().__init__()
        self.user_embedding = nn.Embedding(user_count, embedding_dim)
        self.item_embedding = nn.Embedding(item_count, embedding_dim)
        self.source_embedding = nn.Embedding(source_count, embedding_dim)
        self.user_linear = nn.Embedding(user_count, 1)
        self.item_linear = nn.Embedding(item_count, 1)
        self.source_linear = nn.Embedding(source_count, 1)
        self.dense_linear = nn.Linear(dense_feature_count, 1)
        input_dim = 3 * embedding_dim + dense_feature_count
        layers: list[nn.Module] = []
        previous = input_dim
        for hidden in hidden_dims:
            layers.extend((nn.Linear(previous, hidden), nn.ReLU(), nn.Dropout(dropout)))
            previous = hidden
        layers.append(nn.Linear(previous, 1))
        self.deep = nn.Sequential(*layers)
        self.bias = nn.Parameter(torch.zeros(1))
        self.reset_parameters()

    def reset_parameters(self) -> None:
        for embedding in (
            self.user_embedding,
            self.item_embedding,
            self.source_embedding,
        ):
            nn.init.normal_(embedding.weight, std=0.02)
        for linear in (self.user_linear, self.item_linear, self.source_linear):
            nn.init.zeros_(linear.weight)
        nn.init.zeros_(self.dense_linear.weight)
        nn.init.zeros_(self.dense_linear.bias)
        for module in self.deep:
            if isinstance(module, nn.Linear):
                nn.init.xavier_uniform_(module.weight)
                nn.init.zeros_(module.bias)

    def forward(
        self,
        user_indices: torch.Tensor,
        item_indices: torch.Tensor,
        source_indices: torch.Tensor,
        dense_features: torch.Tensor,
    ) -> torch.Tensor:
        fields = torch.stack(
            (
                self.user_embedding(user_indices),
                self.item_embedding(item_indices),
                self.source_embedding(source_indices),
            ),
            dim=1,
        )
        summed = fields.sum(dim=1)
        fm = 0.5 * (summed.square() - fields.square().sum(dim=1)).sum(dim=1)
        linear = (
            self.user_linear(user_indices).squeeze(-1)
            + self.item_linear(item_indices).squeeze(-1)
            + self.source_linear(source_indices).squeeze(-1)
            + self.dense_linear(dense_features).squeeze(-1)
            + self.bias
        )
        deep_input = torch.cat((fields.flatten(start_dim=1), dense_features), dim=1)
        return linear + fm + self.deep(deep_input).squeeze(-1)

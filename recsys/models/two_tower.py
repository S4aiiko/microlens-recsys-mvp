from __future__ import annotations

from collections.abc import Sequence

import torch
from torch import nn
from torch.nn import functional as functional


def _mlp(
    input_dim: int, hidden_dims: Sequence[int], output_dim: int, dropout: float
) -> nn.Sequential:
    layers: list[nn.Module] = []
    previous = input_dim
    for hidden in hidden_dims:
        layers.extend((nn.Linear(previous, hidden), nn.ReLU(), nn.Dropout(dropout)))
        previous = hidden
    layers.append(nn.Linear(previous, output_dim))
    return nn.Sequential(*layers)


class TwoTowerModel(nn.Module):
    """Small CPU DSSM with train-history and title features in both towers."""

    def __init__(
        self,
        *,
        user_count: int,
        item_count: int,
        title_bucket_count: int,
        item_title_tokens: torch.Tensor,
        item_title_weights: torch.Tensor,
        user_title_tokens: torch.Tensor,
        user_title_weights: torch.Tensor,
        embedding_dim: int,
        title_dim: int,
        hidden_dims: Sequence[int],
        output_dim: int,
        dropout: float,
        temperature: float,
        title_enabled: bool,
    ) -> None:
        super().__init__()
        if user_count < 1 or item_count < 2:
            raise ValueError("two-tower requires users and at least two items")
        if temperature <= 0:
            raise ValueError("temperature must be positive")
        self.title_enabled = title_enabled
        self.temperature = float(temperature)
        self.user_embedding = nn.Embedding(user_count, embedding_dim)
        self.item_embedding = nn.Embedding(item_count, embedding_dim)
        self.title_embedding = nn.EmbeddingBag(
            title_bucket_count + 1,
            title_dim,
            mode="sum",
            padding_idx=0,
            include_last_offset=False,
        )
        self.user_tower = _mlp(embedding_dim + title_dim, hidden_dims, output_dim, dropout)
        self.item_tower = _mlp(embedding_dim + title_dim, hidden_dims, output_dim, dropout)
        self.register_buffer("item_title_tokens", item_title_tokens.long())
        self.register_buffer("item_title_weights", item_title_weights.float())
        self.register_buffer("user_title_tokens", user_title_tokens.long())
        self.register_buffer("user_title_weights", user_title_weights.float())
        self.reset_parameters()

    def reset_parameters(self) -> None:
        nn.init.normal_(self.user_embedding.weight, std=0.02)
        nn.init.normal_(self.item_embedding.weight, std=0.02)
        nn.init.normal_(self.title_embedding.weight, std=0.02)
        with torch.no_grad():
            self.title_embedding.weight[0].zero_()
        for module in (*self.user_tower, *self.item_tower):
            if isinstance(module, nn.Linear):
                nn.init.xavier_uniform_(module.weight)
                nn.init.zeros_(module.bias)

    def _title_bag(
        self,
        indices: torch.Tensor,
        token_table: torch.Tensor,
        weight_table: torch.Tensor,
    ) -> torch.Tensor:
        width = token_table.shape[1]
        tokens = token_table[indices].reshape(-1)
        weights = weight_table[indices].reshape(-1)
        offsets = torch.arange(indices.numel(), device=indices.device, dtype=torch.long) * width
        vectors = self.title_embedding(tokens, offsets, per_sample_weights=weights)
        return vectors if self.title_enabled else torch.zeros_like(vectors)

    def encode_users(self, user_indices: torch.Tensor) -> torch.Tensor:
        title = self._title_bag(user_indices, self.user_title_tokens, self.user_title_weights)
        encoded = self.user_tower(torch.cat((self.user_embedding(user_indices), title), dim=-1))
        return functional.normalize(encoded, dim=-1)

    def encode_items(self, item_indices: torch.Tensor) -> torch.Tensor:
        title = self._title_bag(item_indices, self.item_title_tokens, self.item_title_weights)
        encoded = self.item_tower(torch.cat((self.item_embedding(item_indices), title), dim=-1))
        return functional.normalize(encoded, dim=-1)

    def pair_scores(self, user_indices: torch.Tensor, item_indices: torch.Tensor) -> torch.Tensor:
        return (self.encode_users(user_indices) * self.encode_items(item_indices)).sum(dim=-1)

    def sampled_loss(
        self,
        user_indices: torch.Tensor,
        positive_indices: torch.Tensor,
        negative_indices: torch.Tensor,
        sample_weights: torch.Tensor,
    ) -> torch.Tensor:
        user = self.encode_users(user_indices)
        positive = self.encode_items(positive_indices)
        negatives = self.encode_items(negative_indices.reshape(-1)).reshape(
            negative_indices.shape[0], negative_indices.shape[1], -1
        )
        positive_score = (user * positive).sum(dim=-1, keepdim=True)
        negative_scores = torch.einsum("bd,bnd->bn", user, negatives)
        logits = torch.cat((positive_score, negative_scores), dim=1) / self.temperature
        targets = torch.zeros(len(user_indices), dtype=torch.long, device=user_indices.device)
        losses = functional.cross_entropy(logits, targets, reduction="none")
        return (losses * sample_weights).sum() / sample_weights.sum().clamp_min(1e-8)

    @torch.no_grad()
    def all_item_embeddings(self) -> torch.Tensor:
        indices = torch.arange(
            self.item_embedding.num_embeddings, device=self.item_embedding.weight.device
        )
        return self.encode_items(indices)

    @torch.no_grad()
    def score_catalog(
        self, user_indices: torch.Tensor, item_embeddings: torch.Tensor
    ) -> torch.Tensor:
        return self.encode_users(user_indices) @ item_embeddings.T

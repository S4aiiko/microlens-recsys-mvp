from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import torch

from recsys.models.bundle import ModelBundle, load_bundle


@dataclass(frozen=True, slots=True)
class LoadedRecommendationModel:
    bundle: ModelBundle

    @torch.no_grad()
    def recall(self, user_id: str, *, top_n: int) -> list[tuple[str, float]]:
        if top_n < 1:
            raise ValueError("top_n must be positive")
        user_index = self.bundle.user_to_index.get(user_id)
        if user_index is None:
            return []
        self.bundle.dssm.eval()
        items = self.bundle.dssm.all_item_embeddings()
        scores = self.bundle.dssm.score_catalog(
            torch.tensor([user_index], dtype=torch.long), items
        )[0].tolist()
        return sorted(
            zip(self.bundle.item_ids, (float(value) for value in scores), strict=True),
            key=lambda row: (-row[1], row[0]),
        )[:top_n]


def load_recommendation_model(
    path: str | Path, expected_manifest_checksum: str
) -> LoadedRecommendationModel:
    return LoadedRecommendationModel(load_bundle(path, expected_manifest_checksum))

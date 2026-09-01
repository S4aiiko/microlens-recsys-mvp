from __future__ import annotations

import heapq
from collections.abc import Callable, Mapping

import torch

from .baselines import relevant_by_user
from .data import ModelData
from .deepfm import DeepFMRanker
from .features import FeatureIndex
from .metrics import aggregate_ranking_metrics
from .two_tower import TwoTowerModel


@torch.no_grad()
def dssm_rankings(
    model: TwoTowerModel,
    data: ModelData,
    features: FeatureIndex,
    *,
    users: set[str],
    top_n: int | None = None,
    batch_size: int = 128,
    allowed_items: set[str] | None = None,
    progress: Callable[[], None] = lambda: None,
) -> tuple[dict[str, list[str]], dict[str, dict[str, float]]]:
    model.eval()
    item_embeddings = model.all_item_embeddings()
    rankings: dict[str, list[str]] = {}
    score_maps: dict[str, dict[str, float]] = {}
    ordered_users = sorted(users)
    for start in range(0, len(ordered_users), batch_size):
        progress()
        batch_users = ordered_users[start : start + batch_size]
        user_indices = torch.tensor(
            [features.user_to_index[user_id] for user_id in batch_users], dtype=torch.long
        )
        scores = model.score_catalog(user_indices, item_embeddings).cpu()
        for row_index, user_id in enumerate(batch_users):
            seen = set(data.user_train_items[user_id])
            scored = (
                (data.item_ids[item_index], float(scores[row_index, item_index]))
                for item_index in range(len(data.item_ids))
                if data.item_ids[item_index] not in seen
                and (allowed_items is None or data.item_ids[item_index] in allowed_items)
            )
            ranked = (
                heapq.nsmallest(top_n, scored, key=lambda row: (-row[1], row[0]))
                if top_n is not None
                else sorted(scored, key=lambda row: (-row[1], row[0]))
            )
            rankings[user_id] = [item_id for item_id, _score in ranked]
            score_maps[user_id] = dict(ranked)
    return rankings, score_maps


@torch.no_grad()
def deepfm_rankings(
    model: DeepFMRanker,
    candidates: Mapping[str, list[str]],
    recall_scores: Mapping[str, Mapping[str, float]],
    features: FeatureIndex,
    progress: Callable[[], None] = lambda: None,
) -> dict[str, list[str]]:
    model.eval()
    output: dict[str, list[str]] = {}
    for user_offset, user_id in enumerate(sorted(candidates)):
        if user_offset % 64 == 0:
            progress()
        items = candidates[user_id]
        if not items:
            output[user_id] = []
            continue
        user_indices = torch.full((len(items),), features.user_to_index[user_id], dtype=torch.long)
        item_indices = torch.tensor(
            [features.item_to_index[item_id] for item_id in items], dtype=torch.long
        )
        sources = torch.zeros(len(items), dtype=torch.long)
        dense = torch.tensor(
            [
                features.dense(
                    user_id=user_id,
                    item_id=item_id,
                    recall_score=float(recall_scores[user_id][item_id]),
                )
                for item_id in items
            ],
            dtype=torch.float32,
        )
        scores = model(user_indices, item_indices, sources, dense).cpu().tolist()
        output[user_id] = [
            item_id
            for item_id, _score in sorted(
                zip(items, scores, strict=True), key=lambda row: (-row[1], row[0])
            )
        ]
    return output


def evaluate_dssm(
    model: TwoTowerModel,
    data: ModelData,
    features: FeatureIndex,
    *,
    split: str,
    k_values: list[int],
    top_n: int | None = None,
    progress: Callable[[], None] = lambda: None,
) -> tuple[dict[str, float], dict[str, list[str]], dict[str, dict[str, float]]]:
    relevant = relevant_by_user(getattr(data, split))
    rankings, scores = dssm_rankings(
        model, data, features, users=set(relevant), top_n=top_n, progress=progress
    )
    return aggregate_ranking_metrics(rankings, relevant, k_values), rankings, scores


def evaluate_two_stage(
    dssm: TwoTowerModel,
    deepfm: DeepFMRanker,
    data: ModelData,
    features: FeatureIndex,
    *,
    split: str,
    k_values: list[int],
    candidate_top_n: int,
    progress: Callable[[], None] = lambda: None,
) -> tuple[dict[str, float], dict[str, list[str]], dict[str, dict[str, float]]]:
    relevant = relevant_by_user(getattr(data, split))
    candidates, recall_scores = dssm_rankings(
        dssm,
        data,
        features,
        users=set(relevant),
        top_n=candidate_top_n,
        progress=progress,
    )
    rankings = deepfm_rankings(deepfm, candidates, recall_scores, features, progress=progress)
    return aggregate_ranking_metrics(rankings, relevant, k_values), rankings, recall_scores

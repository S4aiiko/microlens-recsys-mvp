from __future__ import annotations

from collections.abc import Mapping

from .baselines import relevant_by_user
from .data import ModelData


def build_badcases(
    data: ModelData,
    *,
    split: str,
    dssm_candidates: Mapping[str, list[str]],
    two_stage_rankings: Mapping[str, list[str]],
    maximum_rows: int = 200,
) -> list[dict[str, object]]:
    """Return deterministic misses with enough context for manual diagnosis."""

    if split not in {"validation", "test"}:
        raise ValueError("badcase split must be validation or test")
    relevant = relevant_by_user(getattr(data, split))
    rows: list[dict[str, object]] = []
    for user_id in sorted(relevant):
        candidates = dssm_candidates.get(user_id, [])
        ranking = two_stage_rankings.get(user_id, [])
        history_length = len(data.user_train_items[user_id])
        top_popularity = max(
            (float(data.train_popularity.get(item_id, 0.0)) for item_id in ranking[:10]),
            default=0.0,
        )
        for item_id in sorted(relevant[user_id]):
            if item_id in ranking:
                continue
            found = item_id in candidates
            title_length = len(data.titles[item_id].strip())
            if not found:
                category = "recall_miss"
            elif title_length < 5:
                category = "title_information_sparse"
            elif history_length <= 2:
                category = "short_history_rerank_miss"
            elif history_length >= 10:
                category = "long_history_rerank_miss"
            elif top_popularity > float(data.train_popularity.get(item_id, 0.0)):
                category = "popularity_bias_rerank_miss"
            else:
                category = "rerank_miss"
            rows.append(
                {
                    "user_id": user_id,
                    "item_id": item_id,
                    "category": category,
                    "dssm_candidate_found": found,
                    "two_stage_rank": ranking.index(item_id) + 1 if item_id in ranking else None,
                    "history_length": history_length,
                    "title_length": title_length,
                }
            )
            if len(rows) >= maximum_rows:
                return rows
        seen_titles: set[str] = set()
        for rank, item_id in enumerate(ranking[:20], start=1):
            title = data.titles[item_id].strip().casefold()
            if title and title in seen_titles:
                rows.append(
                    {
                        "user_id": user_id,
                        "item_id": item_id,
                        "category": "duplicate_title_diversity",
                        "dssm_candidate_found": item_id in candidates,
                        "two_stage_rank": rank,
                        "history_length": history_length,
                        "title_length": len(title),
                    }
                )
                if len(rows) >= maximum_rows:
                    return rows
            seen_titles.add(title)
    return rows

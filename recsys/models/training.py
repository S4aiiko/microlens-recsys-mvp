from __future__ import annotations

import math
import random
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import torch
from torch.nn import functional as functional

from .data import ModelData
from .deepfm import DeepFMRanker
from .early_stopping import EarlyStopper
from .errors import TrainingCancelled
from .evaluation import dssm_rankings, evaluate_dssm, evaluate_two_stage
from .features import DENSE_FEATURE_NAMES, FeatureIndex, padded_title_tables
from .sampling import TrainOnlyNegativeSampler
from .state import (
    decode_optimizer_state,
    decode_state_dict,
    encode_optimizer_state,
    encode_state_dict,
)
from .two_tower import TwoTowerModel


@dataclass(frozen=True, slots=True)
class StageResult:
    best_epoch: int
    stop_reason: str
    best_validation_metric: float
    history: tuple[dict[str, float | int], ...]
    resumed_from_epoch: int | None


def set_determinism(seed: int) -> None:
    random.seed(seed)
    torch.manual_seed(seed)
    torch.use_deterministic_algorithms(True)
    torch.set_num_threads(max(1, min(4, torch.get_num_threads())))


def _model_title_tables(
    data: ModelData, *, maximum_tokens: int
) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
    item_tokens, item_weights = padded_title_tables(
        [data.encoded_titles[item_id] for item_id in data.item_ids],
        maximum_tokens=maximum_tokens,
    )
    user_tokens, user_weights = padded_title_tables(
        [data.user_history_titles[user_id] for user_id in data.user_ids],
        maximum_tokens=maximum_tokens,
    )
    return item_tokens, item_weights, user_tokens, user_weights


def build_dssm(data: ModelData, config: Mapping[str, Any]) -> TwoTowerModel:
    title = config["title"]
    stage = config["dssm"]
    item_tokens, item_weights, user_tokens, user_weights = _model_title_tables(
        data, maximum_tokens=int(title.get("maximum_tokens", 32))
    )
    return TwoTowerModel(
        user_count=len(data.user_ids),
        item_count=len(data.item_ids),
        title_bucket_count=data.title_encoder.bucket_count,
        item_title_tokens=item_tokens,
        item_title_weights=item_weights,
        user_title_tokens=user_tokens,
        user_title_weights=user_weights,
        embedding_dim=int(stage["embedding_dim"]),
        title_dim=int(title["embedding_dim"]),
        hidden_dims=[int(value) for value in stage.get("hidden_dims", [])],
        output_dim=int(stage["output_dim"]),
        dropout=float(stage.get("dropout", 0.0)),
        temperature=float(stage.get("temperature", 0.1)),
        title_enabled=bool(title["enabled"]),
    )


def build_deepfm(data: ModelData, config: Mapping[str, Any]) -> DeepFMRanker:
    stage = config["deepfm"]
    return DeepFMRanker(
        user_count=len(data.user_ids),
        item_count=len(data.item_ids),
        source_count=1,
        dense_feature_count=len(DENSE_FEATURE_NAMES),
        embedding_dim=int(stage["embedding_dim"]),
        hidden_dims=[int(value) for value in stage.get("hidden_dims", [])],
        dropout=float(stage.get("dropout", 0.0)),
    )


def _time_weight(timestamp: int, reference: int, decay: Mapping[str, Any]) -> float:
    if not decay["enabled"]:
        return 1.0
    age_seconds = max(0.0, (reference - timestamp) / 1000)
    return math.exp(-math.log(2) * age_seconds / int(decay["half_life_seconds"]))


def train_dssm(
    model: TwoTowerModel,
    data: ModelData,
    features: FeatureIndex,
    config: Mapping[str, Any],
    *,
    checkpoint: Callable[[str, dict[str, Any]], None],
    heartbeat: Callable[[], None] = lambda: None,
    cancellation_requested: Callable[[], bool] = lambda: False,
    resume: Mapping[str, Any] | None = None,
) -> StageResult:
    stage = config["dssm"]

    def pulse() -> None:
        if cancellation_requested():
            raise TrainingCancelled("training cancelled during DSSM")
        heartbeat()

    seed = int(config["seed"])
    sampler = TrainOnlyNegativeSampler(
        train_item_ids=tuple(sorted({str(row["item_id"]) for row in data.train})),
        popularity=data.train_popularity,
        title_features=data.encoded_titles,
        alpha=float(stage.get("popularity_alpha", 0.75)),
    )
    optimizer = torch.optim.Adam(model.parameters(), lr=float(stage["learning_rate"]))
    start_epoch = 0
    resumed_from: int | None = None
    stopper = EarlyStopper(
        patience=int(stage["patience"]), min_delta=float(stage.get("min_delta", 0.0))
    )
    if resume is not None:
        model.load_state_dict(decode_state_dict(resume["model_state"]), strict=True)
        optimizer.load_state_dict(decode_optimizer_state(resume["optimizer_state"]))
        resumed_from = int(resume["epoch"])
        start_epoch = resumed_from + 1
        stopper.best_epoch = int(resume.get("best_epoch", resumed_from))
        stopper.best_metric = float(resume.get("best_metric", float("-inf")))
        stopper.epochs_without_improvement = int(resume.get("epochs_without_improvement", 0))
    train_rows = list(data.train)
    reference_timestamp = max(int(row["timestamp"]) for row in train_rows)
    decay = stage["time_decay"]
    k_values = [int(value) for value in config["evaluation"]["k"]]
    metric_name = f"ndcg@{max(k_values)}"
    best_state = (
        decode_state_dict(resume["best_model_state"])
        if resume is not None
        else {name: tensor.detach().clone() for name, tensor in model.state_dict().items()}
    )
    history: list[dict[str, float | int]] = (
        [dict(row) for row in resume.get("history", [])] if resume is not None else []
    )
    for epoch in range(start_epoch, int(stage["epochs"])):
        pulse()
        order = list(range(len(train_rows)))
        random.Random(seed + epoch).shuffle(order)
        model.train()
        losses: list[float] = []
        batch_size = int(stage["batch_size"])
        for batch_start in range(0, len(order), batch_size):
            pulse()
            selected = [
                train_rows[index] for index in order[batch_start : batch_start + batch_size]
            ]
            users: list[int] = []
            positives: list[int] = []
            negatives: list[list[int]] = []
            weights: list[float] = []
            for row in selected:
                user_id = str(row["user_id"])
                positive = str(row["item_id"])
                sampled = sampler.sample(
                    user_id=user_id,
                    positive_item_id=positive,
                    seen_item_ids=set(data.user_train_items[user_id]),
                    count=int(stage["negatives_per_positive"]),
                    seed=seed + epoch,
                    strategy=str(stage["negative_sampling"]),
                )
                if len(sampled) != int(stage["negatives_per_positive"]):
                    continue
                users.append(features.user_to_index[user_id])
                positives.append(features.item_to_index[positive])
                negatives.append([features.item_to_index[item_id] for item_id in sampled])
                weights.append(_time_weight(int(row["timestamp"]), reference_timestamp, decay))
            if not users:
                continue
            optimizer.zero_grad(set_to_none=True)
            loss = model.sampled_loss(
                torch.tensor(users, dtype=torch.long),
                torch.tensor(positives, dtype=torch.long),
                torch.tensor(negatives, dtype=torch.long),
                torch.tensor(weights, dtype=torch.float32),
            )
            loss.backward()
            optimizer.step()
            losses.append(float(loss.detach()))
        if data.purpose == "systems_only":
            metric = -(sum(losses) / len(losses) if losses else 0.0)
        else:
            validation_metrics, _rankings, _scores = evaluate_dssm(
                model,
                data,
                features,
                split="validation",
                k_values=k_values,
                top_n=max(k_values),
                progress=pulse,
            )
            metric = validation_metrics[metric_name]
        record = {
            "epoch": epoch,
            "train_loss": sum(losses) / len(losses) if losses else 0.0,
            "validation_metric": metric,
        }
        history.append(record)
        improved = metric > stopper.best_metric + stopper.min_delta
        should_stop = stopper.observe(epoch=epoch, metric=metric)
        if improved:
            best_state = {
                name: tensor.detach().clone() for name, tensor in model.state_dict().items()
            }
        checkpoint(
            "dssm",
            {
                "schema_version": "1.0",
                "stage": "dssm",
                "epoch": epoch,
                "best_epoch": stopper.best_epoch,
                "best_metric": stopper.best_metric,
                "epochs_without_improvement": stopper.epochs_without_improvement,
                "history": history,
                "model_state": encode_state_dict(model.state_dict()),
                "best_model_state": encode_state_dict(best_state),
                "optimizer_state": encode_optimizer_state(optimizer),
            },
        )
        if should_stop:
            break
    model.load_state_dict(best_state, strict=True)
    return StageResult(
        best_epoch=stopper.best_epoch,
        stop_reason=stopper.reason,
        best_validation_metric=stopper.best_metric,
        history=tuple(history),
        resumed_from_epoch=resumed_from,
    )


def _deepfm_examples(
    dssm: TwoTowerModel,
    data: ModelData,
    features: FeatureIndex,
    config: Mapping[str, Any],
    *,
    heartbeat: Callable[[], None] = lambda: None,
) -> list[tuple[int, int, int, tuple[float, ...], float, float]]:
    stage = config["deepfm"]
    users = set(data.user_ids)
    train_item_ids = {str(row["item_id"]) for row in data.train}
    candidates, recall_scores = dssm_rankings(
        dssm,
        data,
        features,
        users=users,
        top_n=int(config["dssm"]["candidate_top_n"]),
        allowed_items=train_item_ids,
        progress=heartbeat,
    )
    dssm.eval()
    examples: list[tuple[int, int, int, tuple[float, ...], float, float]] = []
    reference = max(int(row["timestamp"]) for row in data.train)
    decay = config["dssm"]["time_decay"]
    with torch.no_grad():
        for row in data.train:
            user_id = str(row["user_id"])
            item_id = str(row["item_id"])
            user_index = features.user_to_index[user_id]
            item_index = features.item_to_index[item_id]
            positive_score = float(
                dssm.pair_scores(torch.tensor([user_index]), torch.tensor([item_index])).item()
            )
            weight = _time_weight(int(row["timestamp"]), reference, decay)
            examples.append(
                (
                    user_index,
                    item_index,
                    0,
                    features.dense(
                        user_id=user_id,
                        item_id=item_id,
                        recall_score=positive_score,
                        time_decay_weight=weight,
                    ),
                    1.0,
                    weight,
                )
            )
            for negative_id in candidates[user_id][: int(stage["negatives_per_positive"])]:
                examples.append(
                    (
                        user_index,
                        features.item_to_index[negative_id],
                        0,
                        features.dense(
                            user_id=user_id,
                            item_id=negative_id,
                            recall_score=recall_scores[user_id][negative_id],
                            time_decay_weight=weight,
                        ),
                        0.0,
                        weight,
                    )
                )
        for signal in data.event_training_signals:
            if signal.get("split") != "train" or signal.get("label") != 0:
                continue
            user_id = str(signal["user_id"])
            item_id = str(signal["item_id"])
            if (
                user_id not in features.user_to_index
                or item_id not in features.item_to_index
                or item_id not in train_item_ids
            ):
                continue
            score = float(
                dssm.pair_scores(
                    torch.tensor([features.user_to_index[user_id]]),
                    torch.tensor([features.item_to_index[item_id]]),
                ).item()
            )
            examples.append(
                (
                    features.user_to_index[user_id],
                    features.item_to_index[item_id],
                    0,
                    features.dense(user_id=user_id, item_id=item_id, recall_score=score),
                    0.0,
                    float(signal["sample_weight"]),
                )
            )
    return examples


def train_deepfm(
    model: DeepFMRanker,
    dssm: TwoTowerModel,
    data: ModelData,
    features: FeatureIndex,
    config: Mapping[str, Any],
    *,
    checkpoint: Callable[[str, dict[str, Any]], None],
    heartbeat: Callable[[], None] = lambda: None,
    cancellation_requested: Callable[[], bool] = lambda: False,
    resume: Mapping[str, Any] | None = None,
) -> StageResult:
    stage = config["deepfm"]

    def pulse() -> None:
        if cancellation_requested():
            raise TrainingCancelled("training cancelled during DeepFM")
        heartbeat()

    seed = int(config["seed"])
    optimizer = torch.optim.Adam(model.parameters(), lr=float(stage["learning_rate"]))
    stopper = EarlyStopper(
        patience=int(stage["patience"]), min_delta=float(stage.get("min_delta", 0.0))
    )
    start_epoch = 0
    resumed_from: int | None = None
    if resume is not None:
        model.load_state_dict(decode_state_dict(resume["model_state"]), strict=True)
        optimizer.load_state_dict(decode_optimizer_state(resume["optimizer_state"]))
        resumed_from = int(resume["epoch"])
        start_epoch = resumed_from + 1
        stopper.best_epoch = int(resume.get("best_epoch", resumed_from))
        stopper.best_metric = float(resume.get("best_metric", float("-inf")))
        stopper.epochs_without_improvement = int(resume.get("epochs_without_improvement", 0))
    examples = _deepfm_examples(dssm, data, features, config, heartbeat=pulse)
    if not examples:
        raise ValueError("DeepFM received no DSSM candidate examples")
    k_values = [int(value) for value in config["evaluation"]["k"]]
    metric_name = f"ndcg@{max(k_values)}"
    best_state = (
        decode_state_dict(resume["best_model_state"])
        if resume is not None
        else {name: tensor.detach().clone() for name, tensor in model.state_dict().items()}
    )
    history: list[dict[str, float | int]] = (
        [dict(row) for row in resume.get("history", [])] if resume is not None else []
    )
    for epoch in range(start_epoch, int(stage["epochs"])):
        pulse()
        order = list(range(len(examples)))
        random.Random(seed + 10_000 + epoch).shuffle(order)
        losses: list[float] = []
        model.train()
        for start in range(0, len(order), int(stage["batch_size"])):
            pulse()
            rows = [examples[index] for index in order[start : start + int(stage["batch_size"])]]
            users = torch.tensor([row[0] for row in rows], dtype=torch.long)
            items = torch.tensor([row[1] for row in rows], dtype=torch.long)
            sources = torch.tensor([row[2] for row in rows], dtype=torch.long)
            dense = torch.tensor([row[3] for row in rows], dtype=torch.float32)
            labels = torch.tensor([row[4] for row in rows], dtype=torch.float32)
            weights = torch.tensor([row[5] for row in rows], dtype=torch.float32)
            optimizer.zero_grad(set_to_none=True)
            logits = model(users, items, sources, dense)
            raw_loss = functional.binary_cross_entropy_with_logits(logits, labels, reduction="none")
            loss = (raw_loss * weights).sum() / weights.sum().clamp_min(1e-8)
            loss.backward()
            optimizer.step()
            losses.append(float(loss.detach()))
        if data.purpose == "systems_only":
            metric = -(sum(losses) / len(losses))
        else:
            validation_metrics, _rankings, _recall = evaluate_two_stage(
                dssm,
                model,
                data,
                features,
                split="validation",
                k_values=k_values,
                candidate_top_n=int(config["dssm"]["candidate_top_n"]),
                progress=pulse,
            )
            metric = validation_metrics[metric_name]
        history.append(
            {
                "epoch": epoch,
                "train_loss": sum(losses) / len(losses),
                "validation_metric": metric,
            }
        )
        improved = metric > stopper.best_metric + stopper.min_delta
        should_stop = stopper.observe(epoch=epoch, metric=metric)
        if improved:
            best_state = {
                name: tensor.detach().clone() for name, tensor in model.state_dict().items()
            }
        checkpoint(
            "deepfm",
            {
                "schema_version": "1.0",
                "stage": "deepfm",
                "epoch": epoch,
                "best_epoch": stopper.best_epoch,
                "best_metric": stopper.best_metric,
                "epochs_without_improvement": stopper.epochs_without_improvement,
                "history": history,
                "model_state": encode_state_dict(model.state_dict()),
                "best_model_state": encode_state_dict(best_state),
                "optimizer_state": encode_optimizer_state(optimizer),
            },
        )
        if should_stop:
            break
    model.load_state_dict(best_state, strict=True)
    return StageResult(
        best_epoch=stopper.best_epoch,
        stop_reason=stopper.reason,
        best_validation_metric=stopper.best_metric,
        history=tuple(history),
        resumed_from_epoch=resumed_from,
    )

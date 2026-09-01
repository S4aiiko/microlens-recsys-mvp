from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class EarlyStopper:
    patience: int
    min_delta: float
    best_metric: float = float("-inf")
    best_epoch: int = -1
    epochs_without_improvement: int = 0

    def observe(self, *, epoch: int, metric: float) -> bool:
        if metric > self.best_metric + self.min_delta:
            self.best_metric = metric
            self.best_epoch = epoch
            self.epochs_without_improvement = 0
            return False
        self.epochs_without_improvement += 1
        return self.epochs_without_improvement >= self.patience

    @property
    def reason(self) -> str:
        return (
            f"validation_metric_no_improvement_for_{self.patience}_epochs"
            if self.epochs_without_improvement >= self.patience
            else "max_epochs_reached"
        )

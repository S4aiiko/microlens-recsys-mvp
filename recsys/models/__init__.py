"""CPU-first two-stage recommendation models."""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .bundle import ModelBundle

__all__ = ["ModelBundle", "load_bundle", "train_model", "worker_training_handler"]


def __getattr__(name: str) -> Any:
    if name in {"ModelBundle", "load_bundle"}:
        from .bundle import ModelBundle, load_bundle

        return {"ModelBundle": ModelBundle, "load_bundle": load_bundle}[name]
    if name in {"train_model", "worker_training_handler"}:
        from .entrypoint import train_model, worker_training_handler

        return {
            "train_model": train_model,
            "worker_training_handler": worker_training_handler,
        }[name]
    raise AttributeError(name)

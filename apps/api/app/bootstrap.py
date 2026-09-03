from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BootstrapPlan:
    migrate_and_seed: bool
    restore_active_model: bool


def bootstrap_plan(environment: Mapping[str, str] | None = None) -> BootstrapPlan:
    values = os.environ if environment is None else environment
    mode = values.get("API_BOOTSTRAP_MODE", "automatic").strip().lower()
    if mode == "automatic":
        return BootstrapPlan(migrate_and_seed=True, restore_active_model=True)
    if mode != "external":
        raise ValueError("API_BOOTSTRAP_MODE must be automatic or external")
    restore = values.get("API_RESTORE_ACTIVE_MODEL", "false").strip().lower()
    if restore not in {"true", "false"}:
        raise ValueError("API_RESTORE_ACTIVE_MODEL must be true or false")
    return BootstrapPlan(migrate_and_seed=False, restore_active_model=restore == "true")

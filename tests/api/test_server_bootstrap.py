from __future__ import annotations

import pytest

from apps.api.app.bootstrap import BootstrapPlan, bootstrap_plan


def test_default_bootstrap_behavior_is_preserved() -> None:
    assert bootstrap_plan({}) == BootstrapPlan(migrate_and_seed=True, restore_active_model=True)


def test_external_bootstrap_can_bind_health_before_schema_and_restore_later() -> None:
    assert bootstrap_plan({"API_BOOTSTRAP_MODE": "external"}) == BootstrapPlan(
        migrate_and_seed=False,
        restore_active_model=False,
    )
    assert bootstrap_plan(
        {"API_BOOTSTRAP_MODE": "external", "API_RESTORE_ACTIVE_MODEL": "true"}
    ) == BootstrapPlan(migrate_and_seed=False, restore_active_model=True)


@pytest.mark.parametrize(
    "environment",
    [
        {"API_BOOTSTRAP_MODE": "unknown"},
        {"API_BOOTSTRAP_MODE": "external", "API_RESTORE_ACTIVE_MODEL": "1"},
    ],
)
def test_bootstrap_policy_rejects_ambiguous_values(environment: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        bootstrap_plan(environment)

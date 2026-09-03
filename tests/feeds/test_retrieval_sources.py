from __future__ import annotations

from unittest import mock

import pytest

from apps.api.app.feeds.retrieval import CatalogItem, retrieve_candidates


def test_default_source_set_matches_explicit_all_sources() -> None:
    catalog = [
        CatalogItem("1", "alpha beta", None, 1, 10),
        CatalogItem("2", "gamma", None, 2, 20),
    ]
    arguments = {
        "feed_type": "personalized",
        "catalog": catalog,
        "bundle": None,
        "source_user_id": None,
        "profile_title_preferences": {"alpha": {"score": 3}},
        "recent_item_ids": (),
        "item_item_index": None,
        "seed": 7,
        "top_n": 2,
    }
    default = retrieve_candidates(**arguments)
    explicit = retrieve_candidates(
        **arguments,
        enabled_sources={"dssm", "item_item_cf", "profile_title", "popular", "explore"},
    )
    assert default == explicit
    assert {row.source for row in default.candidates} == {"profile_title", "popular", "explore"}


def test_source_allowlist_disables_only_requested_source_and_rejects_unknown() -> None:
    catalog = [CatalogItem("1", "alpha beta", None, 1, 10)]
    arguments = {
        "feed_type": "personalized",
        "catalog": catalog,
        "bundle": None,
        "source_user_id": None,
        "profile_title_preferences": {"alpha": {"score": 3}},
        "recent_item_ids": (),
        "item_item_index": None,
        "seed": 7,
        "top_n": 1,
    }
    result = retrieve_candidates(**arguments, enabled_sources={"popular"})
    assert [row.source for row in result.candidates] == ["popular"]
    with pytest.raises(ValueError, match="unknown recall sources"):
        retrieve_candidates(**arguments, enabled_sources={"not-a-source"})


def test_disabled_catalog_scans_do_not_execute() -> None:
    arguments = {
        "feed_type": "personalized",
        "catalog": [CatalogItem("1", "alpha beta", None, 1, 10)],
        "bundle": None,
        "source_user_id": None,
        "profile_title_preferences": {"alpha": {"score": 3}},
        "recent_item_ids": (),
        "item_item_index": None,
        "seed": 7,
        "top_n": 1,
        "enabled_sources": {"item_item_cf"},
    }
    with (
        mock.patch(
            "apps.api.app.feeds.retrieval._popular_score",
            side_effect=AssertionError("disabled popularity path executed"),
        ),
        mock.patch(
            "apps.api.app.feeds.retrieval._title_tokens",
            side_effect=AssertionError("disabled profile-title path executed"),
        ),
    ):
        result = retrieve_candidates(**arguments)
    assert result.candidates == ()
    assert result.fallback_reasons == ("empty_retrieval",)


def test_profile_title_source_is_deterministic_and_bounded_to_top_n() -> None:
    result = retrieve_candidates(
        feed_type="personalized",
        catalog=[
            CatalogItem("3", "alpha", None, 0, 0),
            CatalogItem("1", "alpha beta", None, 0, 0),
            CatalogItem("2", "alpha beta", None, 0, 0),
        ],
        bundle=None,
        source_user_id=None,
        profile_title_preferences={"alpha": {"score": 2}, "beta": {"score": 3}},
        recent_item_ids=(),
        item_item_index=None,
        seed=7,
        top_n=2,
        enabled_sources={"profile_title"},
    )
    assert [(row.item_id, row.raw_score) for row in result.candidates] == [
        ("1", 5.0),
        ("2", 5.0),
    ]

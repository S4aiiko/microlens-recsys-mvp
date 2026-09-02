from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from apps.api.app.settings import AppSettings


def test_search_defaults_are_internal_and_credential_free() -> None:
    with patch.dict("os.environ", {}, clear=True):
        settings = AppSettings.from_environment(allow_unconfigured=True)
    assert settings.search_url == "http://search:9200"
    assert settings.search_read_alias == "microlens-items-read"


def test_processed_data_root_is_explicitly_configurable() -> None:
    with patch.dict("os.environ", {"PROCESSED_DATA_ROOT": "/artifacts/processed"}, clear=True):
        settings = AppSettings.from_environment(allow_unconfigured=True)
    assert settings.processed_data_root == Path("/artifacts/processed")


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SEARCH_URL", "http://user:password@search:9200"),
        ("SEARCH_URL", "http://search:9200/path"),
        ("SEARCH_URL", "file:///tmp/search"),
        ("SEARCH_READ_ALIAS", "Uppercase"),
        ("SEARCH_READ_ALIAS", "../escape"),
        ("SEARCH_READ_ALIAS", "another-valid-alias"),
    ],
)
def test_search_settings_reject_credentials_paths_and_unsafe_aliases(name: str, value: str) -> None:
    with patch.dict("os.environ", {name: value}, clear=True), pytest.raises(ValueError):
        AppSettings.from_environment(allow_unconfigured=True)

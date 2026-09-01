from __future__ import annotations

import argparse
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from recsys.cli.modelctl import _publish


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *arguments):
        return False

    def read(self, amount: int = -1) -> bytes:
        del amount
        return json.dumps(self.payload).encode()


class ModelPublisherTests(unittest.TestCase):
    def arguments(self) -> argparse.Namespace:
        return argparse.Namespace(
            bundle="/artifacts/models/m1/bundle.json",
            manifest_checksum="a" * 64,
            expected_current_version="m0",
            internal_api_url="http://api:8001",
        )

    def test_publish_validates_then_sends_exact_cas_payload(self) -> None:
        bundle = SimpleNamespace(
            model_version="m1",
            manifest={
                "status": "READY",
                "activation_eligible": True,
                "evaluation_comparability": "comparable",
                "purpose": "base_official",
            },
        )
        captured: dict[str, object] = {}

        def open_request(request, timeout):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data)
            captured["token"] = request.headers["X-publish-token"]
            captured["timeout"] = timeout
            return _Response({"model_version": "m1", "status": "ACTIVE"})

        with (
            patch("recsys.cli.modelctl._validate", return_value=bundle) as validate,
            patch("recsys.cli.modelctl.urllib.request.urlopen", side_effect=open_request),
            patch.dict(os.environ, {"PUBLISH_TOKEN": "x" * 24}),
        ):
            result = _publish(self.arguments())
        validate.assert_called_once_with("/artifacts/models/m1/bundle.json", "a" * 64)
        self.assertEqual(captured["url"], "http://api:8001/internal/model-versions/m1/activate")
        self.assertEqual(
            captured["body"],
            {"expected_current_version": "m0", "manifest_checksum": "a" * 64},
        )
        self.assertEqual(captured["token"], "x" * 24)
        self.assertEqual(result["status"], "ACTIVE")

    def test_noncomparable_bundle_is_rejected_before_network(self) -> None:
        bundle = SimpleNamespace(
            model_version="m1",
            manifest={
                "status": "EVALUATED",
                "activation_eligible": False,
                "evaluation_comparability": "non_comparable",
                "purpose": "systems_only",
            },
        )
        with (
            patch("recsys.cli.modelctl._validate", return_value=bundle),
            patch("recsys.cli.modelctl.urllib.request.urlopen") as open_request,
            self.assertRaisesRegex(ValueError, "publisher refuses"),
        ):
            _publish(self.arguments())
        open_request.assert_not_called()


if __name__ == "__main__":
    unittest.main()

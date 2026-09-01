from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

from recsys.models.bundle import load_bundle


def _validate(path: str, checksum: str):
    bundle = load_bundle(path, checksum)
    bundle.smoke()
    return bundle


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="Validate or atomically activate ModelBundles")
    actions = command.add_subparsers(dest="action", required=True)
    validate = actions.add_parser("validate")
    validate.add_argument("--bundle", required=True)
    validate.add_argument("--manifest-checksum", required=True)
    publish = actions.add_parser("publish")
    publish.add_argument("--bundle", required=True)
    publish.add_argument("--manifest-checksum", required=True)
    publish.add_argument("--expected-current-version")
    publish.add_argument(
        "--internal-api-url",
        default=os.environ.get("INTERNAL_API_URL", "http://api:8001"),
    )
    return command


def _publish(arguments: argparse.Namespace) -> dict[str, object]:
    bundle = _validate(arguments.bundle, arguments.manifest_checksum)
    manifest = bundle.manifest
    if (
        manifest.get("status") != "READY"
        or manifest.get("activation_eligible") is not True
        or manifest.get("evaluation_comparability") != "comparable"
        or manifest.get("purpose") == "systems_only"
    ):
        raise ValueError("publisher refuses a model that is not READY/comparable/eligible")
    token = os.environ.get("PUBLISH_TOKEN")
    if token is None or len(token.encode("utf-8")) < 24:
        raise ValueError("PUBLISH_TOKEN must contain at least 24 bytes")
    body = json.dumps(
        {
            "expected_current_version": arguments.expected_current_version,
            "manifest_checksum": arguments.manifest_checksum,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    url = (
        arguments.internal_api_url.rstrip("/")
        + f"/internal/model-versions/{bundle.model_version}/activate"
    )
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Publish-Token": token,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        safe_body = exc.read(4096).decode("utf-8", errors="replace")
        raise RuntimeError(f"publish failed with HTTP {exc.code}: {safe_body}") from exc
    if not isinstance(payload, dict) or payload.get("model_version") != bundle.model_version:
        raise RuntimeError("activation API returned an unexpected model version")
    return payload


def main() -> int:
    arguments = parser().parse_args()
    if arguments.action == "validate":
        bundle = _validate(arguments.bundle, arguments.manifest_checksum)
        payload: dict[str, object] = {
            "model_version": bundle.model_version,
            "data_version": bundle.data_version,
            "manifest_checksum": bundle.manifest_checksum,
            "bundle_path": str(Path(arguments.bundle)),
            "status": bundle.manifest["status"],
            "activation_eligible": bundle.manifest["activation_eligible"],
        }
    else:
        payload = _publish(arguments)
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import json
from pathlib import Path

from apps.api.app.internal_main import create_internal_app
from apps.api.app.main import create_public_app
from apps.api.app.settings import AppSettings

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "docs" / "contracts"


def canonical(document: object) -> bytes:
    return (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def generated_documents() -> dict[Path, bytes]:
    settings = AppSettings.from_environment(allow_unconfigured=True)
    public = create_public_app(settings)
    internal = create_internal_app(settings)
    return {
        CONTRACTS / "openapi.json": canonical(public.openapi()),
        CONTRACTS / "internal-openapi.json": canonical(internal.openapi()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate public/internal runtime API contracts")
    parser.add_argument("--check", action="store_true", help="fail instead of writing on drift")
    arguments = parser.parse_args()
    drift: list[str] = []
    for path, payload in generated_documents().items():
        if arguments.check:
            if not path.exists() or path.read_bytes() != payload:
                drift.append(str(path.relative_to(ROOT)))
        else:
            path.write_bytes(payload)
            print(f"generated={path.relative_to(ROOT)}")
    if drift:
        print(f"contract_drift={','.join(drift)}")
        return 1
    if arguments.check:
        print("contract_drift=none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

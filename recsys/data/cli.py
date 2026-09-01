from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .artifacts import ParquetCodec
from .common import canonical_json_bytes
from .events import build_training_data
from .pipeline import build_official_dataset, inspect_official_files


def _print(value: Any) -> None:
    print(canonical_json_bytes(value).decode("utf-8"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deterministic MicroLens data pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect_parser = subparsers.add_parser("inspect", help="read-only official-file inspection")
    inspect_parser.add_argument("--raw-dir", type=Path, required=True)

    build_parser = subparsers.add_parser("build-official", help="build immutable Parquet data")
    build_parser.add_argument("--config", type=Path, required=True)
    build_parser.add_argument("--raw-dir", type=Path, required=True)
    build_parser.add_argument("--output-root", type=Path, required=True)

    event_parser = subparsers.add_parser(
        "build-training-data", help="build an immutable event-derived data version"
    )
    event_parser.add_argument("--base-data-version", required=True)
    event_parser.add_argument("--processed-root", type=Path, required=True)
    event_parser.add_argument("--event-export", type=Path, required=True)
    event_parser.add_argument("--mapping-config", type=Path, required=True)
    event_parser.add_argument(
        "--purpose", choices=("systems_only", "quality_evaluation"), required=True
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "inspect":
        _print(inspect_official_files(args.raw_dir))
        return 0
    if args.command == "build-official":
        result = build_official_dataset(
            args.config,
            args.raw_dir,
            args.output_root,
            codec=ParquetCodec(),
        )
    else:
        result = build_training_data(
            args.base_data_version,
            args.processed_root,
            args.event_export,
            args.mapping_config,
            args.purpose,
            codec=ParquetCodec(),
        )
    _print(
        {
            "data_version": result.data_version,
            "manifest_checksum": result.manifest_checksum,
            "path": str(result.path),
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

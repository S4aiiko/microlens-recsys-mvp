from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence

from sqlalchemy.exc import SQLAlchemyError

from apps.api.app.db.session import create_database_engine, create_session_factory
from apps.api.app.events.export import (
    EventExportError,
    TrainingEventExporter,
    validate_watermark_name,
)

EXIT_OK = 0
EXIT_CONFIGURATION = 2
EXIT_DATABASE = 3
EXIT_EXPORT = 4


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="export-training-events",
        description="Atomically export one PostgreSQL event-id watermark range.",
    )
    parser.add_argument(
        "--watermark-name",
        default="online-events",
        help="Explicit training_export_watermarks row name (default: online-events).",
    )
    return parser


def run(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        validate_watermark_name(args.watermark_name)
    except ValueError:
        print("export-training-events: invalid watermark name", file=sys.stderr)
        return EXIT_CONFIGURATION
    values = os.environ if environ is None else environ
    database_url = values.get("DATABASE_URL", "")
    output_root = values.get("TRAINING_EXPORTS_DIR", "")
    if not database_url or not output_root:
        print(
            "export-training-events: DATABASE_URL and TRAINING_EXPORTS_DIR are required",
            file=sys.stderr,
        )
        return EXIT_CONFIGURATION
    engine = None
    try:
        engine = create_database_engine(database_url)
        factory = create_session_factory(engine)
        with factory.begin() as session:
            result = TrainingEventExporter().export(
                session,
                output_root=output_root,
                watermark_name=args.watermark_name,
            )
    except SQLAlchemyError:
        # Deliberately omit exception text: driver messages may contain connection
        # details. Operators can correlate the stable exit code with protected logs.
        print("export-training-events: database operation failed", file=sys.stderr)
        return EXIT_DATABASE
    except (EventExportError, OSError, RuntimeError, ValueError):
        print("export-training-events: artifact export failed", file=sys.stderr)
        return EXIT_EXPORT
    finally:
        if engine is not None:
            engine.dispose()

    print(
        json.dumps(
            {
                "accepted": result.accepted,
                "end_inclusive": result.end_inclusive,
                "manifest_checksum": result.manifest_checksum,
                "path": str(result.path),
                "rejected": result.rejected,
                "reused": result.reused,
                "start_exclusive": result.start_exclusive,
                "watermark_name": args.watermark_name,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return EXIT_OK


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()

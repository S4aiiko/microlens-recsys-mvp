from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections.abc import Sequence
from typing import Any, Protocol

from apps.api.app.search.domain import (
    FullReindexResult,
    FullReindexSpec,
    IndexBuildConflict,
    ProjectionUnavailable,
)

DEFAULT_RUNTIME_FACTORY = ("apps.api.app.search.runtime", "build_full_reindexer_from_environment")


class FullReindexRunner(Protocol):
    def run(self, spec: FullReindexSpec) -> FullReindexResult: ...


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        prog="python -m apps.api.app.cli.search_reindex",
        description="Build, seal and atomically activate one versioned item search index.",
    )
    value.add_argument("--index-version", required=True)
    value.add_argument("--source-version", required=True)
    value.add_argument("--expected-current-index")
    value.add_argument("--batch-size", type=int, default=500)
    return value


def run(argv: Sequence[str] | None, *, runner: FullReindexRunner) -> FullReindexResult:
    args = parser().parse_args(argv)
    return runner.run(
        FullReindexSpec(
            index_version=args.index_version,
            source_version=args.source_version,
            expected_current_index=args.expected_current_index,
            batch_size=args.batch_size,
        )
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: FullReindexRunner | None = None,
    stdout: Any = None,
    stderr: Any = None,
) -> int:
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    try:
        selected_runner = runner or _load_default_runner()
        result = run(argv, runner=selected_runner)
    except ValueError:
        _emit(stderr, {"status": "error", "code": "invalid_reindex_request"})
        return 2
    except IndexBuildConflict:
        _emit(stderr, {"status": "error", "code": "search_index_conflict"})
        return 3
    except ProjectionUnavailable:
        _emit(stderr, {"status": "error", "code": "search_projection_unavailable"})
        return 4
    except Exception:
        # Never expose an exception string: it may contain a database/ES credential.
        _emit(stderr, {"status": "error", "code": "search_runtime_unavailable"})
        return 5
    _emit(
        stdout,
        {
            "status": "ok",
            "physical_index": result.physical_index,
            "previous_index": result.previous_index,
            "document_count": result.document_count,
            "projection_checksum": result.projection_checksum,
            "replayed": result.replayed,
        },
    )
    return 0


def _load_default_runner() -> FullReindexRunner:
    module_name, attribute = DEFAULT_RUNTIME_FACTORY
    module = importlib.import_module(module_name)
    factory = getattr(module, attribute)
    runner = factory()
    if not callable(getattr(runner, "run", None)):
        raise RuntimeError("invalid search runtime factory")
    return runner


def _emit(stream: Any, payload: dict[str, object]) -> None:
    print(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        file=stream,
    )


if __name__ == "__main__":
    raise SystemExit(main())

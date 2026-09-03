from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .phase7a import preflight_phase7a, resolve_matrix, resolved_matrix_document, run_phase7a


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="Resolve or run the frozen Phase 7A matrix")
    subparsers = value.add_subparsers(dest="command", required=True)
    resolve = subparsers.add_parser("resolve")
    resolve.add_argument("--matrix", default="configs/models/experiment-matrix.json")
    resolve.add_argument("--repo-root", default=".")
    for command in ("run", "preflight"):
        run = subparsers.add_parser(command)
        run.add_argument("--matrix", default="configs/models/experiment-matrix.json")
        run.add_argument("--repo-root", default=".")
        run.add_argument("--processed-root", required=True)
        run.add_argument("--data-version", required=True)
        run.add_argument("--data-manifest-checksum", required=True)
        run.add_argument("--output-root", required=True)
        run.add_argument("--run-id", required=True)
        run.add_argument("--git-revision", required=True)
        run.add_argument("--image-digest", required=True)
        run.add_argument("--source-checksum", required=True)
        run.add_argument("--attestation-path", required=True)
    return value


def main(argv: list[str] | None = None) -> int:
    effective_argv = list(sys.argv[1:] if argv is None else argv)
    arguments = parser().parse_args(effective_argv)
    if arguments.command == "resolve":
        resolved = resolve_matrix(arguments.matrix, repo_root=arguments.repo_root)
        print(json.dumps(resolved_matrix_document(resolved), sort_keys=True))
        return 0
    common = {
        "matrix_path": arguments.matrix,
        "repo_root": arguments.repo_root,
        "processed_root": arguments.processed_root,
        "data_version": arguments.data_version,
        "data_manifest_checksum": arguments.data_manifest_checksum,
        "output_root": arguments.output_root,
        "run_id": arguments.run_id,
        "git_revision": arguments.git_revision,
        "image_digest": arguments.image_digest,
        "requested_source_checksum": arguments.source_checksum,
        "attestation_path": arguments.attestation_path,
    }
    if arguments.command == "preflight":
        result = preflight_phase7a(**common)
        print(json.dumps(result, sort_keys=True))
        return 0
    result = run_phase7a(
        **common,
        command=[
            str(Path(sys.executable)),
            "-m",
            "recsys.experiments.phase7a_cli",
            *effective_argv,
        ],
    )
    print(json.dumps({"status": "PASS", "result": str(result)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

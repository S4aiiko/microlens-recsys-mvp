from __future__ import annotations

import argparse
import json

from recsys.models.entrypoint import train_model


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="Train an immutable two-stage model bundle")
    command.add_argument("--processed-root", required=True)
    command.add_argument("--data-version", required=True)
    command.add_argument("--data-manifest-checksum", required=True)
    command.add_argument("--config", required=True)
    command.add_argument("--output-root", required=True)
    command.add_argument("--checkpoint-root")
    command.add_argument("--resume-dssm")
    command.add_argument("--resume-deepfm")
    return command


def main() -> int:
    arguments = parser().parse_args()
    artifact = train_model(
        processed_root=arguments.processed_root,
        data_version=arguments.data_version,
        data_manifest_checksum=arguments.data_manifest_checksum,
        config=arguments.config,
        output_root=arguments.output_root,
        checkpoint_root=arguments.checkpoint_root,
        resume_dssm=arguments.resume_dssm,
        resume_deepfm=arguments.resume_deepfm,
    )
    print(
        json.dumps(
            {
                "model_version": artifact.model_version,
                "manifest_checksum": artifact.manifest_checksum,
                "bundle_checksum": artifact.bundle_checksum,
                "bundle_path": str(artifact.bundle_path),
                "status": artifact.status,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

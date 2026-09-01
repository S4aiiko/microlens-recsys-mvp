from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from test_events import _event, _mapping, _write_export
from test_pipeline import _config, _write_raw

from recsys.data import (
    EventExportError,
    ImmutableArtifactError,
    JsonLinesCodec,
    build_official_dataset,
    build_training_data,
    canonical_json_bytes,
)
from recsys.data.pipeline import _verify_existing


def _rewrite_manifest(path: Path, manifest: dict[str, object]) -> None:
    path.write_bytes(canonical_json_bytes(manifest) + b"\n")


class ImmutableArtifactTests(unittest.TestCase):
    def _base(self, root: Path):
        raw = root / "raw"
        _write_raw(raw)
        return build_official_dataset(_config(), raw, root / "processed", codec=JsonLinesCodec())

    def test_invalid_base_version_paths_fail_before_io(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for value in (
                "",
                ".",
                "..",
                "/absolute",
                "nested/version",
                "nested\\version",
                "latest",
            ):
                with (
                    self.subTest(value=value),
                    self.assertRaisesRegex(EventExportError, "explicit immutable version"),
                ):
                    build_training_data(
                        value,
                        root,
                        root / "unused-export",
                        _mapping(),
                        "systems_only",
                        codec=JsonLinesCodec(),
                    )

    def test_base_directory_and_manifest_symlinks_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = self._base(root)
            export = _write_export(
                root / "export",
                [_event(1, "click", "2026-01-01T00:00:00Z")],
                "2026-01-04T00:00:00Z",
            )

            symlink_root = root / "symlink-root"
            symlink_root.mkdir()
            (symlink_root / base.data_version).symlink_to(base.path, target_is_directory=True)
            with self.assertRaisesRegex(ImmutableArtifactError, "real directory"):
                build_training_data(
                    base.data_version,
                    symlink_root,
                    export,
                    _mapping(),
                    "systems_only",
                    codec=JsonLinesCodec(),
                )

            copied_root = root / "copied-root"
            copied_root.mkdir()
            copied = copied_root / base.data_version
            shutil.copytree(base.path, copied)
            manifest = copied / "manifest.json"
            real_manifest = copied / "manifest.real.json"
            manifest.rename(real_manifest)
            manifest.symlink_to(real_manifest.name)
            with self.assertRaisesRegex(ImmutableArtifactError, "lacks manifest"):
                build_training_data(
                    base.data_version,
                    copied_root,
                    export,
                    _mapping(),
                    "systems_only",
                    codec=JsonLinesCodec(),
                )

    def test_existing_manifest_and_descriptors_are_strictly_verified(self) -> None:
        mutations = {
            "version": lambda manifest: manifest.__setitem__("data_version", "wrong-version"),
            "traversal": lambda manifest: manifest["artifacts"][0].__setitem__(
                "path", "../escape.jsonl"
            ),
            "missing-sha": lambda manifest: manifest["artifacts"][0].pop("sha256"),
            "bad-size": lambda manifest: manifest["artifacts"][0].__setitem__("size_bytes", -1),
            "duplicate": lambda manifest: manifest["artifacts"].append(
                dict(manifest["artifacts"][0])
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                base = self._base(root)
                manifest_path = base.path / "manifest.json"
                manifest = json.loads(manifest_path.read_text())
                mutate(manifest)
                _rewrite_manifest(manifest_path, manifest)
                with self.assertRaises(ImmutableArtifactError):
                    _verify_existing(base.path)

    def test_existing_artifact_and_output_directory_symlinks_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = self._base(root)
            items = base.path / "items.jsonl"
            real_items = root / "real-items.jsonl"
            shutil.copyfile(items, real_items)
            items.unlink()
            items.symlink_to(real_items)
            with self.assertRaisesRegex(ImmutableArtifactError, "real file"):
                _verify_existing(base.path)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_root = root / "source"
            raw = source_root / "raw"
            _write_raw(raw)
            original = build_official_dataset(
                _config(), raw, source_root / "processed", codec=JsonLinesCodec()
            )
            output = root / "output"
            output.mkdir()
            (output / original.data_version).symlink_to(original.path, target_is_directory=True)
            with self.assertRaisesRegex(ImmutableArtifactError, "real directory"):
                build_official_dataset(_config(), raw, output, codec=JsonLinesCodec())

    def test_title_corpus_descriptor_is_required_for_event_build(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base = self._base(root)
            manifest_path = base.path / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["artifacts"] = [
                row for row in manifest["artifacts"] if row["path"] != "title_corpus.jsonl"
            ]
            _rewrite_manifest(manifest_path, manifest)
            export = _write_export(
                root / "export",
                [_event(1, "click", "2026-01-01T00:00:00Z")],
                "2026-01-04T00:00:00Z",
            )
            with self.assertRaisesRegex(EventExportError, "title_corpus"):
                build_training_data(
                    base.data_version,
                    root / "processed",
                    export,
                    _mapping(),
                    "systems_only",
                    codec=JsonLinesCodec(),
                )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from recsys.experiments.source_identity import (
    CONTROL_FILES,
    SOURCE_FILES,
    validate_reviewed_source,
)
from recsys.models.errors import ModelInputError
from scripts.phase7a_launcher import build_docker_argv, build_image_argv

IMAGE_REFERENCE = "registry.example/worker@sha256:" + "c" * 64
REPOSITORY = Path(__file__).resolve().parents[2]


def _source_tree(root: Path) -> None:
    for name in ("apps", "recsys", "configs"):
        target = root / name
        target.mkdir(parents=True)
        (target / f"{name}.txt").write_text(f"{name}\n")
    for relative in (*SOURCE_FILES, *CONTROL_FILES, "README.md", "notes.txt"):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{relative}\n")


def _commit_source_tree(root: Path) -> tuple[str, str]:
    _source_tree(root)
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "phase7a@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Phase 7A Test"], cwd=root, check=True)
    subprocess.run(["git", "add", "--all"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=root, check=True, capture_output=True)
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()
    return revision, validate_reviewed_source(root, revision)


@unittest.skipUnless(shutil.which("git"), "Git is required for commit-tree boundary tests")
class Phase7AGitBoundaryTests(unittest.TestCase):
    def test_formal_build_rejects_dirty_launcher_and_copied_source(self) -> None:
        for dirty_path in ("scripts/phase7a_launcher.py", "apps/apps.txt"):
            with (
                self.subTest(dirty_path=dirty_path),
                tempfile.TemporaryDirectory(dir=REPOSITORY) as temporary,
            ):
                root = Path(temporary)
                revision, checksum = _commit_source_tree(root)
                target = root / dirty_path
                target.write_text(target.read_text() + "dirty\n")
                environment = {
                    "GIT_REVISION": revision,
                    "PHASE7A_SOURCE_CHECKSUM": checksum,
                    "PHASE7A_BUILD_TAG": "phase7a:test",
                }
                with self.assertRaisesRegex(ModelInputError, "entirely clean Git worktree"):
                    build_image_argv(environment=environment, repo_root=root)

    def test_formal_build_uses_exact_bounded_buildkit_argv(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPOSITORY) as temporary:
            root = Path(temporary)
            revision, checksum = _commit_source_tree(root)
            argv = build_image_argv(
                environment={
                    "GIT_REVISION": revision,
                    "PHASE7A_SOURCE_CHECKSUM": checksum,
                    "PHASE7A_BUILD_TAG": "phase7a:test",
                },
                repo_root=root,
            )
            self.assertEqual(argv[:3], ["docker", "buildx", "build"])
            self.assertEqual(argv.count("--load"), 1)
            resources = [
                argv[index + 1] for index, token in enumerate(argv) if token == "--resource"
            ]
            self.assertEqual(
                resources,
                ["memory=5g", "cpu-period=100000", "cpu-quota=400000"],
            )
            self.assertEqual(argv.count("--resource"), 3)

    def test_all_tracked_dirt_is_refused_including_external_controls(self) -> None:
        for dirty_path, staged in (
            ("README.md", False),
            ("notes.txt", True),
            (".dockerignore", False),
            ("pyproject.toml", True),
            ("docs/phase-7a-experiments.md", False),
            (".github/workflows/ci.yml", True),
        ):
            with (
                self.subTest(dirty_path=dirty_path, staged=staged),
                tempfile.TemporaryDirectory(dir=REPOSITORY) as temporary,
            ):
                root = Path(temporary)
                revision, checksum = _commit_source_tree(root)
                target = root / dirty_path
                target.write_text(target.read_text() + "dirty\n")
                if staged:
                    subprocess.run(["git", "add", "--", dirty_path], cwd=root, check=True)
                with self.assertRaisesRegex(ModelInputError, "entirely clean Git worktree"):
                    build_image_argv(
                        environment={
                            "GIT_REVISION": revision,
                            "PHASE7A_SOURCE_CHECKSUM": checksum,
                            "PHASE7A_BUILD_TAG": "phase7a:test",
                        },
                        repo_root=root,
                    )

    def test_root_and_script_side_untracked_bootstrap_paths_are_refused(self) -> None:
        for untracked_path in (
            "sitecustomize.py",
            "usercustomize.py",
            "arbitrary-untracked.txt",
            "scripts/json.py",
            "scripts/subprocess.py",
        ):
            with (
                self.subTest(untracked_path=untracked_path),
                tempfile.TemporaryDirectory(dir=REPOSITORY) as temporary,
            ):
                root = Path(temporary)
                revision, checksum = _commit_source_tree(root)
                target = root / untracked_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("raise SystemExit('must not execute')\n")
                with self.assertRaisesRegex(ModelInputError, "entirely clean Git worktree"):
                    build_image_argv(
                        environment={
                            "GIT_REVISION": revision,
                            "PHASE7A_SOURCE_CHECKSUM": checksum,
                            "PHASE7A_BUILD_TAG": "phase7a:test",
                        },
                        repo_root=root,
                    )

    def test_isolated_launcher_refuses_bootstrap_files_before_they_execute(self) -> None:
        launcher = REPOSITORY / "scripts/phase7a_launcher.py"
        for untracked_path in ("sitecustomize.py", "usercustomize.py", "scripts/json.py"):
            with (
                self.subTest(untracked_path=untracked_path),
                tempfile.TemporaryDirectory(dir=REPOSITORY) as temporary,
            ):
                root = Path(temporary)
                scripts = root / "scripts"
                scripts.mkdir()
                shutil.copyfile(launcher, scripts / launcher.name)
                subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
                subprocess.run(
                    ["git", "config", "user.email", "phase7a@example.invalid"],
                    cwd=root,
                    check=True,
                )
                subprocess.run(
                    ["git", "config", "user.name", "Phase 7A Test"], cwd=root, check=True
                )
                subprocess.run(["git", "add", "--all"], cwd=root, check=True)
                subprocess.run(
                    ["git", "commit", "-m", "launcher"],
                    cwd=root,
                    check=True,
                    capture_output=True,
                )
                marker = root / "bootstrap-executed"
                payload = (
                    f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n"
                )
                target = root / untracked_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(payload)
                completed = subprocess.run(
                    [sys.executable, "-I", "-S", str(scripts / launcher.name), "build"],
                    cwd=root,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(completed.returncode, 2)
                self.assertIn("entirely clean Git worktree", completed.stderr)
                self.assertFalse(marker.exists())

    def test_formal_make_targets_use_isolated_python_without_pythonpath(self) -> None:
        makefile = (REPOSITORY / "Makefile").read_text()
        for target in (
            "phase7a-checksum",
            "phase7a-build",
            "phase7a-run",
            "phase7a-preflight",
            "phase7a-render",
        ):
            recipe = makefile.split(f"{target}:\n", 1)[1].split("\n\n", 1)[0]
            self.assertIn("$(PYTHON) -I -S scripts/phase7a_launcher.py", recipe)
            self.assertNotIn("PYTHONPATH", recipe)
        public_contract = (REPOSITORY / "docs/phase-7a-experiments.md").read_text()
        self.assertIn("phase7a-checksum", public_contract)
        self.assertNotIn("-m recsys.experiments.source_identity compute", public_contract)

    def test_clean_formal_make_render_imports_project_in_isolated_mode(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPOSITORY) as temporary:
            root = Path(temporary)
            for directory in ("apps", "recsys", "configs"):
                shutil.copytree(
                    REPOSITORY / directory,
                    root / directory,
                    ignore=shutil.ignore_patterns(
                        "node_modules", "dist", ".vite", "__pycache__", "*.pyc"
                    ),
                )
            (root / "scripts").mkdir()
            shutil.copyfile(
                REPOSITORY / "scripts/phase7a_launcher.py",
                root / "scripts/phase7a_launcher.py",
            )
            for relative in (*SOURCE_FILES[:-1], *CONTROL_FILES, ".gitignore"):
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(REPOSITORY / relative, target)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(
                ["git", "config", "user.email", "phase7a@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(["git", "config", "user.name", "Phase 7A Test"], cwd=root, check=True)
            subprocess.run(["git", "add", "--all"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-m", "clean-render"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            processed = root / "processed"
            processed.mkdir()
            (processed / ".keep").write_text("tracked fixture input root\n")
            subprocess.run(["git", "add", "-f", "--", "processed"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-m", "processed-root"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            revision = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            checksum = validate_reviewed_source(root, revision)
            run_root = root / "fresh-output"
            environment = {
                **os.environ,
                "DATA_VERSION": "data-v1",
                "DATA_MANIFEST_CHECKSUM": "f" * 64,
                "GIT_REVISION": revision,
                "RUN_ID": "run-1",
                "PHASE7A_IMAGE": IMAGE_REFERENCE,
                "PHASE7A_SOURCE_CHECKSUM": checksum,
                "PHASE7A_PROCESSED_ROOT": str(processed),
                "PHASE7A_RUN_ROOT": str(run_root),
            }
            completed = subprocess.run(
                ["make", f"PYTHON={sys.executable}", "phase7a-render"],
                cwd=root,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            argv = json.loads(completed.stdout)
            self.assertEqual(argv[:2], ["docker", "run"])
            self.assertFalse(run_root.exists())

    def test_ignored_virtualenv_startup_hooks_never_precede_actual_make_entries(self) -> None:
        with (
            tempfile.TemporaryDirectory(dir=REPOSITORY) as temporary,
            tempfile.TemporaryDirectory() as fake_temporary,
        ):
            root = Path(temporary)
            for directory in ("apps", "recsys", "configs"):
                shutil.copytree(
                    REPOSITORY / directory,
                    root / directory,
                    ignore=shutil.ignore_patterns(
                        "node_modules", "dist", ".vite", "__pycache__", "*.pyc"
                    ),
                )
            (root / "scripts").mkdir()
            shutil.copyfile(
                REPOSITORY / "scripts/phase7a_launcher.py",
                root / "scripts/phase7a_launcher.py",
            )
            for relative in (*SOURCE_FILES[:-1], *CONTROL_FILES, ".gitignore"):
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(REPOSITORY / relative, target)
            with (root / ".gitignore").open("a") as handle:
                handle.write("\nprocessed/\nprobe-output/\noutput/\n")
            processed = root / "processed"
            processed.mkdir()
            (processed / ".keep").write_text("fixture\n")
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(
                ["git", "config", "user.email", "phase7a@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(["git", "config", "user.name", "Phase 7A Test"], cwd=root, check=True)
            subprocess.run(["git", "add", "--all"], cwd=root, check=True)
            subprocess.run(["git", "add", "-f", "--", "processed/.keep"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-m", "virtualenv-boundary"],
                cwd=root,
                check=True,
                capture_output=True,
            )
            revision = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            checksum = validate_reviewed_source(root, revision)

            virtualenv = root / ".venv"
            subprocess.run(
                [sys.executable, "-m", "venv", "--without-pip", str(virtualenv)], check=True
            )
            site_packages = next((virtualenv / "lib").glob("python*/site-packages"))
            sentinels = {
                "pth": root / "pth-executed",
                "sitecustomize": root / "sitecustomize-executed",
                "usercustomize": root / "usercustomize-executed",
            }
            (site_packages / "phase7a_audit.pth").write_text(
                "import pathlib; pathlib.Path(" + repr(str(sentinels["pth"])) + ").touch()\n"
            )
            for module in ("sitecustomize", "usercustomize"):
                (site_packages / f"{module}.py").write_text(
                    f"from pathlib import Path\nPath({str(sentinels[module])!r}).touch()\n"
                )

            fake_root = Path(fake_temporary)
            docker_log = fake_root / "docker.log"
            fake_docker = fake_root / "docker"
            fake_docker.write_text(
                "#!/bin/sh\n"
                "printf 'CALL' >> \"$PHASE7A_FAKE_DOCKER_LOG\"\n"
                'for argument in "$@"; do printf \'\\t%s\' "$argument" >> '
                '"$PHASE7A_FAKE_DOCKER_LOG"; done\n'
                "printf '\\n' >> \"$PHASE7A_FAKE_DOCKER_LOG\"\n"
            )
            fake_docker.chmod(0o755)
            probe_root = root / "probe-output"
            probe_root.mkdir()
            historical_parent = root / "output/phase7a"
            historical_parent.mkdir(parents=True)
            run_root = historical_parent / "r6-run"
            environment = {
                **os.environ,
                "PATH": f"{fake_root}{os.pathsep}{os.environ['PATH']}",
                "PHASE7A_FAKE_DOCKER_LOG": str(docker_log),
                "DATA_VERSION": "data-v1",
                "DATA_MANIFEST_CHECKSUM": "f" * 64,
                "GIT_REVISION": revision,
                "RUN_ID": "run-1",
                "PHASE7A_IMAGE": IMAGE_REFERENCE,
                "PHASE7A_SOURCE_CHECKSUM": checksum,
                "PHASE7A_BUILD_TAG": "phase7a:test",
                "PHASE7A_PROCESSED_ROOT": str(processed),
                "PHASE7A_RUN_ROOT": str(run_root),
            }
            python = virtualenv / "bin/python"

            checksum_result = subprocess.run(
                ["make", f"PYTHON={python}", "phase7a-checksum"],
                cwd=root,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(checksum_result.stdout.strip(), checksum)
            for sentinel in sentinels.values():
                self.assertFalse(sentinel.exists())

            rendered = subprocess.run(
                ["make", f"PYTHON={python}", "phase7a-render"],
                cwd=root,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(json.loads(rendered.stdout)[:2], ["docker", "run"])
            self.assertFalse(run_root.exists())
            for sentinel in sentinels.values():
                self.assertFalse(sentinel.exists())

            subprocess.run(
                ["make", f"PYTHON={python}", "phase7a-build"],
                cwd=root,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            preflight_environment = {
                **environment,
                "PHASE7A_RUN_ROOT": str(probe_root),
                "RUN_ID": "probe-no-data",
            }
            subprocess.run(
                ["make", f"PYTHON={python}", "phase7a-preflight"],
                cwd=root,
                env=preflight_environment,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["make", f"PYTHON={python}", "phase7a-run"],
                cwd=root,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            for sentinel in sentinels.values():
                self.assertFalse(sentinel.exists())
            calls = docker_log.read_text().splitlines()
            self.assertEqual(len(calls), 3)
            self.assertTrue(calls[0].startswith("CALL\tbuildx\tbuild"))
            self.assertIn("\trun\t", calls[1])
            self.assertIn("\tpreflight\t", calls[1])
            self.assertIn("\trun\t", calls[2])
            self.assertTrue(run_root.is_dir())

    def test_git_replace_ref_cannot_make_an_old_revision_accept_new_bytes(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPOSITORY) as temporary:
            root = Path(temporary)
            old_revision, old_checksum = _commit_source_tree(root)
            target = root / "apps/apps.txt"
            target.write_text("new tree\n")
            subprocess.run(["git", "add", "--all"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-m", "new-tree"], cwd=root, check=True, capture_output=True
            )
            new_revision = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            subprocess.run(["git", "update-ref", "HEAD", old_revision], cwd=root, check=True)
            subprocess.run(["git", "replace", old_revision, new_revision], cwd=root, check=True)
            ordinary_status = subprocess.run(
                ["git", "status", "--porcelain=v1", "--untracked-files=all"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertEqual(ordinary_status, "")
            with self.assertRaisesRegex(ModelInputError, "entirely clean Git worktree"):
                build_image_argv(
                    environment={
                        "GIT_REVISION": old_revision,
                        "PHASE7A_SOURCE_CHECKSUM": old_checksum,
                        "PHASE7A_BUILD_TAG": "phase7a:test",
                    },
                    repo_root=root,
                )

    def test_formal_run_rejects_relevant_untracked_file_before_output(self) -> None:
        with tempfile.TemporaryDirectory(dir=REPOSITORY) as temporary:
            root = Path(temporary)
            revision, checksum = _commit_source_tree(root)
            processed = root / "processed"
            processed.mkdir()
            output = root / "fresh-output"
            environment = {
                "DATA_VERSION": "data-v1",
                "DATA_MANIFEST_CHECKSUM": "f" * 64,
                "GIT_REVISION": revision,
                "RUN_ID": "run-1",
                "PHASE7A_IMAGE": IMAGE_REFERENCE,
                "PHASE7A_SOURCE_CHECKSUM": checksum,
                "PHASE7A_PROCESSED_ROOT": str(processed),
                "PHASE7A_RUN_ROOT": str(output),
            }
            (root / "recsys/untracked_runtime.py").write_text("raise SystemExit(1)\n")
            with self.assertRaisesRegex(ModelInputError, "entirely clean Git worktree"):
                build_docker_argv(mode="run", environment=environment, repo_root=root)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()

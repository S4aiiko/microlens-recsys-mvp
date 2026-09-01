from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class ComplianceContractTest(unittest.TestCase):
    def test_compliance_artifacts_have_no_generator_drift(self) -> None:
        result = subprocess.run(
            [sys.executable, "scripts/generate_compliance.py", "--check"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_sbom_covers_exact_locks_images_and_reference_boundary(self) -> None:
        sbom = json.loads((ROOT / "docs/sbom.cdx.json").read_text(encoding="utf-8"))
        components = sbom["components"]
        refs = {component["bom-ref"] for component in components}
        self.assertEqual(sbom["bomFormat"], "CycloneDX")
        self.assertEqual(sbom["compositions"][0]["aggregate"], "incomplete")
        self.assertIn("pkg:pypi/redis@7.3.1", refs)
        self.assertIn("pkg:pypi/pyarrow@25.0.1", refs)
        self.assertTrue(any(ref.startswith("pkg:npm/react@19.2.8") for ref in refs))
        self.assertEqual(sum(ref.startswith("pkg:docker/") for ref in refs), 4)
        reference = next(c for c in components if c["name"] == "oasdiff/oasdiff")
        properties = {prop["name"]: prop["value"] for prop in reference["properties"]}
        self.assertEqual(reference["scope"], "excluded")
        self.assertEqual(properties["microlens:relation"], "reference-only")
        self.assertEqual(properties["microlens:source-copied"], "false")
        self.assertNotIn("microlens:source-imported", properties)

        installed = next(c for c in components if c["bom-ref"] == "pkg:pypi/redis@7.3.1")
        installed_properties = {prop["name"]: prop["value"] for prop in installed["properties"]}
        self.assertEqual(installed_properties["microlens:relation"], "direct")
        self.assertEqual(installed_properties["microlens:source-copied"], "false")

        cffi = next(c for c in components if c["name"] == "cffi")
        self.assertEqual(cffi["version"], "2.1.1")
        self.assertEqual(cffi["licenses"], [{"expression": "MIT-0"}])

        sources = (ROOT / "docs/open-source-sources.md").read_text(encoding="utf-8")
        self.assertIn("Vendored/copied source", sources)
        self.assertIn("through `pip`/`npm`", sources)

    def test_every_locked_python_and_npm_component_is_in_sbom(self) -> None:
        sbom = json.loads((ROOT / "docs/sbom.cdx.json").read_text(encoding="utf-8"))
        inventory = {
            (component["name"].lower().replace("_", "-"), component["version"])
            for component in sbom["components"]
        }
        for lock in ROOT.glob("requirements-*.lock"):
            for match in re.finditer(r"^([A-Za-z0-9_.-]+)==([^ ;\\]+)", lock.read_text(), re.M):
                component = (match.group(1).lower().replace("_", "-"), match.group(2))
                with self.subTest(lock=lock.name, component=component):
                    self.assertIn(component, inventory)

        npm_lock = json.loads((ROOT / "apps/web/package-lock.json").read_text(encoding="utf-8"))
        for path, entry in npm_lock["packages"].items():
            if not path or not entry.get("version"):
                continue
            name = entry.get("name") or path.rsplit("node_modules/", 1)[-1]
            component = (name.lower(), str(entry["version"]))
            with self.subTest(path=path, component=component):
                self.assertIn(component, inventory)

    def test_make_cache_stats_is_docker_first(self) -> None:
        makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
        target = makefile.split("cache-stats:\n", 1)[1].split("\n\npublish:", 1)[0]
        self.assertIn("$(DOCKER_COMPOSE) exec -T api", target)
        self.assertIn("apps.api.app.cli.cache_stats", target)
        self.assertNotIn("placeholder cache-stats", target)

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUTPUTS = {
    ROOT / "docs" / "sbom.cdx.json": "sbom",
    ROOT / "docs" / "open-source-sources.md": "sources",
    ROOT / "THIRD_PARTY_NOTICES.md": "notices",
}

# Snapshot from the exact release metadata on PyPI. UNKNOWN is used only when
# the registry did not publish a machine-readable expression and an official
# repository check did not resolve it. Never guess a license from a package name.
PYTHON_LICENSES = {
    "alembic": "MIT",
    "annotated-doc": "MIT",
    "annotated-types": "MIT",
    "anyio": "MIT",
    "argon2-cffi": "MIT",
    "argon2-cffi-bindings": "MIT",
    "attrs": "MIT",
    "certifi": "MPL-2.0",
    "cffi": "MIT-0",
    "click": "BSD-3-Clause",
    "colorama": "BSD-3-Clause",
    "duckdb": "MIT",
    "elastic-transport": "Apache-2.0",
    "elasticsearch": "Apache-2.0",
    "fastapi": "MIT",
    "filelock": "MIT",
    "fsspec": "BSD-3-Clause",
    "greenlet": "MIT AND PSF-2.0",
    "h11": "MIT",
    "httpcore": "BSD-3-Clause",
    "httptools": "MIT",
    "httpx": "BSD-3-Clause",
    "idna": "BSD-3-Clause",
    "iniconfig": "MIT",
    "jinja2": "BSD-3-Clause",
    "jsonschema": "MIT",
    "jsonschema-specifications": "MIT",
    "mako": "MIT",
    "markupsafe": "BSD-3-Clause",
    "mpmath": "BSD-3-Clause",
    "networkx": "BSD-3-Clause",
    "packaging": "Apache-2.0 OR BSD-2-Clause",
    "pluggy": "MIT",
    "psycopg": "LGPL-3.0-only",
    "psycopg-binary": "LGPL-3.0-only",
    "pwdlib": "MIT",
    "pyarrow": "Apache-2.0",
    "pycparser": "BSD-3-Clause",
    "pydantic": "MIT",
    "pydantic-core": "MIT",
    "pygments": "BSD-2-Clause",
    "pyjwt": "MIT",
    "pytest": "MIT",
    "python-dateutil": "Apache-2.0 OR BSD-3-Clause",
    "python-dotenv": "BSD-3-Clause",
    "pyyaml": "MIT",
    "redis": "MIT",
    "referencing": "MIT",
    "rpds-py": "MIT",
    "ruff": "MIT",
    "sqlalchemy": "MIT",
    "starlette": "BSD-3-Clause",
    "setuptools": "MIT",
    "six": "MIT",
    "sniffio": "MIT",
    "sympy": "BSD-3-Clause",
    "torch": (
        "Apache-2.0 AND Apache-2.0 WITH LLVM-exception AND BSD-2-Clause "
        "AND BSD-3-Clause AND BSL-1.0 AND MIT"
    ),
    "typing-extensions": "PSF-2.0",
    "typing-inspection": "MIT",
    "uvicorn": "BSD-3-Clause",
    "uvloop": "MIT",
    "urllib3": "MIT",
    "watchfiles": "MIT",
    "websockets": "BSD-3-Clause",
}

BASE_IMAGE_SOURCES = {
    "python": "https://github.com/docker-library/python",
    "node": "https://github.com/nodejs/docker-node",
    "postgres": "https://github.com/docker-library/postgres",
    "redis": "https://github.com/redis/docker-library-redis",
    "elasticsearch": "https://github.com/elastic/elasticsearch",
}

PYTHON_PACKAGE_SOURCES = {
    # The model lock is intentionally resolved from PyTorch's official CPU-only
    # wheel index; the ``+cpu`` build is not distributed by PyPI.
    "torch": "https://download.pytorch.org/whl/cpu/torch/",
}

# These were inspected only. They are deliberately excluded from the runtime
# dependency graph and recorded to prevent "reference" from being mistaken for
# copied or vendored source.
REFERENCES = [
    ("westlake-repl/MicroLens", "0fc876066987fb3b920df2765cfbac2763c515eb", "UNKNOWN"),
    ("recommenders-team/recommenders", "0bb4b3690941ffb668118e31ccaf8a7d19f8212a", "MIT"),
    ("apple/ml-negative-sampling", "8dc093469cf0ac693dd894fc904e1f2e88cc34e7", "UNKNOWN"),
    ("frictionlessdata/frictionless-py", "5debad3409639438bb4dbffa15d200dbb458a555", "MIT"),
    ("fastapi/full-stack-fastapi-template", "486f054cc8d1aead59ec96cc0a16933d06c10e0d", "MIT"),
    ("fastapi-users/fastapi-users", "9ef8cd82619856772ac06a178b114eb47c79586c", "MIT"),
    ("oasdiff/oasdiff", "9e66f6fe923f14816d39898320450b65e6932a55", "Apache-2.0"),
    ("docker/awesome-compose", "reference-gate", "CC0-1.0"),
    ("microsoft/playwright", "reference-gate", "Apache-2.0"),
    ("CycloneDX/cyclonedx-python", "reference-only-2026-09-01", "Apache-2.0"),
    ("raimon49/pip-licenses", "reference-only-2026-09-01", "MIT"),
    ("procrastinate-org/procrastinate", "509487b0765c3a95be93424ec5c844d8e306c089", "MIT"),
    ("hyzyla/outbox-streaming", "6f682a64104c7004935c9a75f47f843422955707", "MIT"),
    ("prometheus/alertmanager", "7935b44682464fa7ba3e8a1f15a6f39eff1b3369", "Apache-2.0"),
    ("apache/arrow", "a769c291e01093b73d03a075179cf7a09bf92ad8", "Apache-2.0"),
    ("duckdb/duckdb", "d8cdaa33fda8df955cc76ef58a280f68f4cd43fa", "MIT"),
    ("apache/iceberg", "86da2dc8414756e05106b3272fd6e6d0dde306e3", "Apache-2.0"),
    ("rixwew/pytorch-fm", "f74ad19771eda104e99874d19dc892e988ec53fa", "MIT"),
    ("USTCLLM/RecStudio", "9114975b8e9ec85bce16c1ed8abbf0e194e4afb3", "MIT"),
    ("elastic/elasticsearch-py", "76e23a37d0cea34c7a580781fd6bf2b678139fb4", "Apache-2.0"),
    (
        "elastic/elasticsearch",
        "3c7c6027c5769d860d87448e2749f4c550a239da",
        "Elastic-2.0 OR AGPL-3.0-only OR SSPL-1.0",
    ),
]


def _normal(name: str) -> str:
    return name.lower().replace("_", "-")


def _python_direct() -> set[str]:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    return {
        _normal(name)
        for name in re.findall(r'^\s*"([A-Za-z0-9_.-]+)(?:\[[^]]+\])?[<>=]', text, re.M)
    }


def _python_components() -> list[dict[str, Any]]:
    direct = _python_direct()
    found: dict[tuple[str, str], set[str]] = {}
    for lock in sorted(ROOT.glob("requirements-*.lock")):
        scope = lock.stem.removeprefix("requirements-")
        for line in lock.read_text(encoding="utf-8").splitlines():
            match = re.match(r"^([A-Za-z0-9_.-]+)==([^ ;\\]+)", line)
            if match:
                key = (_normal(match.group(1)), match.group(2))
                found.setdefault(key, set()).add(scope)
    result = []
    for (name, version), scopes in sorted(found.items()):
        license_id = PYTHON_LICENSES.get(name, "UNKNOWN")
        result.append(
            _component(
                name=name,
                version=version,
                purl=f"pkg:pypi/{name}@{version}",
                license_id=license_id,
                source=PYTHON_PACKAGE_SOURCES.get(
                    name, f"https://pypi.org/project/{name}/{version}/"
                ),
                ecosystem="python",
                relation="direct" if name in direct else "transitive",
                scopes=",".join(sorted(scopes)),
            )
        )
    return result


def _npm_name(path: str, entry: dict[str, Any]) -> str:
    if entry.get("name"):
        return str(entry["name"])
    return path.rsplit("node_modules/", 1)[-1]


def _npm_components() -> list[dict[str, Any]]:
    lock = json.loads((ROOT / "apps/web/package-lock.json").read_text(encoding="utf-8"))
    root = lock["packages"][""]
    runtime = set(root.get("dependencies", {}))
    development = set(root.get("devDependencies", {}))
    found: dict[tuple[str, str], dict[str, Any]] = {}
    for path, entry in lock["packages"].items():
        if not path or not entry.get("version"):
            continue
        name = _npm_name(path, entry)
        found.setdefault((name, str(entry["version"])), entry)
    result = []
    for (name, version), entry in sorted(found.items()):
        is_direct = name in runtime or name in development
        scope = "runtime" if name in runtime else "dev" if name in development else "transitive"
        result.append(
            _component(
                name=name,
                version=version,
                purl=f"pkg:npm/{name.replace('@', '%40')}@{version}",
                license_id=str(entry.get("license") or "UNKNOWN"),
                source=str(
                    entry.get("resolved") or f"https://www.npmjs.com/package/{name}/v/{version}"
                ),
                ecosystem="npm",
                relation="direct" if is_direct else "transitive",
                scopes=scope,
            )
        )
    return result


def _parse_image(reference: str) -> tuple[str, str, str]:
    match = re.fullmatch(r"(?:[^/]+/)*([^/:@]+):([^@]+)@sha256:([0-9a-f]{64})", reference)
    if not match:
        raise ValueError(f"base image must be tag plus sha256 digest: {reference}")
    return match.group(1), match.group(2), match.group(3)


def _base_images() -> list[tuple[str, str, str, str, str]]:
    observed: dict[tuple[str, str, str], set[str]] = {}
    for service, dockerfile in (
        ("api", ROOT / "apps/api/Dockerfile"),
        ("model", ROOT / "docker/model.Dockerfile"),
        ("worker", ROOT / "apps/worker/Dockerfile"),
        ("web", ROOT / "apps/web/Dockerfile"),
    ):
        match = re.search(r"^FROM\s+(\S+)", dockerfile.read_text(encoding="utf-8"), re.M)
        if not match:
            raise ValueError(f"missing FROM in {dockerfile.relative_to(ROOT)}")
        parsed = _parse_image(match.group(1))
        observed.setdefault(parsed, set()).add(service)

    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    for service in ("db", "redis", "search"):
        match = re.search(rf"(?ms)^  {service}:\n.*?^    image:\s*(\S+)", compose)
        if not match:
            raise ValueError(f"missing digest-pinned image for Compose service {service}")
        parsed = _parse_image(match.group(1))
        observed.setdefault(parsed, set()).add(service)

    return [
        (name, tag, digest, BASE_IMAGE_SOURCES[name], ",".join(sorted(services)))
        for (name, tag, digest), services in sorted(observed.items())
    ]


def _component(
    *,
    name: str,
    version: str,
    purl: str,
    license_id: str,
    source: str,
    ecosystem: str,
    relation: str,
    scopes: str,
    component_type: str = "library",
) -> dict[str, Any]:
    license_value = (
        {"expression": license_id} if license_id != "UNKNOWN" else {"license": {"name": "UNKNOWN"}}
    )
    return {
        "type": component_type,
        "bom-ref": purl,
        "name": name,
        "version": version,
        "purl": purl,
        "licenses": [license_value],
        "externalReferences": [{"type": "distribution", "url": source}],
        "properties": [
            {"name": "microlens:ecosystem", "value": ecosystem},
            {"name": "microlens:relation", "value": relation},
            {"name": "microlens:scopes", "value": scopes},
            {"name": "microlens:source-copied", "value": "false"},
        ],
    }


def components() -> list[dict[str, Any]]:
    values = _python_components() + _npm_components()
    for name, tag, digest, source, services in _base_images():
        values.append(
            _component(
                name=name,
                version=f"{tag}@sha256:{digest}",
                purl=f"pkg:docker/{name}@sha256:{digest}",
                license_id="UNKNOWN",
                source=source,
                ecosystem="container",
                relation="direct",
                scopes=services,
                component_type="container",
            )
        )
    for repository, revision, license_id in REFERENCES:
        owner, name = repository.split("/", 1)
        component = _component(
            name=repository,
            version=revision,
            purl=f"pkg:github/{owner}/{name}@{revision}",
            license_id=license_id,
            source=f"https://github.com/{repository}",
            ecosystem="github-reference",
            relation="reference-only",
            scopes="none",
        )
        component["scope"] = "excluded"
        values.append(component)
    return sorted(values, key=lambda value: value["bom-ref"])


def render_sbom(values: list[dict[str, Any]]) -> str:
    direct = [c["bom-ref"] for c in values if _prop(c, "microlens:relation") == "direct"]
    dependencies = [{"ref": "microlens-recsys-mvp@0.0.0", "dependsOn": sorted(direct)}]
    dependencies.extend({"ref": c["bom-ref"], "dependsOn": []} for c in values)
    document = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "bom-ref": "microlens-recsys-mvp@0.0.0",
                "name": "microlens-recsys-mvp",
                "version": "0.0.0",
                "licenses": [{"license": {"id": "MIT"}}],
            }
        },
        "components": values,
        "dependencies": dependencies,
        "compositions": [{"aggregate": "incomplete", "assemblies": ["microlens-recsys-mvp@0.0.0"]}],
    }
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _prop(component: dict[str, Any], name: str) -> str:
    return next(prop["value"] for prop in component["properties"] if prop["name"] == name)


def _license(component: dict[str, Any]) -> str:
    value = component["licenses"][0]
    return value.get("expression") or value["license"].get("id") or value["license"]["name"]


def render_sources(values: list[dict[str, Any]]) -> str:
    rows = []
    for item in values:
        source = item["externalReferences"][0]["url"]
        ecosystem = _prop(item, "microlens:ecosystem")
        relation = _prop(item, "microlens:relation")
        scopes = _prop(item, "microlens:scopes")
        rows.append(
            f"| `{item['name']}` | `{item['version']}` | `{ecosystem}` | "
            f"`{relation}` | `{_license(item)}` | "
            f"[official/package source]({source}) | `{scopes}` | no |"
        )
    header = (
        "| Name | Version/revision | Ecosystem | Relation | License | Source | Scope | "
        "Vendored/copied source |"
    )
    return (
        """# Open-source sources and dependency inventory

This file is generated from the exact Python/npm locks, pinned Compose base-image
digests and approved read-only reference gates. Run `python scripts/generate_compliance.py
--check` to detect drift. Python/npm `direct` and `transitive` dependencies are installed
through `pip`/`npm`; `no` below means their source was not copied or vendored into this
repository, not that the dependency is absent. `reference-only` entries were inspected
with zero source copying and are excluded from the runtime graph. `UNKNOWN` is
intentional: a composite base image or upstream record did not provide one truthful
single SPDX expression. The CycloneDX composition is marked incomplete because OS
packages inside base-image layers are not enumerated by the application lock files.

"""
        + header
        + "\n|---|---|---|---|---|---|---|---|\n"
        + "\n".join(rows)
        + "\n"
    )


def render_notices(values: list[dict[str, Any]]) -> str:
    lines = [
        "# Third-party notices",
        "",
        "MicroLens Recsys MVP is MIT-licensed. Its installed dependencies and base",
        "images remain under their own licenses. This inventory is not a replacement",
        "for the license files shipped in the corresponding wheel, npm package or image.",
        "Python/npm dependencies are installed through package managers; listing them",
        "as direct/transitive use does not mean their source was copied into this repository.",
        "Entries marked `reference-only` have zero copied or vendored source.",
        "",
        "| Component | Version | License | Use |",
        "|---|---|---|---|",
    ]
    for item in values:
        relation = _prop(item, "microlens:relation")
        lines.append(
            f"| `{item['name']}` | `{item['version']}` | `{_license(item)}` | `{relation}` |"
        )
    lines.extend(["", "Machine-readable inventory: `docs/sbom.cdx.json`.", ""])
    return "\n".join(lines)


def generated() -> dict[Path, str]:
    values = components()
    return {
        ROOT / "docs" / "sbom.cdx.json": render_sbom(values),
        ROOT / "docs" / "open-source-sources.md": render_sources(values),
        ROOT / "THIRD_PARTY_NOTICES.md": render_notices(values),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate deterministic dependency compliance artifacts"
    )
    parser.add_argument("--check", action="store_true", help="fail when generated files drift")
    args = parser.parse_args()
    drift = []
    for path, content in generated().items():
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                drift.append(str(path.relative_to(ROOT)))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    if drift:
        print("compliance artifact drift: " + ", ".join(drift), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

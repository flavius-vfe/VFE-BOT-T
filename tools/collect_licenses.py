#!/usr/bin/env python3
"""Collect license files for packages installed by one pip --report run."""

from __future__ import annotations

import argparse
import json
import re
import shutil
from importlib import metadata
from pathlib import Path

LICENSE_NAMES = re.compile(r"^(licen[cs]e|copying|notice|copyright|authors?)([-_.].*)?$", re.I)


def normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def requested_packages(report_path: Path) -> dict[str, str]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if str(report.get("version")) != "1":
        raise RuntimeError(f"Unsupported pip report version: {report.get('version')!r}")

    packages: dict[str, str] = {}
    for item in report.get("install", []):
        package = item.get("metadata", {})
        name = package.get("name")
        version = package.get("version")
        if name and version:
            packages[normalize(str(name))] = str(version)
    if not packages:
        raise RuntimeError("The pip report contains no installed packages")
    return packages


def collect(report_path: Path, output_dir: Path) -> None:
    requested = requested_packages(report_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[str] = []
    missing: list[str] = []

    installed = {normalize(dist.metadata["Name"]): dist for dist in metadata.distributions() if dist.metadata.get("Name")}

    for key, expected_version in sorted(requested.items()):
        dist = installed.get(key)
        if dist is None:
            missing.append(f"{key}=={expected_version} (not installed)")
            continue

        package_name = dist.metadata["Name"]
        package_version = dist.version
        package_dir = output_dir / f"{normalize(package_name)}-{package_version}"
        copied = 0

        for file in dist.files or []:
            basename = Path(str(file)).name
            if not LICENSE_NAMES.match(basename):
                continue
            source = Path(dist.locate_file(file))
            if not source.is_file():
                continue
            package_dir.mkdir(parents=True, exist_ok=True)
            destination = package_dir / basename
            if destination.exists():
                destination = package_dir / f"{copied + 1}-{basename}"
            shutil.copyfile(source, destination)
            copied += 1

        license_value = dist.metadata.get("License-Expression") or dist.metadata.get("License") or "not declared"
        project_url = dist.metadata.get("Home-page") or ""
        metadata_text = (
            f"Package: {package_name}\n"
            f"Version: {package_version}\n"
            f"Declared license: {license_value}\n"
            f"Project URL: {project_url}\n"
        )
        package_dir.mkdir(parents=True, exist_ok=True)
        (package_dir / "PACKAGE-METADATA.txt").write_text(metadata_text, encoding="utf-8")

        if copied == 0:
            missing.append(f"{package_name}=={package_version} (no license file in distribution)")
        manifest.append(f"{package_name}=={package_version} | {license_value} | license files: {copied}")

    (output_dir.parent / "INSTALLED_PACKAGES.txt").write_text("\n".join(manifest) + "\n", encoding="utf-8")

    if missing:
        details = "\n - ".join(missing)
        raise RuntimeError(f"License collection incomplete:\n - {details}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    collect(args.report, args.output)


if __name__ == "__main__":
    main()

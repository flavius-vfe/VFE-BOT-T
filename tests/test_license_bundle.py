import json
import subprocess
import sys
from importlib import metadata
from pathlib import Path


def test_license_collector_copies_installed_distribution_notices(tmp_path: Path) -> None:
    pip_version = metadata.version("pip")
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(
            {
                "version": "1",
                "pip_version": pip_version,
                "install": [{"metadata": {"name": "pip", "version": pip_version}}],
            }
        ),
        encoding="utf-8",
    )

    output = tmp_path / "licenses" / "python-packages"
    script = Path(__file__).parents[1] / "tools" / "collect_licenses.py"
    subprocess.run(
        [sys.executable, str(script), "--report", str(report), "--output", str(output)],
        check=True,
    )

    package_dir = output / f"pip-{pip_version}"
    assert package_dir.is_dir()
    assert any(path.name.lower().startswith("license") for path in package_dir.iterdir())
    assert (output.parent / "INSTALLED_PACKAGES.txt").is_file()

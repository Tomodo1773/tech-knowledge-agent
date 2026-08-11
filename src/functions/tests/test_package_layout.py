from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).parents[3]
CHECKER = REPOSITORY_ROOT / "scripts" / "check-python-package-layout.py"


def test_package_checker_rejects_secret_configuration() -> None:
    functions_root = REPOSITORY_ROOT / "src" / "functions"
    with tempfile.TemporaryDirectory(
        prefix=".package-layout-test-", dir=functions_root
    ) as directory:
        project = Path(directory)
        (project / ".funcignore").write_text("", encoding="utf-8")
        (project / "function_app.py").write_text("", encoding="utf-8")
        (project / "host.json").write_text("{}", encoding="utf-8")
        (project / "requirements.txt").write_text("", encoding="utf-8")
        forbidden_files = (
            ".env",
            ".env.development",
            ".cache/item",
            ".git/config",
            ".pytest_cache/item",
            ".venv/secret.txt",
            "__pycache__/module.pyc",
            "local.settings.json",
        )
        for relative in forbidden_files:
            target = project / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("secret", encoding="utf-8")

        result = subprocess.run(
            [
                sys.executable,
                str(CHECKER),
                str(project),
                ".funcignore",
                "function_app.py",
                "host.json",
                "requirements.txt",
            ],
            capture_output=True,
            check=False,
            text=True,
        )

    assert result.returncode != 0
    assert "package contains forbidden files" in result.stderr
    for relative in forbidden_files:
        assert relative in result.stderr

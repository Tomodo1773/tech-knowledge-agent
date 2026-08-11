"""Build an ephemeral source archive and verify remote-build root files."""

from __future__ import annotations

import argparse
import fnmatch
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

FORBIDDEN_PARTS = frozenset(
    {
        ".cache",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "uv-cache",
        "venv",
    }
)


def _patterns(ignore_file: Path) -> tuple[str, ...]:
    return tuple(
        line.strip()
        for line in ignore_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def _is_ignored(relative: PurePosixPath, patterns: tuple[str, ...]) -> bool:
    value = relative.as_posix()
    for pattern in patterns:
        normalized = pattern.removeprefix("./")
        if normalized.endswith("/"):
            prefix = normalized.rstrip("/")
            if value == prefix or value.startswith(f"{prefix}/"):
                return True
        elif fnmatch.fnmatch(value, normalized) or fnmatch.fnmatch(relative.name, normalized):
            return True
    return False


def _is_forbidden(relative: PurePosixPath) -> bool:
    name = relative.name
    return (
        any(part in FORBIDDEN_PARTS for part in relative.parts)
        or name == ".env"
        or name.startswith(".env.")
        or name == "local.settings.json"
        or name.endswith((".pyc", ".pyo"))
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("project", type=Path)
    parser.add_argument("ignore_file")
    parser.add_argument("required", nargs="+")
    args = parser.parse_args()

    project = args.project.resolve()
    patterns = _patterns(project / args.ignore_file)
    with tempfile.TemporaryDirectory() as temp_dir:
        archive = Path(temp_dir) / "source.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
            for source in project.rglob("*"):
                if not source.is_file():
                    continue
                relative = PurePosixPath(source.relative_to(project).as_posix())
                if not _is_ignored(relative, patterns):
                    output.write(source, relative.as_posix())

        with zipfile.ZipFile(archive) as package:
            names = set(package.namelist())

    missing = sorted(set(args.required) - names)
    if missing:
        raise SystemExit(f"package root files are missing: {', '.join(missing)}")
    forbidden = sorted(name for name in names if _is_forbidden(PurePosixPath(name)))
    if forbidden:
        raise SystemExit(f"package contains forbidden files: {', '.join(forbidden)}")
    print(f"PACKAGE_ZIP_OK={project.name}:{','.join(args.required)}")


if __name__ == "__main__":
    main()

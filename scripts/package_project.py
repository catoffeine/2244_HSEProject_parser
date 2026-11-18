import argparse
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PATHS = (
    "pyproject.toml", "uv.lock", ".python-version", ".gitignore", "README.md",
    "ekb_parser", "configs", "docs", "scripts", "tests", "data", "project_files/analytics_data",
)
LOCAL_ONLY = {Path("scripts/make_transfer.py"), Path("tests/test_transfer.py")}
DATABASE_SUFFIXES = {".sqlite3", ".sqlite", ".db"}


def included(path, relative):
    return (
        path.is_file() and not path.is_symlink() and relative not in LOCAL_ONLY
        and not any(part == "__pycache__" or part.startswith(".") and part not in (".gitignore", ".python-version")
                    for part in relative.parts)
        and path.suffix not in {".pyc", ".pyo", ".tmp", ".log"}
        and not path.name.endswith(("-wal", "-shm", "-journal"))
    )


def snapshot_database(source, target):
    with (
        closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original,
        closing(sqlite3.connect(target)) as snapshot,
    ):
        original.backup(snapshot)
        if snapshot.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ValueError(f"Не удалось проверить снимок SQLite {source}")


def package_project(root, target):
    root = Path(root).resolve()
    target = Path(target).resolve()
    files = []
    for name in PACKAGE_PATHS:
        path = root / name
        for candidate in [path] if path.is_file() else path.rglob("*"):
            relative = candidate.relative_to(root)
            if candidate != target and included(candidate, relative):
                files.append((relative, candidate))

    target.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".package-", dir=target.parent) as directory:
        staged = Path(directory) / target.name
        snapshot = Path(directory) / "snapshot.sqlite3"
        with ZipFile(staged, "w", compression=ZIP_DEFLATED) as archive:
            for relative, source in sorted(files):
                if source.suffix in DATABASE_SUFFIXES:
                    # В снимок попадают и зафиксированные страницы, которые ещё лежат в WAL.
                    snapshot_database(source, snapshot)
                    archive.write(snapshot, relative.as_posix())
                    snapshot.unlink()
                else:
                    archive.write(source, relative.as_posix())
        staged.replace(target)
    return target


def main():
    parser = argparse.ArgumentParser(description="Собрать ZIP для переноса проекта и сохранённых сборов")
    parser.add_argument("--output", type=Path, default=ROOT / "dist/ekb-parser.zip", help="Путь к ZIP")
    args = parser.parse_args()
    print(package_project(ROOT, args.output))


if __name__ == "__main__":
    main()

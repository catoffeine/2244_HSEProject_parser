from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

from scripts.package_project import package_project


class PackageProjectTests(unittest.TestCase):
    def test_zip_includes_collected_data_and_committed_wal_pages(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {
                "README.md": "Документация",
                "project_files/analytics_data/requirements.html": "<table></table>",
                "data/yageo/runs/example/datasheets/document.pdf": "%PDF-1.7",
                "data/yageo/runs/example/ui.log": "Локальный журнал",
                "data/.env": "Локальные настройки",
                "scripts/make_transfer.py": "Локальный скрипт переноса",
            }
            for name, contents in paths.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(contents, encoding="utf-8")

            database = "data/yageo/runs/example/crawl_state.sqlite3"
            target = root / "dist/ekb-parser.zip"
            with closing(sqlite3.connect(root / database)) as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("CREATE TABLE test_record(value TEXT)")
                db.execute("INSERT INTO test_record VALUES ('Сохранённая запись')")
                db.commit()
                self.assertTrue((root / (database + "-wal")).is_file())
                package_project(root, target)

            with ZipFile(target) as archive:
                self.assertEqual(set(archive.namelist()), {
                    "README.md", "project_files/analytics_data/requirements.html",
                    "data/yageo/runs/example/datasheets/document.pdf", database,
                })
                restored = root / "restored.sqlite3"
                restored.write_bytes(archive.read(database))
            with closing(sqlite3.connect(restored)) as db:
                self.assertEqual(db.execute("SELECT value FROM test_record").fetchone(), ("Сохранённая запись",))

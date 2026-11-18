import csv
from contextlib import closing
from io import TextIOWrapper
import json
import re
import shutil
import sqlite3
from tempfile import TemporaryDirectory, TemporaryFile
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from ekb_parser.core.coverage import assessment_rows
from ekb_parser.core.storage import Store
from ekb_parser.web.reader import read_connection

FORMATS = {
    "csv": "text/csv",
    "jsonl": "application/x-ndjson",
    "sqlite": "application/vnd.sqlite3",
}
CSV_FIELDS = (
    "source", "source_id", "part_number", "manufacturer", "series", "product_url",
    "source_url", "retrieved_at", "datasheet_url", "specsheet_url", "parameters", "supplement",
)


def export_name(name):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")[:60] or "catalogue"


def write_text_export(stream, output, source, group, kind):
    encoding = "utf-8-sig" if kind == "csv" else "utf-8"
    text = TextIOWrapper(stream, encoding=encoding, newline="")
    try:
        with closing(read_connection(output)) as db:
            db.execute("BEGIN")
            entries = assessment_rows(db, where="""components.source = ? AND EXISTS (
                SELECT 1 FROM membership WHERE group_key = ? AND source = components.source
                AND source_id = components.source_id)""", params=(source, group))
            if kind == "csv":
                writer = csv.DictWriter(text, fieldnames=CSV_FIELDS, extrasaction="ignore", delimiter=";")
                writer.writeheader()
                for record, supplement, _ in entries:
                    writer.writerow({**record, "parameters": json.dumps(record["parameters"], ensure_ascii=False),
                                     "supplement": json.dumps(supplement, ensure_ascii=False)})
            else:
                for record, supplement, _ in entries:
                    if supplement:
                        record["supplement"] = supplement
                    text.write(json.dumps(record, ensure_ascii=False) + "\n")
    finally:
        text.detach()


def sqlite_export(path, output, source, group, table_name):
    Store(path.parent).close()
    with closing(sqlite3.connect(path.as_uri(), uri=True)) as db, db:
        db.execute("ATTACH DATABASE ? AS original", [(output / "crawl_state.sqlite3").as_uri() + "?mode=ro"])
        db.execute("INSERT INTO groups SELECT * FROM original.groups WHERE key=?", (group,))
        db.execute("""INSERT INTO components SELECT part.*
                      FROM original.components AS part JOIN original.membership AS member USING (source, source_id)
                      WHERE member.group_key=? AND source=?""", (group, source))
        db.execute("INSERT INTO membership SELECT * FROM original.membership WHERE group_key=? AND source=?", (group, source))
        tables = {row[0] for row in db.execute("SELECT name FROM original.sqlite_master WHERE type='table'")}
        if "datasheets" in tables:
            db.execute("""INSERT INTO datasheets
                          SELECT document.source, document.url, document.status, document.sha256, NULL,
                                 document.downloaded_at, document.error
                          FROM original.datasheets AS document
                          JOIN (SELECT DISTINCT source, json_extract(payload, '$.datasheet_url') AS url
                                FROM components) AS links USING (source, url)""")
        db.execute("""CREATE TABLE component_supplements (
            source TEXT NOT NULL, source_id TEXT NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY (source, source_id)
        )""")
        if "component_supplements" in tables:
            db.execute("""INSERT INTO component_supplements SELECT supplement.*
                          FROM original.component_supplements AS supplement JOIN membership USING (source, source_id)""")
        db.execute("INSERT INTO metadata VALUES ('catalogue_export', ?)",
                   (json.dumps({"source": source, "group": group, "name": table_name}, ensure_ascii=False),))


def download_group(output, source, group, kind, name):
    stream = TemporaryFile()
    try:
        if kind == "sqlite":
            with TemporaryDirectory() as directory:
                path = Path(directory) / "crawl_state.sqlite3"
                sqlite_export(path, output, source, group, name)
                with path.open("rb") as database:
                    shutil.copyfileobj(database, stream)
        else:
            write_text_export(stream, output, source, group, kind)
        stream.seek(0)
        return stream
    except BaseException:
        stream.close()
        raise


def archive_catalogue(categories, best, source, requirements, mappings):
    stream = TemporaryFile()
    manifest = []
    try:
        with ZipFile(stream, "w", compression=ZIP_DEFLATED, compresslevel=4) as archive:
            archive.writestr("requirements.json", json.dumps(requirements, ensure_ascii=False, indent=2) + "\n")
            archive.writestr("catalogue_mappings.json", json.dumps(mappings, ensure_ascii=False, indent=2) + "\n")
            for category in categories:
                for group in category["groups"].values():
                    selected = best.get((category["id"], group["id"]))
                    if not selected:
                        continue
                    _, output, group_key = selected
                    folder = f"{export_name(category['name'])}-{category['id'][:6]}/{export_name(group['name'])}-{group['id'][:6]}"
                    with TemporaryDirectory() as directory:
                        snapshot = Path(directory)
                        path = snapshot / "crawl_state.sqlite3"
                        sqlite_export(path, output, source, group_key, group["name"])
                        with closing(read_connection(snapshot)) as db:
                            count = db.execute("SELECT COUNT(*) FROM components").fetchone()[0]
                        manifest.append({"category": category["name"], "group": group["name"],
                                         "count": count, "folder": folder})
                        archive.write(path, folder + "/components.sqlite")
                        for kind in ("csv", "jsonl"):
                            with archive.open(folder + "/components." + kind, "w", force_zip64=True) as part:
                                write_text_export(part, snapshot, source, group_key, kind)
            archive.writestr("index.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        stream.seek(0)
        return stream
    except BaseException:
        stream.close()
        raise

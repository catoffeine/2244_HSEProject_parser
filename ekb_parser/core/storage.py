import csv
import json
import sqlite3
from pathlib import Path


def document_entries(db):
    urls = """SELECT DISTINCT source, json_extract(payload, '$.datasheet_url') AS url
              FROM components WHERE json_extract(payload, '$.datasheet_url') IS NOT NULL"""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='datasheets'").fetchone():
        return db.execute(f"SELECT source, url, NULL, NULL FROM ({urls}) ORDER BY source, url")
    return db.execute(f"""SELECT urls.source, urls.url, datasheets.status, datasheets.local_path
                          FROM ({urls}) AS urls LEFT JOIN datasheets USING (source, url)
                          ORDER BY urls.source, urls.url""")


def saved_document_path(output, local_path):
    if not local_path:
        return None
    path = (output / local_path).resolve()
    if not path.is_relative_to(output.resolve()):
        return None
    try:
        if path.is_file():
            with path.open("rb") as stream:
                if stream.read(5) == b"%PDF-":
                    return path
    except OSError:
        # Файл могли удалить, пока мы его открывали.
        return None
    return None


class Store:
    def __init__(self, output):
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.output / "crawl_state.sqlite3", timeout=30)
        self.db.executescript("""
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS groups (
                key TEXT PRIMARY KEY, name TEXT NOT NULL, query TEXT NOT NULL,
                next_offset INTEGER NOT NULL DEFAULT 0, pages INTEGER NOT NULL DEFAULT 0,
                expected_count INTEGER, complete INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS components (
                source TEXT NOT NULL, source_id TEXT NOT NULL,
                manufacturer TEXT NOT NULL, part_number TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(source, source_id)
            );
            CREATE TABLE IF NOT EXISTS membership (
                group_key TEXT NOT NULL REFERENCES groups(key),
                source TEXT NOT NULL, source_id TEXT NOT NULL,
                PRIMARY KEY(group_key, source, source_id),
                FOREIGN KEY(source, source_id) REFERENCES components(source, source_id)
            );
            CREATE TABLE IF NOT EXISTS datasheets (
                source TEXT NOT NULL, url TEXT NOT NULL, status TEXT NOT NULL,
                sha256 TEXT, local_path TEXT, downloaded_at TEXT, error TEXT,
                PRIMARY KEY(source,url)
            );
        """)

    def initialize(self, config):
        fingerprint = config.fingerprint()
        row = self.db.execute("SELECT value FROM metadata WHERE key='config_hash'").fetchone()
        if row and row[0] != fingerprint:
            raise ValueError("This output belongs to different queries/page size. Use a new --output directory.")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('config_hash', ?)", (fingerprint,))
            for group in config.groups:
                self.db.execute("INSERT OR IGNORE INTO groups(key,name,query) VALUES (?,?,?)",
                                (group.key, group.name, json.dumps(group.query, ensure_ascii=False)))
        self._write_json("collection_config.json", config.model_dump())

    def progress(self, key):
        row = self.db.execute("SELECT next_offset,pages,expected_count,complete FROM groups WHERE key=?", (key,)).fetchone()
        return dict(zip(("next_offset", "pages", "expected_count", "complete"), row))

    def set_expected(self, key, count):
        with self.db:
            self.db.execute("UPDATE groups SET expected_count=? WHERE key=?", (count, key))

    def count(self):
        return self.db.execute("SELECT COUNT(*) FROM components").fetchone()[0]

    def save_page(self, key, offset, records, *, complete=False):
        progress = self.progress(key)
        if progress["next_offset"] != offset:
            raise ValueError(f"Offset mismatch: expected {progress['next_offset']}, got {offset}")
        expected = progress["expected_count"]
        ids = [(r["source"], r["source_id"]) for r in records]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate source IDs within a page")
        if not records and expected is not None and offset < expected:
            raise ValueError(f"Empty page at {offset}, but source advertises {expected} rows")
        previous = {row[0] for row in self.db.execute(
            "SELECT source_id FROM membership WHERE group_key=? AND source=? AND source_id IN ("
            + ",".join("?" for _ in records) + ")",
            [key, records[0]["source"], *[r["source_id"] for r in records]],
        )} if records else set()
        if previous:
            raise ValueError("Overlapping catalogue pages: source ordering changed. Restart in a new output directory.")
        # Строки страницы и новый offset записываем вместе, чтобы сбор можно было продолжить.
        with self.db:
            for record in records:
                self.db.execute("""INSERT INTO components VALUES (?,?,?,?,?)
                    ON CONFLICT(source,source_id) DO UPDATE SET
                    manufacturer=excluded.manufacturer,part_number=excluded.part_number,payload=excluded.payload""",
                    (record["source"], record["source_id"], record["manufacturer"],
                     record["part_number"], json.dumps(record, ensure_ascii=False)))
                self.db.execute("INSERT INTO membership VALUES (?,?,?)", (key, record["source"], record["source_id"]))
            self.db.execute("UPDATE groups SET next_offset=?,pages=pages+1,complete=? WHERE key=?",
                            (offset + len(records), int(complete), key))

    def summary(self):
        self.db.row_factory = sqlite3.Row
        groups = [dict(r) for r in self.db.execute("""SELECT g.key,g.name,g.next_offset,g.pages,
            g.expected_count,g.complete,COUNT(m.source_id) AS saved
            FROM groups g LEFT JOIN membership m ON m.group_key=g.key GROUP BY g.key""")]
        self.db.row_factory = None
        return {"unique_components": self.count(), "groups": groups}

    def export(self):
        self.db.execute("CREATE INDEX IF NOT EXISTS membership_component ON membership(source,source_id)")
        self.db.commit()
        paths = {name: self.output / name for name in ("parts.jsonl", "articles.txt", "parts.csv", "documents.jsonl")}
        with (paths["parts.jsonl"].with_suffix(".jsonl.tmp").open("w", encoding="utf-8") as js,
              paths["articles.txt"].with_suffix(".txt.tmp").open("w", encoding="utf-8") as articles,
              paths["parts.csv"].with_suffix(".csv.tmp").open("w", encoding="utf-8-sig", newline="") as cf):
            fields = ["source", "source_id", "manufacturer", "part_number", "series", "groups", "datasheet_url", "parameters"]
            writer = csv.DictWriter(cf, fieldnames=fields)
            writer.writeheader()
            for (payload,) in self.db.execute("SELECT payload FROM components ORDER BY source,source_id"):
                record = json.loads(payload)
                record["groups"] = [r[0] for r in self.db.execute(
                    "SELECT group_key FROM membership WHERE source=? AND source_id=? ORDER BY group_key",
                    (record["source"], record["source_id"]))]
                js.write(json.dumps(record, ensure_ascii=False) + "\n")
                articles.write(record["part_number"] + "\n")
                flat = {k: record.get(k) for k in fields}
                for key in ("parameters", "groups"):
                    flat[key] = json.dumps(flat[key], ensure_ascii=False)
                writer.writerow(flat)
        with paths["documents.jsonl"].with_suffix(".jsonl.tmp").open("w", encoding="utf-8") as docs:
            for source, url, count in self.db.execute("""SELECT source,json_extract(payload,'$.datasheet_url'),COUNT(*)
                FROM components WHERE json_extract(payload,'$.datasheet_url') IS NOT NULL GROUP BY 1,2"""):
                docs.write(json.dumps({"source": source, "url": url, "component_count": count}, ensure_ascii=False) + "\n")
        for path in paths.values():
            path.with_suffix(path.suffix + ".tmp").replace(path)
        # Здесь считаем, как часто поле встречается в каталоге.
        parameters = []
        group_counts = dict(self.db.execute("SELECT group_key,COUNT(*) FROM membership GROUP BY group_key"))
        for row in self.db.execute("""SELECT m.group_key,c.source,
                json_extract(p.value,'$.source_parameter_id'),json_extract(p.value,'$.name'),
                COUNT(DISTINCT c.source_id),MIN(CAST(json_extract(p.value,'$.formatted_value') AS TEXT))
            FROM components c JOIN membership m USING(source,source_id), json_each(c.payload,'$.parameters') p
            GROUP BY 1,2,3,4 ORDER BY 1,3"""):
            parameter = dict(zip(("group", "source", "source_parameter_id", "name", "components_with_field", "example"), row))
            parameter["group_components"] = group_counts[parameter["group"]]
            parameter["field_presence_fraction"] = parameter["components_with_field"] / parameter["group_components"]
            parameters.append(parameter)
        self._write_json("group_parameters.json", parameters)
        self._write_json("summary.json", self.summary())

    def _write_json(self, name, value):
        path = self.output / name
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(path)

    def save_document(self, source, url, *, sha256=None, local_path=None, downloaded_at=None, error=None):
        with self.db:
            self.db.execute("""INSERT INTO datasheets VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(source,url) DO UPDATE SET status=excluded.status,sha256=excluded.sha256,
                local_path=excluded.local_path,downloaded_at=excluded.downloaded_at,error=excluded.error""",
                (source, url, "failed" if error else "downloaded", sha256, local_path, downloaded_at, error))

    def export_documents(self):
        path = self.output / "datasheet_downloads.jsonl"
        temp = path.with_suffix(".jsonl.tmp")
        with temp.open("w", encoding="utf-8") as stream:
            for row in self.db.execute("SELECT source,url,status,sha256,local_path,downloaded_at,error FROM datasheets ORDER BY source,url"):
                record = dict(zip(("source", "url", "status", "sha256", "local_path", "downloaded_at", "error"), row))
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        temp.replace(path)

    def close(self):
        self.db.close()

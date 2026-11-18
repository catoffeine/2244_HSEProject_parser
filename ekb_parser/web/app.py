import argparse
import atexit
from contextlib import closing
from datetime import datetime, timezone
from errno import EEXIST, ENOTEMPTY
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import secrets
import signal
import sqlite3
import subprocess
import sys
import threading
from tempfile import TemporaryDirectory

from flask import Flask, abort, jsonify, render_template, request, send_file
from pydantic import BaseModel, ConfigDict, Field
from werkzeug.exceptions import RequestEntityTooLarge

from ekb_parser.core.catalogue_mappings import (
    MAPPINGS_FILE, CatalogueRule, MappingScope, load_mappings, query_key, write_mappings,
)
from ekb_parser.core.config import CollectionConfig, load_config
from ekb_parser.core.coverage import Coverage, assessment_rows, import_supplements
from ekb_parser.core.requirements import (
    HTML_DIRECTORY, REQUIREMENTS_FILE, file_version, load_requirements, requirements_version,
)
from ekb_parser.core.storage import document_entries, saved_document_path
from ekb_parser.sources.registry import list_sources
from ekb_parser.web.reader import (
    PAGE_SIZE as READER_PAGE_SIZE, catalogue_index, collection_version, component, group_statistics, listing, read_connection,
    saved_analyst_groups,
)
from ekb_parser.web.reader_export import FORMATS, archive_catalogue, download_group, export_name

ROOT = Path(__file__).resolve().parents[2]
FILES = ("parts.csv", "parts.jsonl", "articles.txt", "documents.jsonl",
         "group_parameters.json", "summary.json", "last_run.json", "crawl_state.sqlite3", "ui.log",
         "datasheet_downloads.jsonl", "last_datasheet_run.json", "coverage.jsonl", "collection_config.json")
PAGES = {
    "reader": ("/catalogue", "Каталог", "▦"),
    "collect": ("/", "Сбор каталога", "⌘"),
    "results": ("/results", "Результаты", "▤"),
    "components": ("/components", "Компоненты", "◫"),
    "sources": ("/sources", "Источники", "≡"),
    "logs": ("/logs", "Журнал", "›_"),
}
TOOLS_LOG = Path("data/analytics/ui.log")


def output_path(root, value):
    if not value or not isinstance(value, str):
        raise ValueError("Укажите папку результата внутри data/")
    path = (root / value).resolve()
    data = (root / "data").resolve()
    if path == data or not path.is_relative_to(data):
        raise ValueError("Результаты должны находиться в отдельной папке внутри data/")
    return path


def read_log(path):
    try:
        with path.open("rb") as stream:
            stream.seek(max(0, os.fstat(stream.fileno()).st_size - 48000))
            return stream.read().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def filter_output(root, preset, group_key):
    if not isinstance(preset, str):
        raise ValueError("Выберите конфигурацию источника")
    path = (root / preset).resolve()
    config_root = (root / "configs").resolve()
    if not path.is_relative_to(config_root) or path.suffix != ".json" or not path.is_file():
        raise ValueError("Выберите конфигурацию из списка")
    config = load_config(path)
    if not any(group.key == group_key for group in config.groups):
        raise ValueError("Выберите группу этой конфигурации")
    target = output_path(root, f"data/{config.source}/metadata/{path.relative_to(config_root).with_suffix('').as_posix()}")
    return path, target / f"filters_{group_key}.json"


def remove_empty_directory(path):
    try:
        path.rmdir()
    except OSError as error:
        if error.errno not in (ENOTEMPTY, EEXIST):
            raise


def read_summary(output):
    if not (output / "crawl_state.sqlite3").is_file():
        return {"unique_components": 0, "groups": []}
    with closing(read_connection(output)) as db:
        count = db.execute("SELECT COUNT(*) FROM components").fetchone()[0]
        groups = [dict(zip(("key", "name", "saved", "pages", "expected_count", "complete"), row))
                  for row in db.execute("SELECT key,name,next_offset,pages,expected_count,complete FROM groups ORDER BY key")]
    return {"unique_components": count, "groups": groups}


@lru_cache(maxsize=16)
def read_coverage(root, output, matrix_version, run_version, mappings_version):
    if run_version[0] is None:
        return {"total": 0, "assessed": 0, "unassessed": 0, "filled": 0, "required": 0, "percent": None}
    assessment = Coverage(load_requirements(root, matrix_version), output, load_mappings(root))
    with closing(read_connection(output)) as db:
        db.execute("BEGIN")
        return assessment.summary(db)


def read_document_status(output):
    downloaded = failed = pending = 0
    checked_paths = {}
    if (output / "crawl_state.sqlite3").is_file():
        with closing(read_connection(output)) as db:
            for _, _, status, local_path in document_entries(db):
                if status == "downloaded":
                    if local_path not in checked_paths:
                        checked_paths[local_path] = saved_document_path(output, local_path)
                    if checked_paths[local_path] is not None:
                        downloaded += 1
                        continue
                pending += 1
                if status == "failed":
                    failed += 1
    unique_files = len({path for path in checked_paths.values() if path is not None})
    return {"downloaded": downloaded, "failed": failed, "pending": pending, "unique_files": unique_files}


class Launch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    output: str
    mode: str = Field(pattern="^(new|resume)$")
    config: CollectionConfig
    max_pages: int = Field(default=2, ge=0, le=1000000)
    max_parts: int = Field(default=200, ge=1, le=100000000)
    log_level: str = Field(default="INFO", pattern="^(DEBUG|INFO|WARNING|ERROR)$")
    no_export: bool = False


class PresetInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    key: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=140)
    catalogue: str | None = Field(default=None, min_length=1, max_length=2000)
    category_file: str | None = Field(default=None, min_length=1, max_length=255)
    category_name: str | None = Field(default=None, min_length=1, max_length=140)
    category_key: str | None = Field(default=None, min_length=1, max_length=80, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    request_url: str = Field(min_length=1, max_length=16000)


class PresetRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    request_url: str = Field(min_length=1, max_length=16000)


class MappingInput(BaseModel):
    preset: str
    catalogue_group: str
    sheet: str
    group: str
    field: str
    mapping: CatalogueRule | None = None


class Jobs:
    def __init__(self, root):
        self.root = root
        self.lock = threading.RLock()
        self.process = None
        self.log_file = None
        self.output = None
        self.stop_requested = False
        self.started_at = None
        self.action = None

    def running(self):
        return self.process is not None and self.process.poll() is None

    def state(self):
        with self.lock:
            active = self.running()
            if not active and self.log_file:
                self.log_file.close()
                self.log_file = None
            return {
                "running": active, "stopping": active and self.stop_requested,
                "exit_code": self.process.poll() if self.process else None,
                "output": self.output.relative_to(self.root).as_posix() if self.output else None,
                "started_at": self.started_at, "action": self.action,
                "stopped": self.stop_requested and not active,
            }

    def _spawn(self, output, args, action):
        if self.log_file:
            self.log_file.close()
        log_path = output / "ui.log" if output is not None else self.root / TOOLS_LOG
        if log_path.is_symlink() or not log_path.resolve().is_relative_to((self.root / "data").resolve()):
            raise ValueError("Журнал должен находиться внутри data/")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_file = log_path.open("a", encoding="utf-8")
        started_at = datetime.now(timezone.utc).isoformat()
        self.log_file.write(f"\n[{started_at}] {action}\n")
        self.log_file.flush()
        options = {"start_new_session": True} if os.name != "nt" else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        try:
            self.process = subprocess.Popen(
                [sys.executable, "-u", "-m", "ekb_parser", *args], cwd=self.root,
                stdout=self.log_file, stderr=subprocess.STDOUT, **options,
            )
        except Exception:
            self.log_file.close()
            self.log_file = None
            raise
        self.output = output
        self.started_at = started_at
        self.stop_requested = False
        self.action = action

    def collect(self, launch):
        with self.lock:
            if self.running():
                raise ValueError("Уже идёт сбор или экспорт. Дождитесь завершения или остановите его.")
            output = output_path(self.root, launch.output)
            state = output / "crawl_state.sqlite3"
            if launch.mode == "new" and state.exists():
                raise ValueError("В папке уже есть результат. Выберите «Продолжить» или другую папку.")
            if launch.mode == "resume":
                if not state.is_file():
                    raise ValueError("В этой папке нет сохранённого сбора. Выберите «С нуля».")
                with closing(read_connection(output)) as db:
                    row = db.execute("SELECT value FROM metadata WHERE key='config_hash'").fetchone()
                if not row or row[0] != launch.config.fingerprint():
                    raise ValueError("Фильтры или размер страницы отличаются от сохранённого сбора. Загрузите его настройки или создайте новую папку.")
            output.mkdir(parents=True, exist_ok=True)
            config_file = output / "ui_config.json"
            config_file.write_text(launch.config.model_dump_json(indent=2), encoding="utf-8")
            args = ["collect", "--config", str(config_file), "--output", str(output),
                    "--max-pages", str(launch.max_pages), "--max-parts", str(launch.max_parts),
                    "--log-level", launch.log_level]
            if launch.no_export:
                args.append("--no-export")
            self._spawn(output, args, "collect")
            return self.state()

    def export(self, value):
        with self.lock:
            if self.running():
                raise ValueError("Дождитесь завершения текущего процесса")
            output = output_path(self.root, value)
            if not (output / "crawl_state.sqlite3").is_file():
                raise ValueError("В этой папке нет сохранённых данных")
            self._spawn(output, ["export", "--output", str(output)], "export")
            return self.state()

    def datasheets(self, value, max_documents=0):
        with self.lock:
            if self.running():
                raise ValueError("Дождитесь завершения текущего процесса")
            if type(max_documents) is not int or max_documents < 0:
                raise ValueError("Введите целый неотрицательный лимит PDF")
            output = output_path(self.root, value)
            if not (output / "crawl_state.sqlite3").is_file():
                raise ValueError("Сначала соберите или откройте сохранённые компоненты")
            config = load_config(output / "collection_config.json")
            if not config.groups:
                raise ValueError("Не найдена конфигурация источника")
            args = ["datasheets", "--output", str(output)]
            if max_documents:
                args.extend(("--max-documents", str(max_documents)))
            self._spawn(output, args, "datasheets")
            return self.state()

    def coverage_report(self, value):
        with self.lock:
            if self.running():
                raise ValueError("Дождитесь завершения текущего процесса")
            output = output_path(self.root, value)
            if not (output / "crawl_state.sqlite3").is_file():
                raise ValueError("Выберите сохранённый сбор")
            self._spawn(output, ["coverage", "--output", str(output), "--save", str(output / "coverage.jsonl"),
                                 "--replace"], "coverage")
            return self.state()

    def filters(self, preset, group):
        with self.lock:
            if self.running():
                raise ValueError("Дождитесь завершения текущего процесса")
            config, output = filter_output(self.root, preset, group)
            if output.is_symlink():
                raise ValueError("Путь для сохранения фильтров занят ссылкой")
            self._spawn(None, ["filters", "--config", str(config), "--group", group, "--save", str(output)], "filters")
            return self.state()

    def convert_requirements(self):
        with self.lock:
            if self.running():
                raise ValueError("Дождитесь завершения текущего процесса")
            if not any((self.root / HTML_DIRECTORY).glob("*.html")):
                raise ValueError("Положите HTML-таблицы в project_files/analytics_data/")
            self._spawn(None, ["convert-requirements"], "convert-requirements")
            return self.state()

    def import_supplement(self, value, upload):
        with self.lock:
            if self.running():
                raise ValueError("Дождитесь завершения текущего процесса")
            output = output_path(self.root, value)
            database = output / "crawl_state.sqlite3"
            if not database.is_file() or database.is_symlink():
                raise ValueError("Выберите сохранённый сбор")
            if not upload or not upload.filename.lower().endswith(".json"):
                raise ValueError("Выберите JSON-файл с дополнениями")
            matrix = load_requirements(self.root, requirements_version(self.root))
            with TemporaryDirectory() as directory:
                path = Path(directory) / "supplements.json"
                upload.save(path)
                return import_supplements(database, path, matrix)

    def delete_result(self, value):
        with self.lock:
            if self.state()["running"]:
                raise ValueError("Дождитесь завершения текущего процесса")
            output = output_path(self.root, value)
            state = output / "crawl_state.sqlite3"
            if Path(os.path.abspath(self.root / value)) != output or state.is_symlink() or not state.is_file():
                raise ValueError("Выберите папку сохранённого сбора")
            with closing(read_connection(output)) as db:
                group_keys = [key for (key,) in db.execute("SELECT key FROM groups")]

            files = [name for name in FILES if name != state.name]
            files.append("ui_config.json")
            files.extend(f"filters_{key}.json" for key in group_keys if re.fullmatch(r"[a-zA-Z0-9_-]+", key))
            try:
                pdf_folder = output / "datasheets"
                if not pdf_folder.is_symlink() and pdf_folder.is_dir():
                    for path in pdf_folder.iterdir():
                        if re.fullmatch(r"[0-9a-f]{64}\.pdf(?:\.tmp)?", path.name) and not path.is_dir():
                            path.unlink(missing_ok=True)
                    remove_empty_directory(pdf_folder)
                for name in (*files, *(f"{name}.tmp" for name in files)):
                    path = output / name
                    if not path.is_dir():
                        path.unlink(missing_ok=True)
                for suffix in ("-wal", "-shm", "-journal"):
                    state.with_name(state.name + suffix).unlink(missing_ok=True)
                state.unlink()
                remove_empty_directory(output)
            except OSError as error:
                raise ValueError(f"Не удалось удалить файлы сбора: {error.strerror}") from error
            if self.output == output:
                self.process = self.output = self.started_at = self.action = None
                self.stop_requested = False
            return self.state()

    def stop(self):
        with self.lock:
            if self.running() and not self.stop_requested:
                self.stop_requested = True
                sig = signal.SIGINT if os.name != "nt" else signal.CTRL_BREAK_EVENT
                self.process.send_signal(sig)
            return self.state()

    def shutdown(self):
        self.stop()
        if self.process and self.running():
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
        if self.log_file:
            self.log_file.close()
            self.log_file = None


def create_app(root=ROOT):
    root = Path(root).resolve()
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["MAX_CONTENT_LENGTH"] = 128 * 1024
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    token = secrets.token_urlsafe(32)
    jobs = Jobs(root)
    app.extensions["jobs"] = jobs

    @app.before_request
    def local_requests():
        # Токен нужен, чтобы чужая страница не могла запустить задачу или удалить сбор.
        if request.host.split(":")[0] not in ("127.0.0.1", "localhost"):
            abort(403)
        if request.method == "POST" and not secrets.compare_digest(request.headers.get("X-UI-Token", ""), token):
            abort(403)

    @app.errorhandler(ValueError)
    def invalid(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(sqlite3.Error)
    def database_error(error):
        return jsonify(error=f"Не удалось прочитать рабочий файл: {error}"), 409

    @app.errorhandler(RequestEntityTooLarge)
    def request_too_large(error):
        message = "Запрос слишком большой."
        if request.endpoint == "import_supplement":
            message = "Файл слишком большой. Разбейте дополнения на JSON-файлы до 32 МБ."
        return jsonify(error=message), 413

    @app.get("/", defaults={"page": "collect"})
    @app.get("/catalogue", defaults={"page": "reader"})
    @app.get("/results", defaults={"page": "results"})
    @app.get("/components", defaults={"page": "components"})
    @app.get("/sources", defaults={"page": "sources"})
    @app.get("/logs", defaults={"page": "logs"})
    def index(page):
        static = Path(app.static_folder)
        versions = {name: (static / name).stat().st_mtime_ns for name in ("style.css", "app.js")}
        return render_template("index.html", token=token, asset_versions=versions, pages=PAGES, page=page), {"Cache-Control": "no-cache"}

    @app.get("/api/configs")
    def configs():
        presets = []
        errors = []
        for path in sorted((root / "configs").rglob("*.json")):
            try:
                config = load_config(path)
                presets.append({"path": path.relative_to(root).as_posix(), "name": config.name or path.stem,
                                "config": config.model_dump()})
            except ValueError as exc:
                errors.append({"path": path.relative_to(root).as_posix(), "error": str(exc)})
        outputs = []
        for path in sorted((root / "data").rglob("crawl_state.sqlite3")):
            if not path.resolve().is_relative_to((root / "data").resolve()):
                continue
            outputs.append(path.parent.relative_to(root).as_posix())
        return jsonify(sources=list_sources(), presets=presets, outputs=outputs, errors=errors)

    @app.get("/api/sources")
    def sources():
        return jsonify(sources=list_sources())

    def reader_index():
        source = request.args.get("source", "")
        if source not in {item["key"] for item in list_sources()}:
            raise ValueError("Выберите источник каталога")
        categories, best = catalogue_index(root, source)
        return source, categories, best

    def reader_location(categories):
        category_id = request.args.get("category")
        group_id = request.args.get("group")
        category = next((item for item in categories if item["id"] == category_id), None)
        if not category or group_id not in category["groups"]:
            abort(404)
        return category_id, group_id

    @app.get("/api/reader")
    def reader():
        _, categories, _ = reader_index()
        return jsonify(categories=[{**category, "groups": list(category["groups"].values())} for category in categories])

    @app.get("/api/reader/parts")
    def reader_parts():
        source, categories, best = reader_index()
        selected = best.get(reader_location(categories))
        if not selected:
            return jsonify(rows=[], total=0, offset=0, limit=READER_PAGE_SIZE)
        _, output, group = selected
        search = request.args.get("search", "").strip()[:150]
        offset = max(0, request.args.get("offset", 0, type=int))
        assessment = Coverage(load_requirements(root, requirements_version(root)), output, load_mappings(root))
        rows, total, offset = listing(output, source, group, search, offset, assessment)
        return jsonify(rows=rows, total=total, offset=offset, limit=READER_PAGE_SIZE)

    @app.get("/api/reader/statistics")
    def reader_statistics():
        source, _, best = reader_index()
        matrix_version = requirements_version(root)
        mappings_version = file_version(root / MAPPINGS_FILE)
        groups = {}
        for (_, group_id), (_, output, group_key) in best.items():
            version = collection_version(output)
            groups[group_id] = group_statistics(root, output, source, group_key, matrix_version, mappings_version, version)
        return jsonify(groups=groups)

    @app.get("/api/reader/part")
    @app.get("/api/reader/datasheet")
    def reader_part():
        source, categories, best = reader_index()
        selected = best.get(reader_location(categories))
        if not selected:
            abort(404)
        _, output, group = selected
        pdf = request.path.endswith("/datasheet")
        assessment = None if pdf else Coverage(load_requirements(root, requirements_version(root)), output, load_mappings(root))
        row, document = component(output, source, group, request.args.get("id", ""), assessment)
        if pdf:
            if not document:
                abort(404)
            return send_file(document, mimetype="application/pdf", download_name=export_name(row["part_number"]) + ".pdf")
        if not row:
            abort(404)
        row["local_datasheet"] = document is not None
        return jsonify(row)

    @app.get("/api/reader/download/<kind>")
    def reader_download(kind):
        if kind not in FORMATS and kind != "zip":
            abort(404)
        source, categories, best = reader_index()
        if kind == "zip":
            if not best:
                abort(404)
            requirements = load_requirements(root, requirements_version(root))
            mappings = [item.model_dump(mode="json") for item in load_mappings(root) if item.source == source]
            stream = archive_catalogue(categories, best, source, requirements, mappings)
            return send_file(stream, mimetype="application/zip", as_attachment=True, download_name=f"{source}-catalogue.zip")
        category_id, group_id = reader_location(categories)
        selected = best.get((category_id, group_id))
        if not selected:
            abort(404)
        category = next(item for item in categories if item["id"] == category_id)
        name = category["groups"][group_id]["name"]
        _, output, key = selected
        stream = download_group(output, source, key, kind, name)
        return send_file(stream, mimetype=FORMATS[kind], as_attachment=True, download_name=f"{export_name(name)}.{kind}")

    @app.post("/api/presets/yageo")
    def save_yageo_preset():
        from scripts.yageo.config_from_url import save_config

        preset = PresetInput.model_validate(request.get_json())
        with jobs.lock:
            path = save_config(root, preset.key, preset.name, preset.catalogue, preset.request_url,
                               category_file=preset.category_file, category_name=preset.category_name,
                               category_key=preset.category_key)
        return jsonify(path=path.as_posix(), group=preset.key), 201

    @app.post("/api/presets/yageo/preview")
    def preview_yageo_request():
        from scripts.yageo.config_from_url import parse_request

        payload = PresetRequest.model_validate(request.get_json())
        return jsonify(query=parse_request(payload.request_url))

    @app.get("/api/status")
    def status():
        job = jobs.state()
        value = request.args.get("output", job["output"])
        tool_logs = read_log(root / TOOLS_LOG) if request.args.get("view") == "logs" else ""
        if not value:
            return jsonify(job=job, summary={"unique_components": 0, "groups": []}, files=[], logs="", tool_logs=tool_logs)
        output = output_path(root, value)
        logs = read_log(output / "ui.log")
        files = [{"name": name, "bytes": (output / name).stat().st_size}
                 for name in FILES if (output / name).is_file()]
        saved_config = output / "collection_config.json"
        last_run = output / "last_run.json"
        matrix_version = requirements_version(root)
        run_version = tuple(file_version(output / name) for name in (
            "crawl_state.sqlite3", "crawl_state.sqlite3-wal", "crawl_state.sqlite3-journal", "collection_config.json",
        ))
        mappings_version = file_version(root / MAPPINGS_FILE)
        coverage = read_coverage(root, output, matrix_version, run_version, mappings_version)
        return jsonify(job=job, summary=read_summary(output), datasheets=read_document_status(output), files=files, logs=logs,
                       tool_logs=tool_logs,
                       coverage=coverage, coverage_version=str(hash((matrix_version, run_version, mappings_version))),
                       config=json.loads(saved_config.read_text(encoding="utf-8")) if saved_config.is_file() else None,
                       last_run=json.loads(last_run.read_text(encoding="utf-8")) if last_run.is_file() else None)

    @app.get("/api/parts")
    def parts():
        output = output_path(root, request.args.get("output"))
        if not (output / "crawl_state.sqlite3").is_file():
            return jsonify(rows=[], total=0)
        offset = max(0, int(request.args.get("offset", 0)))
        search = request.args.get("search", "")[:150]
        assessment = Coverage(load_requirements(root, requirements_version(root)), output, load_mappings(root))
        with closing(read_connection(output)) as db:
            db.execute("BEGIN")
            condition = "part_number LIKE ? OR manufacturer LIKE ?" if search else ""
            clause = f"WHERE {condition}" if condition else ""
            params = [f"%{search}%", f"%{search}%"] if search else []
            total = db.execute(f"SELECT COUNT(*) FROM components {clause}", params).fetchone()[0]
            rows = []
            for row, supplement, groups in assessment_rows(db, where=condition,
                                                           params=[*params, offset], page=True):
                row["coverage"] = assessment.part(row, supplement, groups, details=True)
                row.pop("raw", None)
                row["groups"] = groups
                rows.append(row)
        return jsonify(rows=rows, total=total)

    @app.post("/api/start")
    def start():
        launch = Launch.model_validate(request.get_json())
        return jsonify(jobs.collect(launch))

    @app.post("/api/stop")
    def stop():
        return jsonify(jobs.stop())

    @app.post("/api/export")
    def export():
        return jsonify(jobs.export(request.get_json().get("output")))

    @app.post("/api/datasheets")
    def datasheets():
        data = request.get_json()
        return jsonify(jobs.datasheets(data.get("output"), data.get("max_documents", 0)))

    @app.post("/api/coverage-report")
    def coverage_report():
        return jsonify(jobs.coverage_report(request.get_json().get("output")))

    @app.post("/api/import-supplements")
    def import_supplement():
        request.max_content_length = 32 * 1024 * 1024
        return jsonify(imported=jobs.import_supplement(request.form.get("output"), request.files.get("file")))

    @app.post("/api/filters")
    def filters():
        data = request.get_json()
        return jsonify(jobs.filters(data.get("preset"), data.get("group")))

    @app.get("/api/filters")
    def saved_filters():
        _, path = filter_output(root, request.args.get("preset"), request.args.get("group"))
        if not path.is_file() or path.is_symlink():
            return jsonify(available=False)
        if request.args.get("download") == "1":
            return send_file(path, as_attachment=True)
        metadata = json.loads(path.read_text(encoding="utf-8"))
        fields = []
        if isinstance(metadata, dict):
            for name, item in metadata.items():
                if isinstance(item, dict) and isinstance(values := item.get("parameterValues"), list):
                    fields.append({"name": item.get("parameterName", name), "id": item.get("parameterId"), "count": len(values),
                                   "examples": [v.get("formattedValue") for v in values[:6] if isinstance(v, dict)]})
        return jsonify(available=True, fields=fields)

    def catalogue_scope(preset, group):
        path, _ = filter_output(root, preset, group)
        config = load_config(path)
        return config, config.group(group).query

    @app.get("/api/catalogue-mappings")
    def catalogue_mappings():
        preset = request.args.get("preset")
        source = request.args.get("source", "yageo")
        key = None
        if preset:
            config, query = catalogue_scope(preset, request.args.get("group"))
            source = config.source
            key = query_key(source, query)
        matrix_version = requirements_version(root)
        sheets = load_requirements(root, matrix_version)["sheets"]
        scopes = [item for item in load_mappings(root) if item.source == source]
        scope = next((item for item in scopes if query_key(item.source, item.query) == key), None)
        available = saved_analyst_groups(root, source, matrix_version, file_version(root / MAPPINGS_FILE)) if sheets else {}
        targets = [{
            "sheet": sheet,
            "groups": [{"name": name, "configured": group["configured"],
                        "has_data": (sheet, name) in available,
                        "complete": available.get((sheet, name), False),
                        "fields": [{"name": field, **rule} for field, rule in group["fields"].items()]}
                       for name, group in groups.items()],
        } for sheet, groups in sheets.items()]
        return jsonify(targets=targets, scopes=[item.model_dump(mode="json") for item in scopes],
                       scope=scope.model_dump(mode="json") if scope else None)

    @app.post("/api/catalogue-mappings")
    def update_catalogue_mapping():
        data = MappingInput.model_validate(request.get_json())
        with jobs.lock:
            config, query = catalogue_scope(data.preset, data.catalogue_group)
            source = config.source
            scopes = load_mappings(root)
            key = query_key(source, query)
            scope = next((item for item in scopes if query_key(item.source, item.query) == key), None)
            if scope and (scope.sheet, scope.group) != (data.sheet, data.group):
                raise ValueError("У этой подкатегории уже есть правила для другой группы аналитиков")
            if data.mapping is None:
                if not scope or data.field not in scope.fields:
                    raise ValueError("Правило не найдено")
                del scope.fields[data.field]
                if not scope.fields:
                    scopes.remove(scope)
            else:
                sheets = load_requirements(root, requirements_version(root))["sheets"]
                target = sheets.get(data.sheet, {}).get(data.group)
                if not target or not target["configured"] or target["fields"].get(data.field, {}).get("state") != "required":
                    raise ValueError("Выберите обязательное поле из настроенной группы аналитиков")
                if not scope:
                    scope = MappingScope(source=source, query=query, sheet=data.sheet, group=data.group, fields={})
                    scopes.append(scope)
                scope.fields[data.field] = data.mapping
            write_mappings(root, scopes)
        return jsonify(saved=data.mapping is not None)

    @app.post("/api/requirements")
    def convert_requirements():
        return jsonify(jobs.convert_requirements())

    @app.get("/api/requirements")
    def requirements():
        version = requirements_version(root)
        matrix = load_requirements(root, version)
        if request.args.get("download") == "1":
            response = app.response_class(
                json.dumps(matrix, ensure_ascii=False, indent=2) + "\n",
                mimetype="application/json",
            )
            response.headers["Content-Disposition"] = 'attachment; filename="requirements.json"'
            response.cache_control.no_store = True
            return response
        sheets = [{"name": name, "total": len(groups), "configured": sum(group["configured"] for group in groups.values())}
                  for name, groups in matrix["sheets"].items()]
        return jsonify(compiled=(root / REQUIREMENTS_FILE).is_file(), sheets=sheets)

    @app.post("/api/delete-result")
    def delete_result():
        return jsonify(jobs.delete_result(request.get_json().get("output")))

    @app.get("/api/download/<name>")
    def download(name):
        if name not in FILES:
            abort(404)
        output = output_path(root, request.args.get("output"))
        if jobs.running() and output == jobs.output and name == "crawl_state.sqlite3":
            return jsonify(error="Дождитесь завершения записи рабочего файла"), 409
        path = output / name
        if not path.is_file() or not path.resolve().is_relative_to(output):
            abort(404)
        return send_file(path, as_attachment=True)

    return app


def main():
    parser = argparse.ArgumentParser(description="Local Nord UI for the EKB parser")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    app = create_app()
    atexit.register(app.extensions["jobs"].shutdown)
    app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()

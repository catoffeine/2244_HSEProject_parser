from contextlib import closing
from io import BytesIO
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from zipfile import ZipFile

from ekb_parser.core.catalogue_mappings import MAPPINGS_FILE
from ekb_parser.core.config import CollectionConfig, load_config
from ekb_parser.core.coverage import import_supplements
from ekb_parser.core.requirements import HTML_DIRECTORY, REQUIREMENTS_FILE, write_requirements
from ekb_parser.core.storage import Store
from ekb_parser.sources.yageo.adapter import normalize
from ekb_parser.web.app import create_app


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        category = load_config("configs/yageo/capacitors.json")
        config = category.model_copy(update={"groups": [category.group("aluminum")]})
        folder = self.root / "configs/yageo"
        folder.mkdir(parents=True)
        (folder / "capacitors.json").write_text(category.model_dump_json())
        mappings = self.root / MAPPINGS_FILE
        mappings.parent.mkdir(parents=True)
        mappings.write_bytes(Path(MAPPINGS_FILE).read_bytes())
        output = self.root / "data/yageo/runs/example"
        store = Store(output)
        store.initialize(config)
        store.close()
        self.app = create_app(self.root)
        self.client = self.app.test_client()
        page = self.client.get("/").get_data(as_text=True)
        self.token = re.search(r'name="ui-token" content="([^"]+)"', page).group(1)

    def tearDown(self):
        self.app.extensions["jobs"].shutdown()
        self.tmp.cleanup()

    def test_nested_presets_and_invalid_config_do_not_hide_valid_presets(self):
        (self.root / "configs/yageo/bad.json").write_text("{}")
        response = self.client.get("/api/configs")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json["presets"]), 1)
        self.assertEqual(len(response.json["errors"]), 1)
        self.assertEqual(response.json["sources"][0]["key"], "yageo")
        self.assertIn("data/yageo/runs/example", response.json["outputs"])

    def test_each_workspace_route_opens_its_own_view(self):
        pages = {"/catalogue": "reader", "/": "collect", "/results": "results", "/components": "components",
                 "/sources": "sources", "/logs": "logs"}
        for path, page in pages.items():
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn(f'<body data-page="{page}">', response.text)
                self.assertIn(f'data-view="{page}">', response.text)
                for inactive in set(pages.values()) - {page}:
                    self.assertIn(f'data-view="{inactive}" hidden', response.text)
                self.assertNotIn('href="#results"', response.text)

    def test_reader_selects_largest_group_and_exports_only_its_records(self):
        category = load_config(self.root / "configs/yageo/capacitors.json")
        write_requirements(HTML_DIRECTORY, self.root / REQUIREMENTS_FILE)

        def add_run(name, specs):
            groups = [category.group(key) for key in specs]
            if name == "same-key-different-query":
                groups = [groups[0].model_copy(update={"query": {"1": 1, "79": [397], "3": [123]}})]
            store = Store(self.root / "data/yageo/runs" / name)
            store.initialize(category.model_copy(update={"groups": groups}))
            for key, ids in specs.items():
                parts = [normalize({"basePartId": i, "basePn": f"PART-{i}", "manufacturer": "TEST",
                                    "parameterValues": [{"parameterId": 3, "value": "Sample series"},
                                                        {"parameterId": 79, "value": "Aluminum" if i == 101 else "Unknown"},
                                                        {"parameterId": 6, "value": "0.000033"}]},
                                   "https://yageogroup.com/list") for i in ids]
                store.save_page(key, 0, parts)
            store.close()

        add_run("best-aluminum", {"aluminum": [101, 102], "ceramic": [201]})
        add_run("best-ceramic", {"aluminum": [103], "ceramic": [202, 203, 204]})
        add_run("same-key-different-query", {"aluminum": [301, 302, 303, 304]})
        source = {"source": "yageo"}
        result = self.client.get("/api/reader", query_string=source)
        self.assertEqual(result.status_code, 200)
        categories = {category["name"]: category for category in result.json["categories"]}
        capacitor = categories["Конденсаторы"]
        groups = {group["name"]: group for group in capacitor["groups"]}
        aluminum = groups["Конденсаторы оксидно-электролитические"]
        self.assertEqual(aluminum["count"], 2)
        self.assertEqual(groups["Конденсаторы керамические"]["count"], 3)
        self.assertEqual(categories["Другие сохранённые группы"]["groups"][0]["count"], 4)
        selection = {**source, "category": capacitor["id"], "group": aluminum["id"]}
        rows = self.client.get("/api/reader/parts", query_string={**selection, "search": "sample series"}).json["rows"]
        self.assertEqual([row["part_number"] for row in rows], ["PART-101", "PART-102"])
        self.assertEqual([row["coverage"]["percent"] for row in rows], [33.3, None])
        stats = self.client.get("/api/reader/statistics", query_string=source).json["groups"]
        self.assertEqual(stats[aluminum["id"]], {
            "total": 2, "complete": 0, "partial": 1, "empty": 0, "unassessed": 1,
            "filled": 3, "required": 9, "with_datasheet": 0,
        })
        detail = "/api/reader/part"
        self.assertEqual(self.client.get(detail, query_string={**selection, "id": "101"}).json["parameters"][0]["value"], "Sample series")
        self.assertEqual(self.client.get(detail, query_string={**selection, "id": "201"}).status_code, 404)
        for kind in ("csv", "jsonl", "sqlite", "zip"):
            with self.subTest(kind=kind):
                response = self.client.get("/api/reader/download/" + kind, query_string=source if kind == "zip" else selection)
                self.assertEqual(response.status_code, 200)
                with response:
                    content = response.data
                if kind in ("csv", "jsonl"):
                    self.assertIn(b"PART-101", content)
                    self.assertNotIn(b"PART-201", content)
                elif kind == "sqlite":
                    with closing(sqlite3.connect(":memory:")) as database:
                        database.deserialize(content)
                        self.assertEqual(database.execute("SELECT part_number FROM components ORDER BY part_number").fetchall(),
                                         [("PART-101",), ("PART-102",)])
                else:
                    with ZipFile(BytesIO(content)) as archive:
                        manifest = json.loads(archive.read("index.json"))
                        self.assertEqual(sorted(entry["count"] for entry in manifest), [2, 3, 4])
                        for entry in manifest:
                            files = [name for name in archive.namelist() if name.startswith(entry["folder"] + "/")]
                            self.assertEqual(sorted(Path(name).suffix for name in files), [".csv", ".jsonl", ".sqlite"])

    def test_tool_actions_use_validated_paths_and_cli_arguments(self):
        preset = "configs/yageo/capacitors.json"
        output = "data/yageo/runs/example"
        headers = {"X-UI-Token": self.token}
        (self.root / HTML_DIRECTORY).mkdir(parents=True)
        (self.root / HTML_DIRECTORY / "Пример.html").touch()
        cases = [
            ("/api/filters", {"preset": preset, "group": "aluminum"}, "filters", "--save"),
            ("/api/requirements", {}, "convert-requirements", None),
            ("/api/coverage-report", {"output": output}, "coverage", "--replace"),
            ("/api/datasheets", {"output": output, "max_documents": 2}, "datasheets", "--max-documents"),
        ]
        with patch("ekb_parser.web.app.subprocess.Popen") as popen:
            process = MagicMock()
            popen.return_value = process
            for endpoint, payload, command, option in cases:
                with self.subTest(endpoint=endpoint):
                    process.poll.return_value = 0
                    response = self.client.post(endpoint, json=payload, headers=headers)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json["action"], command)
                    args = popen.call_args.args[0]
                    self.assertEqual(args[2:5], ["-m", "ekb_parser", command])
                    if option:
                        self.assertIn(option, args)
            response = self.client.post("/api/filters", json={"preset": "../outside.json", "group": "aluminum"}, headers=headers)
            self.assertEqual(response.status_code, 400)
            self.assertEqual(popen.call_count, len(cases))

    def test_requirements_download_matches_formatted_matrix(self):
        path = self.root / REQUIREMENTS_FILE
        matrix = write_requirements(HTML_DIRECTORY, path)
        response = self.client.get("/api/requirements?download=1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Content-Disposition"], 'attachment; filename="requirements.json"')
        self.assertTrue(response.cache_control.no_store)
        self.assertEqual(response.json, matrix)
        self.assertEqual(response.text, path.read_text(encoding="utf-8"))

    def test_catalogue_mapping_assesses_existing_collection_and_can_be_removed(self):
        matrix = write_requirements(HTML_DIRECTORY, self.root / REQUIREMENTS_FILE)
        sheet, group = "Дроссели, индуктивности", "Дроссели высокочастотные"
        field = "Максимально допустимый ток, А"
        self.assertEqual(matrix["sheets"][sheet][group]["fields"][field]["state"], "required")
        config = CollectionConfig.model_validate({
            "options": {"catalogue_category": "Inductors"},
            "groups": [{"key": "rf", "name": "Высокочастотные", "query": {"1": 99901, "79": [99902]}},
                       {"key": "other", "name": "Другие", "query": {"1": 99901, "79": [99903]}}],
        })
        preset = "configs/yageo/inductors.json"
        (self.root / preset).write_text(config.model_dump_json())
        output = "data/yageo/runs/inductors"
        store = Store(self.root / output)
        store.initialize(config)
        for identifier, key in enumerate(("rf", "other"), 1):
            store.save_page(key, 0, [normalize({
                "basePartId": identifier, "basePn": f"T{identifier}", "manufacturer": "TEST",
                "parameterValues": [{"parameterId": 999, "parameterName": "Rated Current", "value": 250}],
            }, "https://yageogroup.com/list", "Inductors")])
        store.close()
        endpoint = "/api/catalogue-mappings"
        def has_group_data():
            response = self.client.get(endpoint, query_string={"source": "yageo"})
            self.assertEqual(response.status_code, 200)
            target = next(item for item in response.json["targets"] if item["sheet"] == sheet)
            return next(item["has_data"] for item in target["groups"] if item["name"] == group)

        headers = {"X-UI-Token": self.token}
        rule = {"preset": preset, "catalogue_group": "rf", "sheet": sheet, "group": group, "field": field,
                "mapping": {"parameter_id": 999, "parameter_name": "Rated Current", "factor": "0.001"}}
        version_before = self.client.get("/api/status", query_string={"output": output}).json["coverage_version"]
        self.assertFalse(has_group_data())
        self.assertEqual(self.client.post(endpoint, json=rule).status_code, 403)
        self.assertEqual(self.client.post(endpoint, json=rule, headers=headers).status_code, 200)
        self.assertTrue((self.root / MAPPINGS_FILE).is_file())
        self.assertTrue(has_group_data())
        editor = self.client.get(endpoint, query_string={"preset": preset, "group": "rf"}).json
        self.assertEqual(editor["scope"]["fields"][field], rule["mapping"])
        target = next(item for item in editor["targets"] if item["sheet"] == sheet)
        fields = next(item["fields"] for item in target["groups"] if item["name"] == group)
        self.assertIn({"name": field, "state": "required"}, fields)
        self.assertTrue(any(item["state"] == "fixed" for item in fields))
        status = self.client.get("/api/status", query_string={"output": output}).json
        self.assertNotEqual(status["coverage_version"], version_before)
        self.assertEqual((status["coverage"]["assessed"], status["coverage"]["unassessed"]), (1, 1))
        rows = self.client.get("/api/parts", query_string={"output": output}).json["rows"]
        value = next(item for item in rows[0]["coverage"]["fields"] if item["name"] == field)
        self.assertEqual((value["value"], value["origin"]), ("0.250", "catalogue"))
        self.assertEqual(rows[1]["coverage"]["state"], "unmapped")
        deferred = {**rule, "mapping": {"deferred": True}}
        self.assertEqual(self.client.post(endpoint, json=deferred, headers=headers).status_code, 200)
        pending = self.client.get("/api/status", query_string={"output": output}).json["coverage"]
        self.assertEqual(pending["required"], status["coverage"]["required"])
        self.assertEqual(pending["filled"], status["coverage"]["filled"] - 1)
        part = self.client.get("/api/parts", query_string={"output": output}).json["rows"][0]["coverage"]
        value = next(item for item in part["fields"] if item["name"] == field)
        self.assertIsNone(value["value"])
        self.assertTrue(value["deferred"])

        supplement = [{"source": "yageo", "source_id": "1",
                       "fields": {field: {"value": 0.3, "evidence": "Паспорт, стр. 2"}}}]
        response = self.client.post("/api/import-supplements", headers=headers, data={
            "output": output, "file": (BytesIO(json.dumps(supplement).encode()), "supplement.json"),
        })
        self.assertEqual(response.status_code, 200)
        part = self.client.get("/api/parts", query_string={"output": output}).json["rows"][0]["coverage"]
        value = next(item for item in part["fields"] if item["name"] == field)
        self.assertEqual((value["value"], value["origin"]), (0.3, "supplement"))
        self.assertNotIn("deferred", value)
        self.assertEqual(self.client.post(endpoint, json={**rule, "mapping": None}, headers=headers).status_code, 200)
        status = self.client.get("/api/status", query_string={"output": output}).json
        self.assertEqual(status["coverage"]["assessed"], 0)
        self.assertFalse(has_group_data())

    def test_catalogue_mapping_table_lists_saved_capacitor_fields(self):
        write_requirements(HTML_DIRECTORY, self.root / REQUIREMENTS_FILE)
        endpoint = "/api/catalogue-mappings"
        selection = {"preset": "configs/yageo/capacitors.json", "group": "aluminum"}
        response = self.client.get(endpoint, query_string=selection)
        self.assertEqual(response.status_code, 200)
        scope = response.json["scope"]
        self.assertEqual(scope["group"], "Конденсаторы оксидно-электролитические")
        fields = scope["fields"]
        self.assertEqual(fields["Номинальная емкость, Ф"],
                         {"parameter_id": 6, "parameter_name": "Capacitance", "factor": "1"})
        self.assertEqual(fields["Номинальное напряжение, В"]["parameter_id"], 10)
        self.assertNotIn("Рабочее напряжение, В", fields)
        matrix = self.client.get(endpoint, query_string={"source": "yageo"}).json
        self.assertIn(scope, matrix["scopes"])
        sheets = {sheet["sheet"]: sheet["groups"] for sheet in matrix["targets"]}
        self.assertIn("Резисторы", sheets)
        aluminum = next(group for group in sheets["Конденсаторы"] if group["name"] == scope["group"])
        states = {field["name"]: field["state"] for field in aluminum["fields"]}
        self.assertEqual(states["Номинальная емкость, Ф"], "required")
        self.assertEqual(states["Приемка"], "fixed")
        self.assertIn("excluded", states.values())
        removal = self.client.post(endpoint, headers={"X-UI-Token": self.token}, json={
            "preset": selection["preset"], "catalogue_group": "aluminum", "sheet": scope["sheet"],
            "group": scope["group"], "field": "Номинальная емкость, Ф", "mapping": None,
        })
        self.assertEqual(removal.status_code, 200)
        remaining = self.client.get(endpoint, query_string=selection).json["scope"]["fields"]
        self.assertNotIn("Номинальная емкость, Ф", remaining)
        self.assertIn("Номинальное напряжение, В", remaining)

    def test_yageo_groups_are_saved_in_the_same_category_file(self):
        from ekb_parser.sources.yageo.adapter import page_url

        preset = {"key": "test-preset", "name": "Тестовая подкатегория",
                  "category_name": "Резисторы YAGEO", "category_key": "yageo-resistors",
                  "catalogue": "https://yageogroup.com/products/Resistors",
                  "request_url": page_url({"1": 1605, "79": [1655], "279": [365395]}, 25, 25)}
        headers = {"X-UI-Token": self.token}
        endpoint = "/api/presets/yageo"
        relative_url = preset["request_url"].removeprefix("https://yageogroup.com")
        preview = self.client.post(endpoint + "/preview", json={"request_url": relative_url}, headers=headers)
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json, {"query": {"1": 1605, "79": [1655], "279": [365395]}})
        self.assertFalse((self.root / "configs/yageo/yageo-resistors.json").exists())

        response = self.client.post(endpoint, json=preset, headers=headers)
        self.assertEqual(response.status_code, 201)
        path = response.json["path"]
        self.assertEqual(path, "configs/yageo/yageo-resistors.json")
        config = load_config(self.root / path)
        self.assertEqual(config.name, "Резисторы YAGEO")
        self.assertEqual(config.groups[0].query, preview.json["query"])
        second = {"category_file": path, "key": "second", "name": "Тонкоплёночные",
                  "request_url": page_url({"1": 1605, "79": [1607]}, 0, 25)}
        another = self.client.post(endpoint, json=second, headers=headers)
        self.assertEqual(another.status_code, 201)
        self.assertEqual(another.json, {"path": path, "group": "second"})
        self.assertEqual([group.key for group in load_config(self.root / path).groups], ["test-preset", "second"])

        for invalid in [
            second,
            {**second, "key": "wrong-category", "request_url": page_url({"1": 1, "79": [397]}, 0, 25)},
            {**second, "key": "invalid-url", "request_url": "/search/query/base-part/%7B"},
            {**preset, "category_key": "duplicate-category"},
        ]:
            with self.subTest(invalid=invalid["key"]):
                self.assertEqual(self.client.post(endpoint, json=invalid, headers=headers).status_code, 400)
        self.assertFalse((self.root / "configs/yageo/duplicate-category.json").exists())
        self.assertEqual([group.key for group in load_config(self.root / path).groups], ["test-preset", "second"])

    def test_datasheet_action_uses_same_cli_without_shell(self):
        with patch("ekb_parser.web.app.subprocess.Popen") as popen:
            process = MagicMock()
            process.poll.return_value = None
            popen.return_value = process
            response = self.client.post("/api/datasheets", json={"output": "data/yageo/runs/example"},
                                        headers={"X-UI-Token": self.token})
            self.assertEqual(response.status_code, 200)
            command = popen.call_args.args[0]
            self.assertEqual(command[2:5], ["-m", "ekb_parser", "datasheets"])
            self.assertNotIn("shell", popen.call_args.kwargs)
            process.poll.return_value = 0

    def test_mutations_require_token_and_paths_stay_inside_data(self):
        self.assertEqual(self.client.post("/api/datasheets", json={"output": "data/x"}).status_code, 403)
        self.assertEqual(self.client.get("/api/status?output=../outside").status_code, 400)

    def test_delete_result_removes_generated_files_and_keeps_other_data(self):
        relative_output = "data/yageo/runs/example"
        output = self.root / relative_output
        pdfs = output / "datasheets"
        pdfs.mkdir()
        (pdfs / ("a" * 64 + ".pdf")).write_bytes(b"%PDF-test")
        (output / "filters_aluminum.json").write_text("{}")
        (output / "parts.csv.tmp").write_text("temporary")
        (output / "notes.txt").write_text("keep")
        nested = Store(output / "another-run")
        nested.close()

        response = self.client.post("/api/delete-result", json={"output": relative_output},
                                    headers={"X-UI-Token": self.token})
        self.assertEqual(response.status_code, 200)
        self.assertEqual({path.name for path in output.iterdir()}, {"notes.txt", "another-run"})
        self.assertEqual(self.client.get("/api/configs").json["outputs"], [relative_output + "/another-run"])

    def test_delete_result_rejects_active_job_paths_and_missing_token(self):
        output = "data/yageo/runs/example"
        state = self.root / output / "crawl_state.sqlite3"
        headers = {"X-UI-Token": self.token}
        jobs = self.app.extensions["jobs"]
        self.assertEqual(self.client.post("/api/delete-result", json={"output": output}).status_code, 403)
        for value in ("data", "../outside", "data/yageo/runs/missing"):
            response = self.client.post("/api/delete-result", json={"output": value}, headers=headers)
            self.assertEqual(response.status_code, 400)
        with patch.object(jobs, "running", return_value=True):
            response = self.client.post("/api/delete-result", json={"output": output}, headers=headers)
        self.assertEqual(response.status_code, 400)
        self.assertTrue(state.is_file())

    def test_coverage_uses_catalogue_and_supplements_for_all_rows(self):
        matrix = write_requirements(HTML_DIRECTORY, self.root / REQUIREMENTS_FILE)
        relative_output = "data/yageo/runs/example"
        output = self.root / relative_output
        store = Store(output)
        records = [normalize({"basePartId": i, "basePn": f"P{i}", "manufacturer": "KEMET",
                              "parameterValues": [{"parameterId": 79, "value": kind},
                                                  {"parameterId": 6, "value": "0.000033"},
                                                  {"parameterId": 10, "value": "10"}]},
                             "https://yageogroup.com/list")
                   for i, kind in enumerate(("Aluminum", "Supercapacitors"), start=1)]
        store.save_page("aluminum", 0, records)
        store.close()

        def coverage():
            response = self.client.get("/api/status", query_string={"output": relative_output})
            self.assertEqual(response.status_code, 200)
            return response.json["coverage"]

        initial = coverage()
        self.assertEqual((initial["filled"], initial["required"], initial["unassessed"]), (4, 9, 1))
        supplement = self.root / "supplements.json"
        observation = {"source": "yageo", "source_id": "1",
                       "fields": {"Tмн, ч": {"value": 20000, "evidence": "Report, p. 4"}}}
        supplement.write_text(json.dumps([observation, dict(observation, source_id="missing")]), encoding="utf-8")
        database = output / "crawl_state.sqlite3"
        with self.assertRaises(ValueError):
            import_supplements(database, supplement, matrix)
        self.assertEqual(coverage(), initial)
        response = self.client.post("/api/import-supplements",
                                    data={"output": relative_output, "file": (BytesIO(json.dumps([observation]).encode()), "aa.json")},
                                    headers={"X-UI-Token": self.token})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["imported"], 1)
        self.assertEqual(coverage()["percent"], 55.6)
        response = self.client.get("/api/parts", query_string={"output": relative_output, "search": "P1"})
        part = response.json["rows"][0]["coverage"]
        self.assertEqual(part["percent"], 55.6)
        field = next(field for field in part["fields"] if field["name"] == "Tмн, ч")
        self.assertEqual((field["value"], field["origin"], field["evidence"]), (20000, "supplement", "Report, p. 4"))
        removal = self.client.post("/api/catalogue-mappings", headers={"X-UI-Token": self.token}, json={
            "preset": "configs/yageo/capacitors.json", "catalogue_group": "aluminum",
            "sheet": "Конденсаторы", "group": "Конденсаторы оксидно-электролитические",
            "field": "Номинальная емкость, Ф", "mapping": None,
        })
        self.assertEqual(removal.status_code, 200)
        self.assertEqual(coverage()["filled"], 4)
        part = self.client.get("/api/parts", query_string={"output": relative_output, "search": "P1"}).json["rows"][0]
        capacitance = next(field for field in part["coverage"]["fields"] if field["name"] == "Номинальная емкость, Ф")
        self.assertIsNone(capacitance["value"])

    def test_datasheet_status_counts_unique_pending_urls_and_missing_files(self):
        relative_output = "data/yageo/runs/example"
        output = self.root / relative_output
        store = Store(output)
        self.addCleanup(store.close)
        records = [normalize({"basePartId": i, "basePn": f"P{i}", "manufacturer": "KEMET", "dataSheet": doc},
                             "https://yageogroup.com/list")
                   for i, doc in enumerate(("saved", "saved", "failed", "new", None), start=1)]
        store.save_page("aluminum", 0, records)
        pdf = output / "datasheets" / "saved.pdf"
        pdf.parent.mkdir()
        pdf.write_bytes(b"%PDF-test")
        store.save_document("yageo", records[0]["datasheet_url"], sha256="saved",
                            local_path="datasheets/saved.pdf")
        store.save_document("yageo", records[2]["datasheet_url"], error="timeout")

        def status():
            response = self.client.get("/api/status", query_string={"output": relative_output})
            self.assertEqual(response.status_code, 200)
            return response.json["datasheets"]

        self.assertEqual(status(), {"pending": 2, "downloaded": 1, "failed": 1, "unique_files": 1})
        pdf.unlink()
        self.assertEqual(status(), {"pending": 3, "downloaded": 0, "failed": 1, "unique_files": 0})
        store.db.execute("DROP TABLE datasheets")
        self.assertEqual(status(), {"pending": 3, "downloaded": 0, "failed": 0, "unique_files": 0})

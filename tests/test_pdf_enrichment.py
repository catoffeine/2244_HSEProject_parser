from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from ekb_parser.core.config import load_config
from ekb_parser.core.coverage import Coverage, observation_table_exists
from ekb_parser.core.pdf_enrichment import ApprovedRule, apply_candidates, prepare_tasks
from ekb_parser.core.storage import Store
from ekb_parser.sources.yageo.adapter import normalize


class PDFEnrichmentTests(unittest.TestCase):
    def test_queue_preview_commit_and_repeat(self):
        group = ["Конденсаторы", "Конденсаторы оксидно-электролитические"]
        field = "Номинальное напряжение, В"
        matrix = {"sheets": {group[0]: {group[1]: {"configured": True, "fields": {
            field: {"state": "required"},
            "Приемка": {"state": "fixed", "value": "1"},
        }}}}}
        with TemporaryDirectory() as temporary:
            output = Path(temporary)
            store = Store(output)
            self.addCleanup(store.close)
            config = load_config("configs/yageo/capacitors.json")
            store.initialize(config.model_copy(update={"groups": [config.group("aluminum")]}))
            records = [normalize({
                "basePartId": i, "basePn": f"TEST-{i}", "manufacturer": "TEST", "dataSheet": document,
                "parameterValues": [{"parameterId": 79, "value": "Aluminum"}],
            }, "https://yageogroup.com/list") for i, document in [(1, "saved"), (2, "missing")]]
            store.save_page("aluminum", 0, records)
            contents = b"%PDF-1.7\n"
            pdf = output / "saved.pdf"
            pdf.write_bytes(contents)
            url = records[0]["datasheet_url"]
            digest = sha256(contents).hexdigest()
            store.save_document("yageo", url, sha256=digest, local_path=pdf.name)
            assessment = Coverage(matrix, output)
            queue = StringIO()
            self.assertEqual(prepare_tasks(output, assessment, queue), 1)
            task = json.loads(queue.getvalue())
            target = task["target"]
            self.assertEqual((target["source_id"], target["field"], task["pdf_path"]), ("1", field, pdf.name))
            candidate = {"version": 1, "target": target, "rule_id": "test:voltage@1",
                         "value": 16, "evidence": {"page": 1, "quote": "TEST-1    16 VDC"}}
            rule = ApprovedRule(version=1, rule_id=candidate["rule_id"], source="yageo", group=group,
                                field=field, document_sha256=digest)

            def run(*items, rules=(rule,), commit=False):
                batch = StringIO("\n".join(json.dumps(item) for item in items))
                report = StringIO()
                result = apply_candidates(output, assessment, batch, report, rules, commit=commit)
                return result, [json.loads(line) for line in report.getvalue().splitlines()]

            self.assertEqual(run(candidate, rules=(), commit=True)[0], {"rule_not_approved": 1})
            self.assertEqual(run(candidate, {**candidate, "value": 20}, commit=True)[0], {"conflict": 2})
            self.assertEqual(run(candidate, candidate)[0], {"ready": 1, "duplicate": 1})
            self.assertFalse(observation_table_exists(store.db))
            pdf.write_bytes(contents + b"modified")
            self.assertEqual(run(candidate, commit=True)[0], {"pdf_changed_or_missing": 1})
            pdf.write_bytes(contents)
            summary, report = run(candidate, {"bad": True}, commit=True)
            self.assertEqual(summary, {"applied": 1, "invalid": 1})
            self.assertEqual(report[-1]["evidence"], candidate["evidence"])
            row = store.db.execute("SELECT payload FROM component_supplements WHERE source_id='1'").fetchone()
            supplement = json.loads(row[0])
            filled = assessment.part(records[0], supplement, ("aluminum",), details=True)
            self.assertEqual(filled["percent"], 100)
            self.assertEqual(filled["fields"][0]["pdf"]["sha256"], digest)
            self.assertEqual(run(candidate, commit=True)[0], {"already_filled": 1})
            self.assertEqual(prepare_tasks(output, assessment, StringIO()), 0)

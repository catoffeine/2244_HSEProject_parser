import asyncio
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from pypdf import PdfWriter
from scrapy.http import Response

from ekb_parser.core.config import CollectionConfig
from ekb_parser.core.documents import DatasheetSpider
from ekb_parser.core.storage import Store
from ekb_parser.sources.registry import get_adapter
from ekb_parser.sources.yageo.adapter import normalize


class DocumentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        config = CollectionConfig.model_validate({"groups": [{"key": "a", "name": "A", "query": {"1": 1}}]})
        self.store.initialize(config)
        records = [normalize({"basePartId": n, "basePn": f"P{n}", "manufacturer": "KEMET",
                              "dataSheet": f"doc_{n}"}, "https://yageogroup.com/list") for n in (1, 2)]
        self.urls = [r["datasheet_url"] for r in records]
        self.store.save_page("a", 0, records, complete=True)
        self.spider = DatasheetSpider(self.store, get_adapter("yageo"))
        self.spider.crawler = SimpleNamespace(stats=SimpleNamespace(inc_value=lambda *a: None))
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        stream = io.BytesIO()
        writer.write(stream)
        self.pdf = stream.getvalue()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def save(self, url):
        self.spider.save_pdf(Response(url=url, body=self.pdf), "yageo", url)

    def test_identical_content_is_one_file_with_two_references(self):
        for url in self.urls:
            self.save(url)
        self.assertEqual(len(list((Path(self.tmp.name) / "datasheets").glob("*.pdf"))), 1)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM datasheets WHERE status='downloaded'").fetchone()[0], 2)
        self.store.export_documents()
        rows = [json.loads(s) for s in (Path(self.tmp.name) / "datasheet_downloads.jsonl").read_text().splitlines()]
        self.assertEqual(rows[0]["local_path"], rows[1]["local_path"])
        self.assertEqual(self.store.progress("a")["next_offset"], 2)

    def test_resume_skips_existing_pdf_and_retries_missing_file(self):
        for url in self.urls:
            self.save(url)
        async def requests():
            return [r async for r in self.spider.start()]
        self.assertEqual(asyncio.run(requests()), [])
        self.assertEqual(self.spider.skipped, 2)
        next((Path(self.tmp.name) / "datasheets").glob("*.pdf")).unlink()
        self.assertEqual(len(asyncio.run(requests())), 2)

    def test_html_error_is_not_saved_as_pdf(self):
        self.spider.save_pdf(Response(url=self.urls[0], body=b"<html>Not a PDF</html>"), "yageo", self.urls[0])
        self.assertFalse((Path(self.tmp.name) / "datasheets").exists())
        self.assertEqual(self.store.db.execute("SELECT status FROM datasheets").fetchone()[0], "failed")
        self.assertEqual(self.store.count(), 2)

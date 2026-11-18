import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from scrapy.http import TextResponse
from scrapy.exceptions import CloseSpider

from ekb_parser.sources.yageo.adapter import normalize, page_url
from ekb_parser.core.config import CollectionConfig
from ekb_parser.core.spider import CatalogSpider
from ekb_parser.core.storage import Store


def row(number):
    return {"basePartId": number, "basePn": f"P{number}", "manufacturer": "Example",
            "dataSheet": "one_document", "parameterValues": [
                {"parameterId": 6, "parameterName": "Capacitance", "value": "0.000033", "formattedValue": "33 uF"}]}


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = CollectionConfig.model_validate({"groups": [
            {"key": "a", "name": "A", "query": {"1": 1, "79": [397]}},
            {"key": "b", "name": "B", "query": {"1": 1, "79": [397], "3": [23269]}}]})
        self.store = Store(self.tmp.name)
        self.store.initialize(self.config)
        self.store.set_expected("a", 3)
        self.store.set_expected("b", 1)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def records(self, *numbers):
        return [normalize(row(n), "https://example.test/page") for n in numbers]

    def test_restart_and_cross_group_deduplication(self):
        self.store.save_page("a", 0, self.records(1, 2))
        self.store.close()
        self.store = Store(self.tmp.name)
        self.store.initialize(self.config)
        self.assertEqual(self.store.progress("a")["next_offset"], 2)
        self.store.save_page("a", 2, self.records(3), complete=True)
        self.store.save_page("b", 0, self.records(1), complete=True)
        self.assertEqual(self.store.count(), 3)
        self.store.export()
        records = [json.loads(s) for s in (Path(self.tmp.name) / "parts.jsonl").read_text().splitlines()]
        self.assertEqual(records[0]["groups"], ["a", "b"])
        self.assertEqual(len((Path(self.tmp.name) / "documents.jsonl").read_text().splitlines()), 1)
        self.assertEqual(len((Path(self.tmp.name) / "articles.txt").read_text().splitlines()), 3)

    def test_invalid_row_rolls_back_entire_page_and_offset(self):
        records = self.records(1, 2)
        records[1]["manufacturer"] = None  # Вторая запись должна прервать сохранение всей страницы.
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.save_page("a", 0, records)
        self.assertEqual(self.store.count(), 0)
        self.assertEqual(self.store.progress("a")["next_offset"], 0)

    def test_empty_or_overlapping_page_never_advances_checkpoint(self):
        self.store.save_page("a", 0, self.records(1))
        for records in ([], self.records(1), self.records(2, 2)):
            with self.assertRaises(ValueError):
                self.store.save_page("a", 1, records)
        self.assertEqual(self.store.progress("a")["next_offset"], 1)

    def test_different_filters_cannot_resume_same_output(self):
        changed = self.config.model_copy(deep=True)
        changed.groups[0].query["79"] = [558]
        with self.assertRaises(ValueError):
            self.store.initialize(changed)

    def test_max_parts_mid_page_keeps_exact_next_offset(self):
        spider = CatalogSpider(self.config, self.store, max_pages=0, max_parts=2)
        spider.crawler = SimpleNamespace(stats=SimpleNamespace(inc_value=lambda *args: None))
        response = TextResponse(url="https://example.test/page", body=json.dumps([row(1), row(2), row(3)]).encode(), encoding="utf-8")
        self.assertIsNone(spider.parse_page(response, index=0, offset=0))
        self.assertEqual(self.store.count(), 2)
        self.assertEqual(self.store.progress("a")["next_offset"], 2)
        self.assertFalse(self.store.progress("a")["complete"])
        self.assertEqual(spider.stop_reason, "max_parts")

    def test_empty_advertised_nonempty_page_is_collection_error(self):
        spider = CatalogSpider(self.config, self.store)
        response = TextResponse(url="https://example.test/page", body=b"[]", encoding="utf-8")
        with self.assertRaises(CloseSpider):
            spider.parse_page(response, index=0, offset=0)
        self.assertEqual(self.store.progress("a")["next_offset"], 0)
        self.assertTrue(spider.errors)

    def test_pagination_url_keeps_source_filter_ids(self):
        from urllib.parse import unquote
        url = page_url({"1": 1, "79": [397]}, 100, 100)
        query = json.loads(unquote(url.rsplit("/", 1)[1]))
        self.assertEqual(query["79"], [397])
        self.assertEqual(query["offset"], 100)


if __name__ == "__main__":
    unittest.main()

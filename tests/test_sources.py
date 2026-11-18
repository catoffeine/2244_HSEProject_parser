import tempfile
from types import SimpleNamespace
import unittest

import scrapy
from scrapy.http import HtmlResponse

from ekb_parser.core.config import CollectionConfig
from ekb_parser.core.spider import CatalogSpider
from ekb_parser.core.storage import Store
from ekb_parser.sources.base import SourceAdapter
from ekb_parser.sources.registry import _ADAPTERS, get_adapter, register_source


class ExampleAdapter(SourceAdapter):
    key = "example_test"
    name = "Offline HTML example"
    website = "https://catalog.example"
    allowed_domains = ("catalog.example",)
    default_groups = [{"key": "all", "name": "All", "query": {"category": "all"}}]

    def page_request(self, query, offset, page_size):
        return scrapy.Request(f"{self.website}/{query['category']}?offset={offset}&limit={page_size}")

    def parse_page(self, response):
        return [{"source": self.key, "source_id": node.attrib["data-id"],
                 "part_number": node.css("::text").get(), "manufacturer": "Example",
                 "source_url": response.url, "retrieved_at": "2026-11-17T00:00:00Z",
                 "parameters": [{"source_parameter_id": "capacitance", "value": "33 uF"}]}
                for node in response.css("li[data-id]")]


class SourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        register_source(ExampleAdapter)

    @classmethod
    def tearDownClass(cls):
        _ADAPTERS.pop(ExampleAdapter.key)

    def test_html_source_uses_common_pagination_and_storage(self):
        config = CollectionConfig.model_validate({"source": ExampleAdapter.key, "page_size": 2,
                                                   "groups": ExampleAdapter.default_groups})
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(tmp)
            try:
                store.initialize(config)
                spider = CatalogSpider(config, store, max_pages=0)
                spider.crawler = SimpleNamespace(stats=SimpleNamespace(inc_value=lambda *a: None))
                request = spider.next_group(0)
                self.assertIn("catalog.example/all", request.url)
                self.assertEqual(spider.allowed_domains, ["catalog.example"])
                response = HtmlResponse(url=request.url, body=b'<li data-id="a1">PART-1</li>', encoding="utf-8")
                self.assertIsNone(spider.parse_page(response, index=0, offset=0))
                self.assertEqual(store.count(), 1)
                self.assertTrue(store.progress("all")["complete"])
                self.assertIsNone(store.progress("all")["expected_count"])
            finally:
                store.close()

    def test_source_validation_is_not_yageo_specific(self):
        config = CollectionConfig.model_validate({"source": ExampleAdapter.key,
                                                   "groups": ExampleAdapter.default_groups})
        self.assertEqual(config.groups[0].query, {"category": "all"})
        with self.assertRaises(ValueError):
            CollectionConfig.model_validate({"source": "yageo", "groups": ExampleAdapter.default_groups})
        with self.assertRaises(ValueError):
            get_adapter("not_registered")

    def test_duplicate_registration_is_rejected(self):
        with self.assertRaises(ValueError):
            register_source(ExampleAdapter)

    def test_yageo_resistor_coverage_distinguishes_construction(self):
        adapter = get_adapter("yageo")
        film = "Резисторы постоянные непроволочные"
        wire_or_foil = "Резисторы постоянные проволочные и фольговые"
        for kind, style, description, resistance, expected in (
            ("Thick Film", "SMD Chip", "Chip resistor", "100", film),
            ("Wirewound", "Axial", "Power resistor", "100", wire_or_foil),
            ("Metal", "SMD Chip", "Metal foil resistor", "0.01", wire_or_foil),
            ("Metal", "SMD Chip", "Current sense resistor", "0.01", None),
            ("Thick Film", "SMD Network/Array", "Chip resistor", "100", None),
            ("Thick Film", "SMD Chip", "Chip resistor", "0", None),
            ("Jumper", "SMD Chip", "Metal alloy", None, None),
        ):
            with self.subTest(kind=kind, style=style, description=description, resistance=resistance):
                values = {79: kind, 41: style, 14: description, 279: resistance, 271: "0.063"}
                record = {"catalogue_category": "Resistors",
                          "parameters": [{"source_parameter_id": key, "value": value} for key, value in values.items()]}
                group, fields = adapter.coverage_data(record)
                self.assertEqual(group, ("Резисторы", expected) if expected else None)
                self.assertEqual(fields, {})

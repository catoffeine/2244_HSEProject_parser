import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import re
from urllib.parse import quote

import scrapy

from ekb_parser.sources.base import Metadata, SourceAdapter

BASE_URL = "https://yageogroup.com"
CATALOGUE_CATEGORIES = {1: "Capacitors", 1605: "Resistors"}
FIXED_FILM_RESISTORS = "Резисторы постоянные непроволочные"
FIXED_WIRE_OR_FOIL_RESISTORS = "Резисторы постоянные проволочные и фольговые"


def query_url(kind, query):
    encoded = quote(json.dumps(query, separators=(",", ":"), ensure_ascii=False), safe="")
    return f"{BASE_URL}/search/query/{kind}/{encoded}"


def page_url(query, offset, page_size):
    return query_url("base-part", {
        **query, "asc": "", "desc": "", "limit": page_size,
        "offset": offset, "isNumeric": False,
    })


def catalogue_number(value):
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def resistor_group(parameters, record):
    kind = str(parameters.get("79") or "").strip().casefold()
    style = str(parameters.get("41") or "").casefold()
    description = str(parameters.get("14") or record.get("raw", {}).get("description") or "").casefold()
    if kind == "jumper" or "jumper" in description or catalogue_number(parameters.get("279")) == 0:
        return None
    if re.search(r"\b(?:network|array)\b", f"{style} {description}"):
        return None
    if kind in {"carbon", "carbon film", "metal oxide film", "metal film", "thick film", "thin film"}:
        return FIXED_FILM_RESISTORS
    if kind in {"wirewound", "wire wound", "wire-wound"}:
        return FIXED_WIRE_OR_FOIL_RESISTORS
    if kind == "metal":
        if re.search(r"\b(?:metal[ -]foil|foil resistor)\b", description):
            return FIXED_WIRE_OR_FOIL_RESISTORS
        if re.search(r"\bmetal[ -]film\b", description):
            return FIXED_FILM_RESISTORS
    return None


def normalize(raw, source_url, category="Capacitors"):
    pn = raw.get("basePn")
    manufacturer = raw.get("manufacturer")
    if not isinstance(pn, str) or not pn.strip():
        raise ValueError("Catalogue row has no basePn")
    if not raw.get("basePartId") or not manufacturer:
        raise ValueError(f"Missing basePartId/manufacturer for {pn}")
    parameters = [{
        "source_parameter_id": p["parameterId"],
        "name": p.get("parameterName"),
        "value": p.get("value"),
        "formatted_value": p.get("formattedValue"),
    } for p in raw.get("parameterValues", [])]
    series = next((p["value"] for p in parameters if p["source_parameter_id"] == 3), None)
    document = raw.get("dataSheet")
    encoded_pn = quote(pn, safe="")
    return {
        "schema_version": 1,
        "source": "yageo",
        "source_id": str(raw["basePartId"]),
        "record_kind": "base_part",
        "part_number": pn,
        "display_part_number": raw.get("displayPn"),
        "manufacturer": manufacturer,
        "series": series,
        "source_url": source_url,
        "catalogue_category": category,
        "product_url": f"{BASE_URL}/products/{quote(category, safe='')}/part/{encoded_pn}",
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "datasheet_id": document,
        "datasheet_url": f"{BASE_URL}/content/datasheet/asset/file/{quote(document, safe='')}" if document else None,
        "specsheet_url": f"{BASE_URL}/component-documentation/download/specsheet/{encoded_pn}?lang=en",
        "parameters": parameters,
        "raw": raw,
    }


class YageoAdapter(SourceAdapter):
    key = "yageo"
    name = "YAGEO Group"
    website = BASE_URL
    allowed_domains = ("yageogroup.com", "www.yageogroup.com")
    max_page_size = 100
    capacitor_groups = {
        "Aluminum": "Конденсаторы оксидно-электролитические",
        "Ceramic": "Конденсаторы керамические",
        "Film": "Конденсаторы и фильтры пленочные и металлопленочные",
    }
    default_groups = [{"key": "aluminum", "name": capacitor_groups["Aluminum"],
                       "query": {"1": 1, "79": [397]}}]

    def coverage_data(self, record):
        category = record.get("catalogue_category", "Capacitors")
        if category not in {"Capacitors", "Resistors"}:
            return None, {}
        parameters = {str(p["source_parameter_id"]): p["value"] for p in record["parameters"]}
        if category == "Resistors":
            group_name = resistor_group(parameters, record)
            group = ("Резисторы", group_name) if group_name else None
        else:
            group_name = self.capacitor_groups.get(parameters.get("79"))
            group = ("Конденсаторы", group_name) if group_name else None
        return group, {}

    def validate_options(self):
        unknown = self.options.keys() - {"catalogue_category"}
        if unknown:
            raise ValueError(f"Unknown YAGEO options: {', '.join(sorted(unknown))}")
        category = self.options.get("catalogue_category", "Capacitors")
        if not isinstance(category, str) or not category.strip() or any(c in category for c in "/?#"):
            raise ValueError("catalogue_category must be a single product category from the YAGEO URL")
        self.category = category

    def validate_query(self, query):
        if "1" not in query:
            raise ValueError('YAGEO query must contain category ID under key "1"')
        if {"limit", "offset", "asc", "desc", "isNumeric"} & query.keys():
            raise ValueError("YAGEO pagination and sorting are controlled by the collector")

    def metadata_request(self, query):
        return scrapy.Request(query_url("bp-filter", query))

    def parse_metadata(self, response):
        data = response.json()
        count = data["counts"]
        if type(count) is not int or count < 0:
            raise ValueError("Expected nonnegative integer counts")
        return Metadata(count, data)

    def page_request(self, query, offset, page_size):
        category = self.options.get("catalogue_category") or CATALOGUE_CATEGORIES.get(query["1"], "Capacitors")
        return scrapy.Request(page_url(query, offset, page_size), meta={"yageo_catalogue_category": category})

    def parse_page(self, response):
        rows = response.json()
        if not isinstance(rows, list):
            raise ValueError("YAGEO catalogue response must be a list")
        category = response.request.meta.get("yageo_catalogue_category", self.category) if response.request else self.category
        return [normalize(row, response.url, category) for row in rows]



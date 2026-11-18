import scrapy
from scrapy.exceptions import CloseSpider

from ekb_parser.core.models import Component
from ekb_parser.sources.registry import get_adapter


class CatalogSpider(scrapy.Spider):
    name = "catalog"

    def __init__(self, config, store, max_pages=3, max_parts=500000, **kwargs):
        super().__init__(**kwargs)
        self.config = config
        self.adapter = get_adapter(config.source, config.options)
        self.allowed_domains = list(self.adapter.allowed_domains)
        self.store = store
        self.max_pages = max_pages
        self.max_parts = max_parts
        self.pages_this_run = 0
        self.errors = []
        self.stop_reason = "finished"

    async def start(self):
        request = self.next_group(0)
        if request:
            yield request

    def next_group(self, index):
        if self.store.count() >= self.max_parts:
            self.stop_reason = "max_parts"
            return None
        for i in range(index, len(self.config.groups)):
            group = self.config.groups[i]
            if not self.store.progress(group.key)["complete"]:
                metadata = self.adapter.metadata_request(group.query)
                if metadata is None:
                    return self.page_request(i, self.store.progress(group.key)["next_offset"])
                return metadata.replace(callback=self.parse_filters, errback=self.failed_request,
                                        cb_kwargs={"index": i})
        return None

    def fail(self, message):
        self.errors.append(message)
        self.logger.error(message)
        raise CloseSpider("collection_error")

    def failed_request(self, failure):
        self.fail(f"Request failed after retries: {failure.request.url}: {failure.value}")

    def parse_filters(self, response, index):
        group = self.config.groups[index]
        try:
            metadata = self.adapter.parse_metadata(response)
            count = metadata.expected_count
            if count is not None and (type(count) is not int or count < 0):
                raise ValueError("Expected a nonnegative count or None")
            previous = self.store.progress(group.key)
            if previous["expected_count"] is not None and count is not None and previous["expected_count"] != count:
                raise ValueError("Catalogue count changed since previous run. Use a new output directory for a fresh snapshot.")
            self.store.set_expected(group.key, count)
            self.store._write_json(f"filters_{group.key}.json", metadata.raw)
            if count is not None and previous["next_offset"] > count:
                raise ValueError("Saved offset exceeds source count")
        except (ValueError, KeyError, TypeError) as exc:
            self.fail(f"Invalid filter response for {group.key}: {exc}")
        self.logger.info("Group %s: advertised=%s, next_offset=%d", group.key, count, previous["next_offset"])
        if count == previous["next_offset"]:
            self.store.save_page(group.key, count, [], complete=True)
            return self.next_group(index + 1)
        return self.page_request(index, previous["next_offset"])

    def page_request(self, index, offset):
        return self.adapter.page_request(self.config.groups[index].query, offset, self.config.page_size).replace(
            callback=self.parse_page, errback=self.failed_request, cb_kwargs={"index": index, "offset": offset})

    def parse_page(self, response, index, offset):
        group = self.config.groups[index]
        try:
            rows = self.adapter.parse_page(response)
            if not isinstance(rows, list) or len(rows) > self.config.page_size:
                raise ValueError("Expected a list no larger than page_size")
            records = [Component.model_validate(row).model_dump() for row in rows]
            if any(record["source"] != self.config.source for record in records):
                raise ValueError("Adapter returned a record belonging to a different source")
            # Компонент мог уже попасть в сбор через другую группу.
            remaining = self.max_parts - self.store.count()
            accepted = []
            for record in records:
                exists = self.store.db.execute("SELECT 1 FROM components WHERE source=? AND source_id=?",
                                               (record["source"], record["source_id"])).fetchone()
                if not exists:
                    if remaining <= 0:
                        break
                    remaining -= 1
                accepted.append(record)
            expected = self.store.progress(group.key)["expected_count"]
            if expected is not None and offset + len(accepted) > expected:
                raise ValueError("Page exceeds the advertised count")
            complete = (offset + len(accepted) == expected) if expected is not None else (
                len(accepted) == len(rows) and len(rows) < self.config.page_size)
            self.store.save_page(group.key, offset, accepted, complete=complete)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            self.fail(f"Invalid page {group.key} offset={offset}: {exc}")
        self.pages_this_run += 1
        self.crawler.stats.inc_value("catalog/pages_committed")
        self.crawler.stats.inc_value("catalog/rows_committed", len(accepted))
        self.logger.info("Committed %s offset=%d rows=%d; unique=%d", group.key, offset, len(accepted), self.store.count())
        if self.store.count() >= self.max_parts:
            self.stop_reason = "max_parts"
            return None
        if self.max_pages and self.pages_this_run >= self.max_pages:
            self.stop_reason = "max_pages"
            return None
        if complete:
            return self.next_group(index + 1)
        return self.page_request(index, offset + len(accepted))

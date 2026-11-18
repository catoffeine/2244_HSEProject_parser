from datetime import datetime, timezone
import hashlib
from pathlib import Path
from urllib.parse import urlsplit

import scrapy

from ekb_parser.core.config import load_config
from ekb_parser.core.storage import Store, document_entries, saved_document_path
from ekb_parser.sources.registry import get_adapter


class DatasheetSpider(scrapy.Spider):
    name = "datasheets"

    def __init__(self, store, adapter, max_documents=0, **kwargs):
        super().__init__(**kwargs)
        self.store = store
        self.adapter = adapter
        self.allowed_domains = list(adapter.allowed_domains)
        self.max_documents = max_documents
        self.downloaded = 0
        self.skipped = 0
        self.errors = []

    def record_error(self, source, url, error):
        self.errors.append(f"{url}: {error}")
        self.store.save_document(source, url, error=str(error))
        self.logger.error("Datasheet failed: %s: %s", url, error)

    async def start(self):
        scheduled = 0
        # Отдельный курсор, чтобы запись скачанных PDF не сбила перебор ссылок.
        for source, url, status, local_path in document_entries(self.store.db):
            if status == "downloaded" and saved_document_path(self.store.output, local_path):
                self.skipped += 1
                continue
            if self.max_documents and scheduled >= self.max_documents:
                break
            scheduled += 1
            parsed = urlsplit(url)
            if parsed.scheme not in ("http", "https") or not any(
                parsed.hostname == domain or (parsed.hostname or "").endswith("." + domain)
                for domain in self.allowed_domains
            ):
                self.record_error(source, url, "URL is outside the adapter's allowed domains")
                continue
            yield scrapy.Request(url, callback=self.save_pdf, errback=self.failed,
                                 cb_kwargs={"source": source, "original_url": url})

    def failed(self, failure):
        kwargs = failure.request.cb_kwargs
        self.record_error(kwargs["source"], kwargs["original_url"], failure.value)

    def save_pdf(self, response, source, original_url):
        if not response.body.startswith(b"%PDF-"):
            self.record_error(source, original_url, "Expected a PDF; response has no %PDF- signature")
            return
        digest = hashlib.sha256(response.body).hexdigest()
        folder = self.store.output / "datasheets"
        if not folder.resolve().is_relative_to(self.store.output.resolve()):
            self.record_error(source, original_url, "Datasheet directory resolves outside the run")
            return
        folder.mkdir(exist_ok=True)
        path = folder / f"{digest}.pdf"
        if not path.exists():
            temp = path.with_suffix(".pdf.tmp")
            temp.write_bytes(response.body)
            temp.replace(path)
        self.store.save_document(source, original_url, sha256=digest,
                                 local_path=path.relative_to(self.store.output).as_posix(),
                                 downloaded_at=datetime.now(timezone.utc).isoformat())
        self.downloaded += 1
        self.crawler.stats.inc_value("datasheets/downloaded")
        self.logger.info("Saved PDF: %s -> %s", original_url, path.name)


def download_datasheets(output, max_documents=0):
    from scrapy.crawler import CrawlerProcess
    from ekb_parser.core.cli import crawler_settings

    output = Path(output)
    config = load_config(output / "collection_config.json")
    adapter = get_adapter(config.source, config.options)
    store = Store(output)
    try:
        process = CrawlerProcess(settings=crawler_settings(config))
        crawler = process.create_crawler(DatasheetSpider)
        process.crawl(crawler, store=store, adapter=adapter, max_documents=max_documents)
        process.start()
        spider = crawler.spider
        if spider is None:
            raise RuntimeError("Datasheet downloader did not start")
        stats = crawler.stats.get_stats()
        errors = list(spider.errors)
        if stats.get("spider_exceptions/count") or stats.get("finish_reason_error"):
            errors.append("Downloader exception; see log")
        report = {"downloaded_this_run": spider.downloaded, "skipped": spider.skipped,
                  "errors": errors, "finish_reason": stats.get("finish_reason")}
        store._write_json("last_datasheet_run.json", report)
        store.export_documents()
        print(report)
        return 1 if errors else 0
    finally:
        store.close()

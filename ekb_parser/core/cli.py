import argparse
from contextlib import closing
import json
import sqlite3
import sys
from pathlib import Path

from ekb_parser.core.config import DEFAULT_CONFIG, load_config
from ekb_parser.core.requirements import (
    HTML_DIRECTORY, REQUIREMENTS_FILE, load_requirements, requirements_version, write_requirements,
)
from ekb_parser.core.storage import Store


def crawler_settings(config, log_level="INFO"):
    return {
        "ROBOTSTXT_OBEY": True, "TELNETCONSOLE_ENABLED": False, "REMOTE_CONTROL_ENABLED": False,
        "CONCURRENT_REQUESTS": 1, "CONCURRENT_REQUESTS_PER_DOMAIN": 1,
        "DOWNLOAD_DELAY": config.download_delay, "DOWNLOAD_DELAY_JITTER": 0,
        "DOWNLOAD_TIMEOUT": 60, "RETRY_TIMES": 4,
        "RETRY_HTTP_CODES": [408, 429, 500, 502, 503, 504, 522, 524],
        "AUTOTHROTTLE_ENABLED": True, "AUTOTHROTTLE_START_DELAY": config.download_delay,
        "AUTOTHROTTLE_TARGET_CONCURRENCY": 1.0, "LOG_LEVEL": log_level,
        "USER_AGENT": "EKBResearchParser/0.3 (academic catalogue research)",
    }


def main():
    parser = argparse.ArgumentParser(description="Portable catalogue collector with source adapters")
    sub = parser.add_subparsers(dest="command", required=True)
    collect = sub.add_parser("collect", help="Collect and resume a configured catalogue")
    collect.add_argument("--config", help=f"Category config; default: {DEFAULT_CONFIG} (first group)")
    collect.add_argument("--group", help="Collect one group from the selected category")
    collect.add_argument("--output", help="Defaults to data/<source>/runs/<group or config name>")
    collect.add_argument("--max-pages", type=int, default=3, help="Pages this invocation; 0 means no page limit")
    collect.add_argument("--max-parts", type=int, default=500000, help="Maximum unique rows in this output")
    collect.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="INFO")
    collect.add_argument("--no-export", action="store_true", help="Keep crawl checkpoint only; export separately later")
    for name in ("status", "export"):
        command = sub.add_parser(name)
        command.add_argument("--output", required=True)
    sub.add_parser("sources", help="List supported website adapters")
    documents = sub.add_parser("datasheets", help="Download unique PDFs referenced by a saved run")
    documents.add_argument("--output", required=True)
    documents.add_argument("--max-documents", type=int, default=0, help="New download attempts; 0 means all")
    filters = sub.add_parser("filters", help="Save filter metadata for a group; no browser required")
    filters.add_argument("--config", default=DEFAULT_CONFIG)
    filters.add_argument("--group", help="Group key; defaults to the first group")
    filters.add_argument("--save", help="Defaults to data/<source>/metadata/filters_<group>.json")
    matrix = sub.add_parser("convert-requirements", help="Convert analytics HTML sheets to a JSON requirements matrix")
    matrix.add_argument("--input", type=Path, default=HTML_DIRECTORY)
    matrix.add_argument("--save", type=Path, default=REQUIREMENTS_FILE)
    supplement = sub.add_parser("import-supplements", help="Import normalized observations into a saved collection")
    supplement.add_argument("--output", type=Path, required=True)
    supplement.add_argument("--input", type=Path, required=True)
    coverage = sub.add_parser("coverage", help="Measure completeness against the analytics requirements")
    coverage.add_argument("--output", type=Path, required=True)
    coverage.add_argument("--save", type=Path, help="Create a JSONL with the required fields and their values for each part")
    coverage.add_argument("--replace", action="store_true", help="Replace an existing report")
    enrichment = sub.add_parser("pdf-enrichment", help="Очередь и проверяемый импорт данных из даташитов")
    operations = enrichment.add_subparsers(dest="operation", required=True)
    prepare = operations.add_parser("prepare", help="Выгрузить пустые поля с доступным PDF")
    prepare.add_argument("--output", type=Path, required=True, help="Папка существующего сбора")
    prepare.add_argument("--tasks", type=Path, required=True, help="Новый файл очереди JSONL")
    prepare.add_argument("--field", help="Ограничить очередь точным именем поля")
    apply = operations.add_parser("apply", help="Проверить кандидатов, при необходимости записать в SQLite")
    apply.add_argument("--output", type=Path, required=True, help="Папка существующего сбора")
    apply.add_argument("--input", type=Path, required=True, help="Кандидаты в JSONL")
    apply.add_argument("--approved-rules", type=Path, required=True, help="JSON с одобренными правилами")
    apply.add_argument("--report", type=Path, required=True, help="Новый файл отчёта JSONL")
    apply.add_argument("--commit", action="store_true", help="Записать прошедшие проверку значения")
    operations.add_parser("schema", help="Показать JSON Schema форматов обмена")
    args = parser.parse_args()
    if args.command == "pdf-enrichment":
        from ekb_parser.core.catalogue_mappings import load_mappings
        from ekb_parser.core.coverage import Coverage
        from ekb_parser.core.pdf_enrichment import APPROVALS, apply_candidates, prepare_tasks, schemas

        if args.operation == "schema":
            print(json.dumps(schemas(), ensure_ascii=False, indent=2))
            return 0
        if not (args.output / "crawl_state.sqlite3").is_file():
            parser.error("В --output нет сохранённого сбора")
        root = Path.cwd()
        try:
            requirements = load_requirements(root, requirements_version(root))
            if not requirements["sheets"]:
                parser.error("Матрица требований отсутствует")
            assessment = Coverage(requirements, args.output, load_mappings(root))
            if args.operation == "prepare":
                with args.tasks.open("x", encoding="utf-8") as stream:
                    result = {"tasks": prepare_tasks(args.output, assessment, stream, args.field)}
            else:
                rules = APPROVALS.validate_json(args.approved_rules.read_text(encoding="utf-8-sig"))
                with args.input.open(encoding="utf-8-sig") as stream, args.report.open("x", encoding="utf-8") as report:
                    result = apply_candidates(args.output, assessment, stream, report, rules, commit=args.commit)
        except (OSError, ValueError, sqlite3.Error) as error:
            parser.error(str(error))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    if args.command == "convert-requirements":
        document = write_requirements(args.input, args.save)
        print(f"Saved {args.save}; sheets: {len(document['sheets'])}")
        return 0
    if args.command in ("import-supplements", "coverage"):
        from ekb_parser.core.coverage import Coverage, import_supplements, write_report

        database = (args.output / "crawl_state.sqlite3").resolve()
        if not database.is_file():
            parser.error("No saved collection in --output")
        root = Path.cwd()
        requirements = load_requirements(root, requirements_version(root))
        if args.command == "import-supplements":
            imported = import_supplements(database, args.input, requirements)
            print(f"Imported supplements: {imported}")
        else:
            from ekb_parser.core.catalogue_mappings import load_mappings

            assessment = Coverage(requirements, args.output, load_mappings(root))
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as db:
                db.execute("BEGIN")
                summary = assessment.summary(db)
                if args.save:
                    write_report(db, assessment, args.save, replace=args.replace)
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    if args.command == "sources":
        from ekb_parser.sources.registry import list_sources
        print(json.dumps(list_sources(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "datasheets":
        if args.max_documents < 0:
            parser.error("max-documents must be nonnegative")
        if not (Path(args.output) / "crawl_state.sqlite3").is_file():
            parser.error("No saved collection in --output")
        from ekb_parser.core.documents import download_datasheets
        return download_datasheets(args.output, args.max_documents)
    if args.command == "collect":
        config_path = args.config or DEFAULT_CONFIG
        config = load_config(config_path)
        group_key = args.group if args.group is not None else (config.groups[0].key if args.config is None else None)
        if group_key is not None:
            config = config.model_copy(update={"groups": [config.group(group_key)]})
        args.output = args.output or f"data/{config.source}/runs/{group_key or Path(config_path).stem}"
    if args.command == "filters":
        import httpx
        from scrapy.http import TextResponse
        from ekb_parser.sources.registry import get_adapter
        config = load_config(args.config)
        group = config.group(args.group) if args.group is not None else config.groups[0]
        adapter = get_adapter(config.source, config.options)
        source_request = adapter.metadata_request(group.query)
        if source_request is None:
            parser.error("This adapter does not expose filter metadata")
        with httpx.Client(timeout=60, follow_redirects=True,
                          headers={"User-Agent": "EKBResearchParser/0.2 (academic catalogue research)"}) as client:
            response = client.request(source_request.method, source_request.url, content=source_request.body,
                                      headers={k.decode(): v[0].decode() for k, v in source_request.headers.items()})
            response.raise_for_status()
            metadata = adapter.parse_metadata(TextResponse(url=str(response.url), body=response.content, encoding="utf-8"))
            data = metadata.raw
        path = Path(args.save or f"data/{config.source}/metadata/filters_{group.key}.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"Saved {path}; advertised rows: {metadata.expected_count}")
        return 0
    if args.command != "collect" and not (Path(args.output) / "crawl_state.sqlite3").is_file():
        parser.error("No crawl_state.sqlite3 found in --output")
    if args.command == "collect" and (args.max_pages < 0 or args.max_parts < 1):
        parser.error("max-pages must be nonnegative; max-parts must be positive")
    store = Store(args.output)
    exit_code = 0
    try:
        if args.command == "collect":
            from scrapy.crawler import CrawlerProcess
            from ekb_parser.core.spider import CatalogSpider
            store.initialize(config)
            process = CrawlerProcess(settings=crawler_settings(config, args.log_level))
            crawler = process.create_crawler(CatalogSpider)
            process.crawl(crawler, config=config, store=store,
                          max_pages=args.max_pages, max_parts=args.max_parts)
            process.start()
            spider = crawler.spider
            if spider is None:
                raise RuntimeError("Crawler did not start")
            stats = crawler.stats.get_stats()
            errors = list(spider.errors)
            if stats.get("spider_exceptions/count"):
                errors.append("Unhandled spider exception; see logs")
            finish_reason = stats.get("finish_reason")
            if finish_reason not in ("finished", "shutdown", "collection_error"):
                errors.append(f"Unexpected crawler finish reason: {finish_reason}")
            store._write_json("last_run.json", {
                "stop_reason": "error" if errors else ("shutdown" if finish_reason == "shutdown" else spider.stop_reason),
                "pages_committed": spider.pages_this_run, "errors": errors,
                "requests": stats.get("downloader/request_count", 0),
            })
            exit_code = 1 if errors else 0
            if not args.no_export:
                store.export()
            else:
                store._write_json("summary.json", store.summary())
        elif args.command == "export":
            store.export()
        print(json.dumps(store.summary(), ensure_ascii=False, indent=2))
    finally:
        store.close()
    return exit_code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

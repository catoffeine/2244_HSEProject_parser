import argparse
import json
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from urllib.parse import unquote, urlsplit

from ekb_parser.core.config import CollectionConfig, load_config
from ekb_parser.sources.yageo.adapter import CATALOGUE_CATEGORIES


def yageo_path(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc.lower() not in {"yageogroup.com", "www.yageogroup.com"}:
        raise ValueError("Нужен публичный HTTPS-адрес yageogroup.com")
    return parsed.path


def catalogue_category(url):
    parts = [unquote(part) for part in yageo_path(url).split("/") if part]
    if parts[:2] == ["browse", "products"]:
        parts = parts[1:]
    if len(parts) < 2 or parts[0] != "products":
        raise ValueError("Нужен URL страницы категории YAGEO")
    return parts[1]


def parse_request(request_url):
    prefix = "/search/query/base-part/"
    path = urlsplit(request_url).path if request_url.startswith(prefix) else yageo_path(request_url)
    if not path.startswith(prefix):
        raise ValueError("Нужен URL запроса /search/query/base-part/ из Network")
    try:
        query = json.loads(unquote(path.removeprefix(prefix)))
    except json.JSONDecodeError as error:
        raise ValueError("Не удалось прочитать фильтры из URL. Скопируйте полный Request URL из Network.") from error
    if not isinstance(query, dict) or type(query.get("1")) is not int or query["1"] <= 0:
        raise ValueError("В запросе отсутствует числовой идентификатор категории «1»")
    return {key: value for key, value in query.items()
            if key not in {"limit", "offset", "asc", "desc", "isNumeric"}}


def build_config(key, name, catalogue_url, filters, category_name=None):
    category = catalogue_category(catalogue_url)
    known_category = CATALOGUE_CATEGORIES.get(filters["1"])
    if known_category != category and (known_category or category in CATALOGUE_CATEGORIES.values()):
        raise ValueError("Адрес страницы и запрос относятся к разным категориям YAGEO")
    options = {} if known_category else {"catalogue_category": category}
    label = {"Capacitors": "Конденсаторы", "Resistors": "Резисторы"}.get(category, category)
    return CollectionConfig.model_validate({
        "source": "yageo", "name": category_name or label, "options": options,
        "groups": [{"key": key, "name": name, "query": filters}],
    })


def save_config(root, key, name, catalogue_url, request_url, *,
                category_file=None, category_name=None, category_key=None):
    if category_file and (category_name or category_key or catalogue_url):
        raise ValueError("Выберите существующую категорию или заполните поля новой категории")
    if category_key and not category_name:
        raise ValueError("Укажите название новой категории")
    filters = parse_request(request_url)
    directory = (root / "configs/yageo").resolve()
    if not directory.is_relative_to((root / "configs").resolve()):
        raise ValueError("Папка пресетов YAGEO должна находиться внутри configs/")
    directory.mkdir(parents=True, exist_ok=True)
    category_id = filters["1"]
    if category_file:
        selected = Path(category_file)
        if selected.parent != Path("configs/yageo") or selected.suffix != ".json":
            raise ValueError("Выберите категорию YAGEO из списка")
        path = directory / selected.name
        if not path.is_file():
            raise ValueError("Выбранная категория больше не существует")
        config = None
    else:
        if not catalogue_url:
            raise ValueError("Укажите адрес страницы категории YAGEO")
        config = build_config(key, name, catalogue_url, filters, category_name)
        category = config.options.get("catalogue_category") or CATALOGUE_CATEGORIES[category_id]
        slug = category_key or re.sub(r"[^a-z0-9]+", "-", category.lower()).strip("-") or str(category_id)
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", slug):
            raise ValueError("Ключ категории должен содержать строчные латинские буквы, цифры, дефис или подчёркивание")
        path = directory / f"{slug}.json"
    if path.is_symlink():
        raise ValueError("Файл категории должен находиться внутри configs/yageo/")
    if category_name:
        if path.exists():
            raise ValueError(f"Файл {path.name} уже существует. Выберите существующую категорию или другой ключ.")
        for candidate in directory.glob("*.json"):
            if candidate.is_symlink():
                continue
            try:
                existing = load_config(candidate)
            except ValueError:
                continue
            if existing.source == "yageo" and (
                existing.options.get("catalogue_category") == category
                or any(group.query.get("1") == category_id for group in existing.groups)
            ):
                raise ValueError(f"Этот раздел YAGEO уже есть в {candidate.name}. Выберите его в списке категорий.")
    elif path.exists():
        existing = load_config(path)
        if existing.source != "yageo" or (config and existing.options != config.options) or any(
            group.query.get("1") != category_id for group in existing.groups
        ):
            raise ValueError("Запрос base-part относится к другой категории YAGEO")
        if any(group.key == key for group in existing.groups):
            raise ValueError("В этой категории уже есть подкатегория с таким ключом")
        data = existing.model_dump()
        data["groups"].append({"key": key, "name": name, "query": filters})
        config = CollectionConfig.model_validate(data)

    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(config.model_dump_json(indent=2, exclude_none=True) + "\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return path.relative_to(root)


def main():
    parser = argparse.ArgumentParser(description="Добавить подкатегорию YAGEO из адреса запроса каталога")
    parser.add_argument("--key", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--catalogue", required=True, help="YAGEO product category page")
    parser.add_argument("--request", required=True, help="YAGEO catalogue base-part request URL")
    args = parser.parse_args()
    try:
        path = save_config(Path.cwd(), args.key, args.name, args.catalogue, args.request)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(path)


if __name__ == "__main__":
    main()

from functools import lru_cache
import json
from pathlib import Path
from tempfile import NamedTemporaryFile

HTML_DIRECTORY = Path("project_files/analytics_data")
REQUIREMENTS_FILE = Path("data/analytics/requirements.json")


def convert_requirements(directory):
    from parsel import Selector

    sheets = {}
    for path in sorted(Path(directory).glob("*.html")):
        document = Selector(text=path.read_text(encoding="utf-8-sig"))
        rows = []
        for row_number, row in enumerate(document.css("tr"), 1):
            cells = row.xpath("./td")
            for index, cell in enumerate(cells):
                if cell.attrib.get("colspan") == "2" and cell.attrib.get("rowspan") == "2":
                    values = [" ".join(c.xpath("string(.)").get("").split()) for c in cells[index:]]
                    if values[0]:
                        rows.append((row_number, values))
                    break
        if not rows or rows[0][1][0] != "Группа":
            raise ValueError(f"{path.name}: не найден заголовок «Группа»")
        columns = rows[0][1][1:]
        labels = [label for label in columns if label]
        if len(set(labels)) != len(labels):
            raise ValueError(f"{path.name}: повторяющиеся заголовки")
        groups = {}
        for row_number, (name, *values) in rows[1:]:
            if len(values) != len(columns):
                raise ValueError(f"{path.name}: некорректная строка группы «{name}»")
            fields = {}
            for label, value in zip(columns, values):
                if not label:
                    if value:
                        raise ValueError(f"{path.name}: значение без заголовка в группе «{name}»")
                    continue
                state = {"": "unspecified", "+": "required", "-": "excluded"}.get(value, "fixed")
                fields[label] = {"state": state}
                if state == "fixed":
                    fields[label]["value"] = value
            if name in groups and groups[name]["fields"] != fields:
                raise ValueError(f"{path.name}: различающиеся требования у группы «{name}»")
            group = groups.setdefault(name, {"configured": bool(columns) and all(columns) and all(values),
                                             "fields": fields, "source_rows": []})
            group["source_rows"].append(row_number)
        sheets[path.stem] = groups
    return {"version": 1, "sheets": sheets}


def write_requirements(directory, output):
    document = convert_requirements(directory)
    if not document["sheets"]:
        raise ValueError(f"HTML-таблицы не найдены: {directory}")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent, delete=False) as stream:
            temp_path = Path(stream.name)
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temp_path.replace(output)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return document


def file_version(path):
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return (stat.st_ino, stat.st_size, stat.st_mtime_ns)


@lru_cache(maxsize=4)
def load_requirements(root, version):
    if version[0] is not None:
        document = json.loads((root / REQUIREMENTS_FILE).read_text(encoding="utf-8"))
        if document.get("version") != 1 or not isinstance(document.get("sheets"), dict):
            raise ValueError("Неподдерживаемый формат матрицы требований")
        return document
    return convert_requirements(root / HTML_DIRECTORY)


def requirements_version(root):
    compiled = file_version(root / REQUIREMENTS_FILE)
    inputs = () if compiled else tuple(
        (path.name, file_version(path)) for path in sorted((root / HTML_DIRECTORY).glob("*.html"))
    )
    return compiled, inputs

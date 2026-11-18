from decimal import Decimal, DecimalException
import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

MAPPINGS_FILE = "data/analytics/catalogue_mappings.json"


class FieldMapping(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    parameter_id: int = Field(strict=True, gt=0, le=2**53 - 1)
    parameter_name: str = Field(default="", max_length=200)
    factor: Decimal = Field(default=Decimal(1), gt=0, max_digits=20, decimal_places=15)


class DeferredField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deferred: Literal[True]


CatalogueRule = FieldMapping | DeferredField


class MappingScope(BaseModel):
    source: str
    query: dict[str, Any]
    sheet: str
    group: str
    fields: dict[str, CatalogueRule]
    classified_only: bool = False


def load_mappings(root):
    path = root / MAPPINGS_FILE
    if not path.is_file():
        return []
    return [MappingScope.model_validate(item) for item in json.loads(path.read_text(encoding="utf-8"))]


def write_mappings(root, scopes):
    path = root / MAPPINGS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            document = [scope.model_dump(mode="json", exclude_defaults=True) for scope in scopes]
            json.dump(document, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def query_key(source, query):
    return source, json.dumps(query, sort_keys=True, ensure_ascii=False)


def mapped_value(value, factor):
    if value is None:
        return None
    if factor == 1:
        if isinstance(value, str):
            return value.strip() or None
        if isinstance(value, bool):
            return json.dumps(value)
        if isinstance(value, (list, dict)):
            return json.dumps(value, ensure_ascii=False) if value else None
    elif isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        return str(number * factor) if number.is_finite() else None
    except DecimalException:
        return None

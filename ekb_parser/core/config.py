import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_CONFIG = "configs/yageo/capacitors.json"


class Group(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    name: str
    query: dict[str, Any]


class CollectionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(default="yageo", pattern=r"^[a-z][a-z0-9_-]*$")
    name: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)
    page_size: int = Field(default=100, ge=1, le=10000)
    download_delay: float = Field(default=2.0, ge=1.0)
    groups: list[Group] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_keys(self):
        keys = [g.key for g in self.groups]
        if len(keys) != len(set(keys)):
            raise ValueError("Group keys must be unique")
        from ekb_parser.sources.registry import get_adapter
        adapter = get_adapter(self.source, self.options)
        if self.page_size > adapter.max_page_size:
            raise ValueError(f"{self.source}: page_size must be <= {adapter.max_page_size}")
        for group in self.groups:
            adapter.validate_query(group.query)
        return self

    def group(self, key):
        group = next((group for group in self.groups if group.key == key), None)
        if group is None:
            raise ValueError(f"Группа «{key}» отсутствует в конфигурации")
        return group

    def fingerprint(self):
        # При продолжении можно поменять лимиты и задержку. Запрос и размер страницы должны совпасть.
        identity = {"source": self.source, "schema_version": 1,
                    "page_size": self.page_size,
                    "groups": [g.model_dump(exclude={"name"}) for g in self.groups]}
        # Пустые настройки не добавляем, чтобы подошли уже сохранённые сборы.
        if self.options:
            identity["options"] = self.options
        value = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(value.encode()).hexdigest()


def load_config(path):
    return CollectionConfig.model_validate_json(Path(path).read_text(encoding="utf-8"))

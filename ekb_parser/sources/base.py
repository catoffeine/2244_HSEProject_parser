from abc import ABC, abstractmethod
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from scrapy import Request
from scrapy.http import Response


@dataclass
class Metadata:
    expected_count: int | None
    raw: Any = None


class SourceAdapter(ABC):
    key: str
    name: str
    website: str
    allowed_domains: tuple[str, ...]
    max_page_size = 100
    default_groups: list[dict] = []

    def __init__(self, options=None):
        self.options = options or {}
        self.validate_options()

    def validate_options(self):
        if self.options:
            raise ValueError(f"Source {self.key} does not support adapter options")

    def validate_query(self, query: dict):
        pass

    def metadata_request(self, query: dict) -> Request | None:
        # Если метаданных нет, читаем страницы до короткой или пустой.
        return None

    def parse_metadata(self, response: Response) -> Metadata:
        return Metadata(None)

    def coverage_data(self, record):
        # Возвращаем пару (лист, группа) и значения по именам полей из таблицы.
        return None, {}

    @abstractmethod
    def page_request(self, query: dict, offset: int, page_size: int) -> Request:
        # offset — сколько строк этой группы уже сохранили.
        pass

    @abstractmethod
    def parse_page(self, response: Response) -> list[dict]:
        pass

    @classmethod
    def descriptor(cls):
        return {
            "key": cls.key, "name": cls.name, "website": cls.website,
            "max_page_size": cls.max_page_size,
            "default_config": {
                "source": cls.key, "page_size": min(100, cls.max_page_size),
                "download_delay": 2.0, "options": {},
                "groups": deepcopy(cls.default_groups),
            },
        }

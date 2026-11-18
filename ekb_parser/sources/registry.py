from ekb_parser.sources.base import SourceAdapter
from ekb_parser.sources.yageo.adapter import YageoAdapter

_ADAPTERS: dict[str, type[SourceAdapter]] = {}


def register_source(adapter: type[SourceAdapter]):
    if adapter.key in _ADAPTERS:
        raise ValueError(f"Duplicate source: {adapter.key}")
    _ADAPTERS[adapter.key] = adapter
    return adapter


def get_adapter(source, options=None):
    try:
        adapter = _ADAPTERS[source]
    except KeyError:
        raise ValueError(f"Unknown source {source!r}. Available: {', '.join(_ADAPTERS)}") from None
    return adapter(options)


def list_sources():
    return [adapter.descriptor() for adapter in _ADAPTERS.values()]


register_source(YageoAdapter)

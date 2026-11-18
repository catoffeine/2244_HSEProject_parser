from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Parameter(BaseModel):
    model_config = ConfigDict(extra="allow")
    source_parameter_id: int | str
    name: str | None = None
    value: Any = None
    formatted_value: Any = None


class Component(BaseModel):
    model_config = ConfigDict(extra="allow")
    schema_version: int = 1
    source: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    part_number: str = Field(min_length=1)
    manufacturer: str = Field(min_length=1)
    series: str | None = None
    source_url: str
    retrieved_at: str
    parameters: list[Parameter] = Field(default_factory=list)
    raw: dict = Field(default_factory=dict)

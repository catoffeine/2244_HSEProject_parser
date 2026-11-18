from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import NamedTemporaryFile

from pydantic import BaseModel, ConfigDict, Field

from ekb_parser.core.catalogue_mappings import DeferredField, mapped_value, query_key
from ekb_parser.sources.registry import get_adapter


class PDFCitation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    page: int = Field(ge=1)
    quote: str = Field(min_length=1, max_length=4000)
    conditions: str | None = Field(default=None, max_length=2000)


class PDFEvidence(PDFCitation):
    url: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    rule_id: str = Field(min_length=1, max_length=160)


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    value: str | int | float
    evidence: str = Field(min_length=1)
    pdf: PDFEvidence | None = None


class Supplement(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    group: list[str] | None = Field(default=None, min_length=2, max_length=2)
    fields: dict[str, Observation] = Field(default_factory=dict)


def observation_table_exists(db):
    return db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='component_supplements'"
    ).fetchone() is not None


def ensure_supplement_table(db):
    db.execute("""CREATE TABLE IF NOT EXISTS component_supplements (
        source TEXT NOT NULL, source_id TEXT NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY (source, source_id)
    )""")


def save_supplement(db, source, source_id, supplement):
    db.execute("""INSERT INTO component_supplements VALUES (?,?,?)
        ON CONFLICT(source, source_id) DO UPDATE SET payload=excluded.payload""",
        (source, source_id, json.dumps(supplement, ensure_ascii=False, allow_nan=False)))


def assessment_rows(db, *, where="", params=(), page=False, group_keys=()):
    membership = "'[]'"
    if page or group_keys:
        membership = """(SELECT json_group_array(group_key) FROM membership
                         WHERE source=components.source AND source_id=components.source_id"""
        if not page:
            placeholders = ", ".join("?" for _ in group_keys)
            membership += f" AND group_key IN ({placeholders})"
            params = (*group_keys, *params)
        membership += ")"
    if observation_table_exists(db):
        query = f"""SELECT components.payload, supplements.payload, {membership} FROM components
                    LEFT JOIN component_supplements AS supplements USING (source, source_id)"""
    else:
        query = f"SELECT components.payload, NULL, {membership} FROM components"
    if where:
        query += " WHERE " + where
    if page:
        query += " ORDER BY components.source, components.source_id LIMIT 25 OFFSET ?"
    for record, supplement, groups in db.execute(query, params):
        yield json.loads(record), json.loads(supplement) if supplement else {}, sorted(json.loads(groups))


class Coverage:
    def __init__(self, requirements, output=None, mappings=()):
        self.sheets = requirements["sheets"]
        self.adapters = {}
        config = output / "collection_config.json" if output is not None else None
        snapshot = json.loads(config.read_text(encoding="utf-8")) if config and config.is_file() else {}
        self.options = snapshot.get("options", {})
        self.source = snapshot.get("source", "yageo")
        by_query = {query_key(scope.source, scope.query): scope for scope in mappings
                    if self.group((scope.sheet, scope.group)) is not None}
        self.group_mappings = {
            group["key"]: scope for group in snapshot.get("groups", [])
            if (scope := by_query.get(query_key(self.source, group["query"]))) is not None
        }

    def group(self, group):
        if group:
            return self.sheets.get(group[0], {}).get(group[1])
        return None

    def part(self, record, supplement, memberships=(), *, details=False):
        source = record["source"]
        if source not in self.adapters:
            self.adapters[source] = get_adapter(source, self.options)
        group, catalogue_fields = self.adapters[source].coverage_data(record)
        scopes = [self.group_mappings[key] for key in memberships if key in self.group_mappings] if source == self.source else []
        # Некоторые правила подходят только артикулам, чей тип уже подтверждён каталогом.
        scopes = [scope for scope in scopes if not scope.classified_only or group == (scope.sheet, scope.group)]
        targets = {(scope.sheet, scope.group) for scope in scopes}
        group = supplement.get("group") or (next(iter(targets)) if len(targets) == 1 else group)
        rules = {}
        conflicts = set()
        for scope in scopes:
            if (scope.sheet, scope.group) != tuple(group or ()):
                continue
            for name, mapping in scope.fields.items():
                rule = None if isinstance(mapping, DeferredField) else (mapping.parameter_id, mapping.factor)
                if name in rules and rules[name] != rule:
                    conflicts.add(name)
                rules[name] = rule
        parameters = {str(p["source_parameter_id"]): p["value"] for p in record["parameters"]} if rules else {}
        deferred = set()
        for name, rule in rules.items():
            if name in conflicts:
                continue
            if rule is None:
                deferred.add(name)
                catalogue_fields.pop(name, None)
            else:
                parameter_id, factor = rule
                catalogue_fields[name] = mapped_value(parameters.get(str(parameter_id)), factor)
        requirements = self.group(group)
        result = {"group": list(group) if group else None, "filled": 0, "required": 0, "percent": None}
        if not self.sheets:
            result["state"] = "matrix_missing"
        elif requirements is None:
            result["state"] = "unmapped"
        elif not requirements["configured"]:
            result["state"] = "requirements_pending"
        else:
            result["state"] = "assessed"

        if details:
            result["fields"] = []
        if result["state"] != "assessed":
            return result

        for name, specification in requirements["fields"].items():
            state = specification["state"]
            if state not in ("fixed", "required"):
                continue
            if state == "fixed":
                value, origin, evidence = specification["value"], "matrix", None
            elif name in supplement.get("fields", {}):
                observation = supplement["fields"][name]
                value, origin, evidence = observation["value"], "supplement", observation["evidence"]
            else:
                value = catalogue_fields.get(name)
                origin, evidence = ("catalogue", record.get("source_url")) if value is not None else (None, None)
            result["required"] += 1
            result["filled"] += value is not None
            if details:
                field = {"name": name, "value": value, "origin": origin, "evidence": evidence}
                if origin == "supplement" and observation.get("pdf"):
                    field["pdf"] = observation["pdf"]
                if value is None and name in deferred:
                    field["deferred"] = True
                result["fields"].append(field)

        if result["required"]:
            result["percent"] = round(100 * result["filled"] / result["required"], 1)
        else:
            result["state"] = "no_required_fields"
        return result

    def summary(self, db):
        total = assessed = filled = required = 0
        reasons = {}
        for record, supplement, groups in assessment_rows(db, group_keys=self.group_mappings):
            part = self.part(record, supplement, groups)
            total += 1
            if part["percent"] is None:
                reason = part["state"]
                reasons[reason] = reasons.get(reason, 0) + 1
                continue
            assessed += 1
            filled += part["filled"]
            required += part["required"]
        return {"total": total, "assessed": assessed, "unassessed": total - assessed,
                "filled": filled, "required": required, "reasons": reasons,
                "percent": round(100 * filled / required, 1) if required else None}


def import_supplements(database, path, requirements):
    coverage = Coverage(requirements)
    known_fields = {name for groups in coverage.sheets.values() for group in groups.values()
                    for name in group["fields"]}
    items = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(items, list):
        raise ValueError("Дополнения должны быть JSON-массивом")

    database = Path(database).resolve()
    with closing(sqlite3.connect(database.as_uri() + "?mode=rw", uri=True, timeout=30)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        ensure_supplement_table(db)
        for index, raw in enumerate(items, 1):
            item = Supplement.model_validate(raw)
            if item.group is not None and coverage.group(item.group) is None:
                raise ValueError(f"Строка {index}: неизвестная подгруппа {item.group}")
            if not item.fields and item.group is None:
                raise ValueError(f"Строка {index}: добавьте поля или подгруппу")
            for name, observation in item.fields.items():
                if name not in known_fields:
                    raise ValueError(f"Строка {index}: неизвестное поле «{name}»")
                if not observation.evidence.strip() or (isinstance(observation.value, str) and not observation.value.strip()):
                    raise ValueError(f"Строка {index}: поле «{name}» требует значения и ссылки на источник")
            key = (item.source, item.source_id)
            if not db.execute("SELECT 1 FROM components WHERE source=? AND source_id=?", key).fetchone():
                raise ValueError(f"Строка {index}: компонент {key} отсутствует в сборе")
            existing = db.execute("SELECT payload FROM component_supplements WHERE source=? AND source_id=?", key).fetchone()
            merged = json.loads(existing[0]) if existing else {"fields": {}}
            if item.group is not None:
                merged["group"] = item.group
            merged["fields"].update({name: observation.model_dump(exclude_none=True)
                                     for name, observation in item.fields.items()})
            save_supplement(db, *key, merged)
    return len(items)


def write_report(db, assessment, output, *, replace=False):
    output = Path(output)

    def write_rows(stream):
        for record, supplement, groups in assessment_rows(db, group_keys=assessment.group_mappings):
            row = {key: record[key] for key in ("source", "source_id", "part_number", "manufacturer")}
            row.update(assessment.part(record, supplement, groups, details=True))
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    if not replace:
        try:
            with output.open("x", encoding="utf-8") as stream:
                write_rows(stream)
        except FileExistsError as error:
            raise ValueError(f"Файл уже существует: {output}") from error
        return

    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent, delete=False) as stream:
            temporary = Path(stream.name)
            write_rows(stream)
        temporary.replace(output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)

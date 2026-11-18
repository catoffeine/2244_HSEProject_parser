from collections import Counter
from contextlib import closing
from functools import lru_cache
from hashlib import file_digest
import json
from pathlib import Path
import sqlite3
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter

from ekb_parser.core.coverage import PDFCitation, PDFEvidence, assessment_rows, ensure_supplement_table, save_supplement
from ekb_parser.core.storage import saved_document_path


Nonempty = Annotated[str, Field(min_length=1)]
SHA256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
RuleID = Annotated[str, Field(min_length=1, max_length=160)]


class FieldScope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source: Nonempty
    group: Annotated[list[Nonempty], Field(min_length=2, max_length=2)]
    field: Nonempty


class Target(FieldScope):
    source_id: Nonempty
    part_number: Nonempty
    document_sha256: SHA256


class Task(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1] = 1
    target: Target
    manufacturer: Nonempty
    series: str | None
    pdf_path: Nonempty
    pdf_url: Nonempty


class Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)
    version: Literal[1]
    target: Target
    rule_id: RuleID
    value: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)] | int | float
    evidence: PDFCitation


class ApprovedRule(FieldScope):
    version: Literal[1]
    rule_id: RuleID
    document_sha256: SHA256 | None = None

    def key(self):
        return self.rule_id, self.source, tuple(self.group), self.field, self.document_sha256


APPROVALS = TypeAdapter(list[ApprovedRule])


def schemas():
    return {"task": Task.model_json_schema(), "candidate": Candidate.model_json_schema(),
            "approved_rules": APPROVALS.json_schema()}


def database_connection(output, mode):
    uri = (output / "crawl_state.sqlite3").resolve().as_uri()
    return sqlite3.connect(f"{uri}?mode={mode}", uri=True, timeout=30)


class Documents:
    def __init__(self, db, output):
        self.output = output.resolve()
        self.entries = {}
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='datasheets'").fetchone():
            self.entries = {(source, url): (digest, path) for source, url, digest, path in db.execute("""
                SELECT source, url, sha256, local_path FROM datasheets WHERE status = 'downloaded'
            """)}
        self.verified_path = lru_cache(maxsize=None)(self._verified_path)

    def _verified_path(self, digest, local_path):
        path = saved_document_path(self.output, local_path)
        if not path or not digest:
            return None
        try:
            with path.open("rb") as stream:
                if file_digest(stream, "sha256").hexdigest() == digest:
                    return path.relative_to(self.output).as_posix()
        except OSError:
            return None
        return None

    def get(self, record):
        url = record.get("datasheet_url")
        digest, local_path = self.entries.get((record["source"], url), (None, None))
        return digest, self.verified_path(digest, local_path)


def write_jsonl(stream, row):
    stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")


def prepare_tasks(output, assessment, stream, field=None):
    count = 0
    with closing(database_connection(output, "ro")) as db:
        db.execute("BEGIN")
        documents = Documents(db, output)
        entries = assessment_rows(db, where="json_extract(components.payload, '$.datasheet_url') IS NOT NULL",
                                  group_keys=assessment.group_mappings)
        for record, supplement, memberships in entries:
            digest, path = documents.get(record)
            if path is None:
                continue
            coverage = assessment.part(record, supplement, memberships, details=True)
            for item in coverage["fields"]:
                if item["value"] is not None or (field and item["name"] != field):
                    continue
                target = Target(source=record["source"], source_id=record["source_id"],
                                part_number=record["part_number"], group=coverage["group"],
                                field=item["name"], document_sha256=digest)
                task = Task(target=target, manufacturer=record["manufacturer"], series=record.get("series"),
                            pdf_path=path, pdf_url=record["datasheet_url"])
                write_jsonl(stream, task.model_dump(mode="json"))
                count += 1
    return count


def read_candidates(stream, report):
    batches = {}
    for line_number, line in enumerate(stream, 1):
        if not line.strip():
            continue
        try:
            candidate = Candidate.model_validate_json(line)
        except ValueError as error:
            report("invalid", line_number, reason=str(error))
            continue
        target = candidate.target
        fields = batches.setdefault((target.source, target.source_id), {})
        fields.setdefault(target.field, []).append((line_number, candidate))
    return batches


def candidate_status(candidate, approved, record, scope, current_fields, digest, path):
    target = candidate.target
    if record is None:
        return "component_missing"
    if record["part_number"] != target.part_number or scope != target.group:
        return "target_changed"
    if path is None or digest != target.document_sha256:
        return "pdf_changed_or_missing"
    if target.field not in current_fields:
        return "field_unavailable"
    if current_fields[target.field] is not None:
        return "already_filled"
    key = candidate.rule_id, target.source, tuple(target.group), target.field
    if (*key, None) not in approved and (*key, digest) not in approved:
        return "rule_not_approved"
    return None


def apply_candidates(output, assessment, stream, report_stream, rules=(), *, commit=False):
    counts = Counter()

    def report(status, line, candidate=None, **details):
        counts[status] += 1
        if candidate is not None:
            details.update(target=candidate.target.model_dump(mode="json"), rule_id=candidate.rule_id,
                           value=candidate.value, evidence=candidate.evidence.model_dump(exclude_none=True))
        write_jsonl(report_stream, {"line": line, "status": status, **details})

    batches = read_candidates(stream, report)
    approved = {rule.key() for rule in rules}
    with closing(database_connection(output, "rw" if commit else "ro")) as db, db:
        db.execute("BEGIN IMMEDIATE" if commit else "BEGIN")
        documents = Documents(db, output)
        supplement_table_ready = False
        for (source, source_id), fields in batches.items():
            entries = assessment_rows(db, where="components.source = ? AND components.source_id = ?",
                                      params=(source, source_id), group_keys=assessment.group_mappings)
            record, supplement, memberships = next(entries, (None, {}, ()))
            entries.close()
            coverage = assessment.part(record, supplement, memberships, details=True) if record else {"group": None, "fields": []}
            current_fields = {item["name"]: item["value"] for item in coverage["fields"] if item["origin"] != "matrix"}
            digest, path = documents.get(record) if record else (None, None)
            updates = {}
            for candidates in fields.values():
                first_line, candidate = candidates[0]
                if any(other != candidate for _, other in candidates[1:]):
                    for line, other in candidates:
                        report("conflict", line, other)
                    continue
                for line, duplicate in candidates[1:]:
                    report("duplicate", line, duplicate)
                status = candidate_status(candidate, approved, record, coverage["group"], current_fields, digest, path)
                if status:
                    report(status, first_line, candidate)
                    continue
                reference = PDFEvidence(**candidate.evidence.model_dump(), url=record["datasheet_url"],
                                        sha256=digest, rule_id=candidate.rule_id)
                evidence = f"{reference.url} · стр. {reference.page} · {reference.quote}"
                if reference.conditions:
                    evidence += f" · {reference.conditions}"
                updates[candidate.target.field] = {"value": candidate.value, "evidence": evidence,
                                                   "pdf": reference.model_dump(exclude_none=True)}
                report("applied" if commit else "ready", first_line, candidate)
            if updates and commit:
                if not supplement_table_ready:
                    ensure_supplement_table(db)
                    supplement_table_ready = True
                supplement.setdefault("fields", {}).update(updates)
                save_supplement(db, source, source_id, supplement)
        report_stream.flush()
    return dict(counts)

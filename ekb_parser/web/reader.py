from contextlib import closing
from datetime import datetime, timezone
from functools import lru_cache
from hashlib import sha256
import json
import sqlite3

from ekb_parser.core.catalogue_mappings import load_mappings, query_key
from ekb_parser.core.config import load_config
from ekb_parser.core.coverage import Coverage, assessment_rows
from ekb_parser.core.requirements import file_version, load_requirements
from ekb_parser.core.storage import saved_document_path

PAGE_SIZE = 200


def read_connection(output):
    path = output / "crawl_state.sqlite3"
    return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)


def saved_collections(root):
    data = (root / "data").resolve()
    for path in sorted(data.rglob("crawl_state.sqlite3")):
        if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(data):
            yield path.parent


def configuration_files(root):
    configs = (root / "configs").resolve()
    for path in sorted(configs.rglob("*.json")):
        if path.is_file() and path.resolve().is_relative_to(configs):
            yield path


def collection_version(output):
    return tuple(file_version(output / name) for name in (
        "crawl_state.sqlite3", "crawl_state.sqlite3-wal", "crawl_state.sqlite3-journal", "collection_config.json",
    ))


def identity(*values):
    data = json.dumps(values, ensure_ascii=False)
    return sha256(data.encode("utf-8")).hexdigest()[:24]


@lru_cache(maxsize=256)
def saved_groups(output, version):
    with closing(read_connection(output)) as db:
        rows = db.execute("""
            SELECT member.source, groups.key, groups.name, groups.query, COUNT(*)
            FROM membership AS member JOIN groups ON groups.key = member.group_key
            JOIN components USING (source, source_id)
            GROUP BY groups.key, member.source
        """)
        return [(source, key, name, json.loads(query), count) for source, key, name, query, count in rows]


def catalogue_index(root, source):
    categories, targets, best = {}, {}, {}
    for path in configuration_files(root):
        try:
            config = load_config(path)
        except (OSError, ValueError):
            continue
        if config.source != source:
            continue
        category_id = identity(source, path.relative_to(root).as_posix())
        category = {"id": category_id, "name": config.name or path.stem, "groups": {}}
        categories[category_id] = category
        for group in config.groups:
            key = query_key(source, group.query)
            group_id = identity(category_id, group.key)
            category["groups"][group_id] = {"id": group_id, "name": group.name, "count": 0, "updated": None}
            targets.setdefault(key, []).append((category_id, group_id))

    unlisted_id = identity(source, "unlisted")
    for output in saved_collections(root):
        version = collection_version(output)
        if version[0] is None:
            continue
        try:
            groups = saved_groups(output, version)
        except (OSError, ValueError, sqlite3.Error):
            continue
        modified = max(item[2] for item in version[:3] if item is not None)
        for saved_source, key, name, query, count in groups:
            if saved_source != source:
                continue
            query_id = query_key(source, query)
            matches = targets.get(query_id)
            if not matches:
                category = categories.setdefault(unlisted_id, {"id": unlisted_id, "name": "Другие сохранённые группы", "groups": {}})
                group_id = identity(unlisted_id, query_id)
                category["groups"].setdefault(group_id, {"id": group_id, "name": name, "count": 0, "updated": None})
                matches = [(unlisted_id, group_id)]
            for location in matches:
                rank = (count, modified, output.as_posix(), key)
                if location not in best or rank > best[location][0]:
                    best[location] = (rank, output, key)

    for (category_id, group_id), ((count, modified, _, _), _, _) in best.items():
        categories[category_id]["groups"][group_id].update(
            count=count, updated=datetime.fromtimestamp(modified / 1e9, timezone.utc).isoformat(),
        )
    return list(categories.values()), best


def saved_analyst_groups(root, source, matrix_version, mappings_version):
    groups = {}
    for output in saved_collections(root):
        version = collection_version(output)
        if version[0] is None:
            continue
        try:
            collected = collection_analyst_groups(root, output, source, matrix_version, mappings_version, version)
        except (OSError, ValueError, sqlite3.Error):
            continue
        for group, complete in collected.items():
            groups[group] = groups.get(group, True) and complete
    return groups


@lru_cache(maxsize=256)
def collection_analyst_groups(root, output, source, matrix_version, mappings_version, run_version):
    assessment = Coverage(load_requirements(root, matrix_version), output, load_mappings(root))
    groups = {}
    with closing(read_connection(output)) as db:
        db.execute("BEGIN")
        entries = assessment_rows(db, where="components.source=?", params=(source,), group_keys=assessment.group_mappings)
        for record, supplement, memberships in entries:
            coverage = assessment.part(record, supplement, memberships)
            group = coverage["group"]
            if assessment.group(group) is not None:
                key = tuple(group)
                complete = coverage["required"] > 0 and coverage["filled"] == coverage["required"]
                groups[key] = groups.get(key, True) and complete
    return groups


def listing(output, source, group, search, offset, assessment):
    condition = """FROM membership AS member JOIN components USING (source, source_id)
                   WHERE member.group_key = ? AND source = ?"""
    params = [group, source]
    if search:
        condition += """ AND (instr(lower(part_number), lower(?))
                              OR instr(lower(manufacturer), lower(?))
                              OR instr(lower(json_extract(payload, '$.series')), lower(?)))"""
        params.extend([search] * 3)
    with closing(read_connection(output)) as db:
        db.execute("BEGIN")
        total = db.execute("SELECT COUNT(*) " + condition, params).fetchone()[0]
        offset = min(offset, ((max(total, 1) - 1) // PAGE_SIZE) * PAGE_SIZE)
        records = [dict(zip(("source_id", "part_number", "manufacturer", "series", "has_datasheet"), row))
                   for row in db.execute(
            """SELECT source_id, part_number, manufacturer, json_extract(payload, '$.series') AS series,
                      json_extract(payload, '$.datasheet_url') IS NOT NULL AS has_datasheet """
            + condition + " ORDER BY part_number COLLATE NOCASE, source_id LIMIT ? OFFSET ?",
            [*params, PAGE_SIZE, offset],
        )]
        if records:
            ids = [row["source_id"] for row in records]
            placeholders = ", ".join("?" for _ in ids)
            entries = assessment_rows(
                db, where=f"components.source=? AND components.source_id IN ({placeholders})", params=(source, *ids),
            )
            coverage = {record["source_id"]: assessment.part(record, supplement, (group,))
                        for record, supplement, _ in entries}
            for row in records:
                row["coverage"] = coverage[row["source_id"]]
        return records, total, offset


@lru_cache(maxsize=256)
def group_statistics(root, output, source, group, matrix_version, mappings_version, run_version):
    assessment = Coverage(load_requirements(root, matrix_version), output, load_mappings(root))
    result = dict(total=0, complete=0, partial=0, empty=0, unassessed=0, filled=0, required=0, with_datasheet=0)
    with closing(read_connection(output)) as db:
        db.execute("BEGIN")
        entries = assessment_rows(db, where="""components.source=? AND components.source_id IN (
            SELECT source_id FROM membership WHERE group_key=? AND source=?)""", params=(source, group, source))
        for record, supplement, _ in entries:
            coverage = assessment.part(record, supplement, (group,))
            result["total"] += 1
            result["with_datasheet"] += bool(record.get("datasheet_url"))
            result["filled"] += coverage["filled"]
            result["required"] += coverage["required"]
            if coverage["percent"] is None:
                result["unassessed"] += 1
            elif coverage["filled"] == coverage["required"]:
                result["complete"] += 1
            elif coverage["filled"]:
                result["partial"] += 1
            else:
                result["empty"] += 1
    return result


def component(output, source, group, component_id, assessment):
    with closing(read_connection(output)) as db:
        db.execute("BEGIN")
        rows = assessment_rows(db, where="""components.source = ? AND components.source_id = ?
            AND EXISTS (SELECT 1 FROM membership WHERE group_key = ? AND source = components.source
                        AND source_id = components.source_id)""", params=(source, component_id, group))
        entry = next(rows, None)
        rows.close()
        if not entry:
            return None, None
        record, supplement, _ = entry
        if assessment:
            record["coverage"] = assessment.part(record, supplement, (group,), details=True)
        record.pop("raw", None)
        document = None
        if record.get("datasheet_url") and db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='datasheets'"
        ).fetchone():
            saved = db.execute("SELECT local_path FROM datasheets WHERE source=? AND url=?",
                               (source, record["datasheet_url"])).fetchone()
            if saved:
                document = saved_document_path(output, saved[0])
        return record, document

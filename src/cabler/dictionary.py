"""The data dictionary: what every schema, table, view, column and function of the database means.

The descriptions are comments in the database (``COMMENT ON``), written by the migrations next to
what they describe. This module reads them from the system catalogue and renders
``docs/data-dictionary.md``, so the document is never written by hand and cannot drift from the
schema. A column of a view that only passes a column of a table on takes the description of that
column; a column that the view computes needs its own.

:func:`missing` names everything without a description; the tests fail when it is not empty, so
every migration documents what it adds.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import psycopg

SCHEMAS = ["meta", "raw", "staging", "core", "results"]
KINDS = {"r": "table", "p": "table", "v": "view", "m": "materialized view"}

SCHEMA_SQL = """
SELECT n.nspname, obj_description(n.oid, 'pg_namespace')
FROM pg_namespace n
WHERE n.nspname = ANY(%(schemas)s)
ORDER BY array_position(%(schemas)s, n.nspname::text)
"""
RELATION_SQL = """
SELECT n.nspname, c.relname, c.relkind::text, obj_description(c.oid, 'pg_class')
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = ANY(%(schemas)s) AND c.relkind IN ('r', 'p', 'v', 'm')
ORDER BY array_position(%(schemas)s, n.nspname::text), c.relname
"""
COLUMN_SQL = """
SELECT n.nspname, c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,
       col_description(c.oid, a.attnum)
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
WHERE n.nspname = ANY(%(schemas)s) AND c.relkind IN ('r', 'p', 'v', 'm')
ORDER BY array_position(%(schemas)s, n.nspname::text), c.relname, a.attnum
"""
# For every column of a view: the columns of the same name in the tables and views it reads, from
# the dependencies of the view's rule (information_schema would hide tables of other owners).
PASSED_ON_SQL = """
SELECT vn.nspname, v.relname, s.attname, tn.nspname, t.relname
FROM pg_rewrite r
JOIN pg_class v ON v.oid = r.ev_class AND v.relkind IN ('v', 'm')
JOIN pg_namespace vn ON vn.oid = v.relnamespace
JOIN pg_depend d ON d.classid = 'pg_rewrite'::regclass AND d.objid = r.oid
                AND d.refclassid = 'pg_class'::regclass AND d.refobjsubid > 0
                AND d.refobjid <> v.oid
JOIN pg_class t ON t.oid = d.refobjid
JOIN pg_namespace tn ON tn.oid = t.relnamespace
JOIN pg_attribute s ON s.attrelid = t.oid AND s.attnum = d.refobjsubid
WHERE vn.nspname = ANY(%(schemas)s)
  AND EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = v.oid AND a.attname = s.attname)
ORDER BY 1, 2, 3, 4, 5
"""
FUNCTION_SQL = """
SELECT n.nspname, p.proname, pg_get_function_identity_arguments(p.oid),
       pg_get_function_result(p.oid), obj_description(p.oid, 'pg_proc')
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = ANY(%(schemas)s) AND p.prokind = 'f'
  AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e')
ORDER BY array_position(%(schemas)s, n.nspname::text), p.proname,
         pg_get_function_identity_arguments(p.oid)
"""
MIGRATION_SQL = "SELECT coalesce(max(version), 0) FROM meta.schema_migration"


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    not_null: bool
    comment: str | None
    inherited: str | None = None  # the description of the table column a view passes on

    @property
    def description(self) -> str | None:
        return self.comment or self.inherited


@dataclass(frozen=True)
class Relation:
    schema: str
    name: str
    kind: str  # table | view | materialized view
    comment: str | None
    columns: tuple[Column, ...]


@dataclass(frozen=True)
class Function:
    schema: str
    name: str
    arguments: str
    result: str
    comment: str | None


@dataclass(frozen=True)
class Dictionary:
    migration: int
    schemas: tuple[tuple[str, str | None], ...]
    relations: tuple[Relation, ...]
    functions: tuple[Function, ...]


Key = tuple[str, str, str]  # schema, relation, column


def passed_on(
    comments: dict[Key, str | None], sources: dict[Key, list[tuple[str, str]]]
) -> dict[Key, str]:
    """The description that each view column without a comment takes from what it reads.

    A view may read another view, so the search follows the chain. A view that joins two tables
    on a column of the same name reads both; when their descriptions differ, nothing is guessed:
    the view column gets none and needs a comment of its own.
    """

    def find(key: Key, seen: frozenset[Key]) -> set[str]:
        if comments.get(key):
            return {str(comments[key])}
        found: set[str] = set()
        for source_schema, source in sources.get(key, []):
            nxt = (source_schema, source, key[2])
            if nxt not in seen:
                found |= find(nxt, seen | {nxt})
        return found

    result = {}
    for key in sources:
        if not comments.get(key):
            texts = find(key, frozenset({key}))
            if len(texts) == 1:
                result[key] = texts.pop()
    return result


def read(conn: psycopg.Connection[Any]) -> Dictionary:
    """Everything the dictionary shows, from the system catalogue. Nothing is written."""
    params = {"schemas": SCHEMAS}
    row = conn.execute(MIGRATION_SQL).fetchone()
    rows = conn.execute(COLUMN_SQL, params).fetchall()
    sources: dict[Key, list[tuple[str, str]]] = {}
    for view_schema, view, column, schema, table in conn.execute(PASSED_ON_SQL, params).fetchall():
        sources.setdefault((view_schema, view, column), []).append((schema, table))
    inherited = passed_on({(r[0], r[1], r[2]): r[5] for r in rows}, sources)
    columns: dict[tuple[str, str], list[Column]] = {}
    for schema, table, name, type_, not_null, comment in rows:
        columns.setdefault((schema, table), []).append(
            Column(name, type_, bool(not_null), comment, inherited.get((schema, table, name)))
        )
    relations = tuple(
        Relation(schema, name, KINDS[kind], comment, tuple(columns.get((schema, name), [])))
        for schema, name, kind, comment in conn.execute(RELATION_SQL, params).fetchall()
    )
    return Dictionary(
        migration=int(row[0]) if row else 0,
        schemas=tuple((str(s), c) for s, c in conn.execute(SCHEMA_SQL, params).fetchall()),
        relations=relations,
        functions=tuple(Function(*f) for f in conn.execute(FUNCTION_SQL, params).fetchall()),
    )


def missing(d: Dictionary) -> list[str]:
    """Every schema, table, view, column and function that has no description."""
    found = [f"schema {name}" for name, comment in d.schemas if not comment]
    for r in d.relations:
        if not r.comment:
            found.append(f"{r.kind} {r.schema}.{r.name}")
        found += [f"column {r.schema}.{r.name}.{c.name}" for c in r.columns if not c.description]
    found += [f"function {f.schema}.{f.name}({f.arguments})" for f in d.functions if not f.comment]
    return found


def _cell(text: str | None) -> str:
    """Text that fits into one cell of a Markdown table."""
    return (text or "").replace("|", "\\|").replace("\n", " ")


def render(d: Dictionary) -> str:
    """The Markdown document: one section per schema, one table per relation."""
    lines = [
        "# Data dictionary",
        "",
        f"Generated by `make dictionary` from the comments in the database (migration "
        f"{d.migration:04d}).",
        "Do not edit it by hand: change the comment in a new migration and generate it again.",
        "A view column without its own comment shows the description of the column it passes on.",
    ]
    for schema, comment in d.schemas:
        lines += ["", f"## {schema}", "", _cell(comment)]
        for r in (r for r in d.relations if r.schema == schema):
            lines += ["", f"### {r.schema}.{r.name} ({r.kind})", "", _cell(r.comment), ""]
            lines += ["| column | type | null | description |", "| --- | --- | --- | --- |"]
            lines += [
                f"| `{c.name}` | {_cell(c.type)} | {'no' if c.not_null else 'yes'} "
                f"| {_cell(c.description)} |"
                for c in r.columns
            ]
        functions = [f for f in d.functions if f.schema == schema]
        if functions:
            lines += ["", f"### Functions in {schema}", ""]
            lines += ["| function | returns | description |", "| --- | --- | --- |"]
            lines += [
                f"| `{f.schema}.{f.name}({_cell(f.arguments)})` | {_cell(f.result)} "
                f"| {_cell(f.comment)} |"
                for f in functions
            ]
    return "\n".join(lines) + "\n"

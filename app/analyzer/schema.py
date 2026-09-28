# ---------------------------------------------------------------------
# Oracle SQL Preflight
# Author:  Federico Guzman  (github.com/kraiosis)
# Website: https://fedeguzman.com    Blog: https://weblantropia.com
#
# Built with AI assistance from Claude (Anthropic). The analyzer's
# runtime behavior stays deterministic and AI-free -- see README.md,
# "Author & Credits", for what the AI assistance covers.
# ---------------------------------------------------------------------
"""
Oracle SQL Preflight Analyzer - Offline Schema Definitions

scope.md section 3 (Phase 1 "Planned expansion") lists:

    schema definition to validate queries and data

This is filed under Phase 1 -- the OFFLINE foundation -- not under
section 7/8 (the live Oracle connection). This module implements that:
the user pastes/imports Oracle DDL (CREATE TABLE ...), it is parsed with
SQLGlot and stored locally as config/schema.json, and the engine can then
check SQL against it without ever connecting to a database.

This is explicitly NOT the same thing as the live-connected metadata
analyzer in section 8. A user-provided schema can be stale -- it reflects
what the user told the tool, not what Oracle currently has. Findings
based on it use a distinct confidence tier, SCHEMA-VERIFIED (see
engine.py), so they are never confused with an actual DATABASE-VERIFIED
fact from a live connection (scope.md 2.5, "conservative conclusions").

Coverage is expected to be partial: most schemas here will only describe
a handful of tables a DBA cares about, not a full database. So schema
checks only ever fire for a table this module actually knows about --
an unknown table is silently ignored rather than flagged, to avoid
false positives from incomplete coverage.
"""

from __future__ import annotations

import json
import pathlib
import threading
from dataclasses import dataclass, field, asdict
from typing import Optional

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

DIALECT = "oracle"

_DEFAULT_CONFIG_PATH = (
    pathlib.Path(__file__).resolve().parents[2] / "config" / "schema.json"
)

_lock = threading.RLock()


TYPE_CATEGORY_NUMERIC = "NUMERIC"
TYPE_CATEGORY_STRING = "STRING"
TYPE_CATEGORY_DATE = "DATE"
TYPE_CATEGORY_OTHER = "OTHER"

_NUMERIC_TYPES = {
    exp.DataType.Type.DECIMAL, exp.DataType.Type.INT, exp.DataType.Type.BIGINT,
    exp.DataType.Type.SMALLINT, exp.DataType.Type.TINYINT, exp.DataType.Type.FLOAT,
    exp.DataType.Type.DOUBLE, exp.DataType.Type.UDECIMAL,
    exp.DataType.Type.UINT, exp.DataType.Type.UBIGINT, exp.DataType.Type.USMALLINT,
    exp.DataType.Type.UTINYINT,
}
_STRING_TYPES = {
    exp.DataType.Type.VARCHAR, exp.DataType.Type.CHAR, exp.DataType.Type.TEXT,
    exp.DataType.Type.NVARCHAR, exp.DataType.Type.NCHAR, exp.DataType.Type.VARBINARY,
}
_DATE_TYPES = {
    exp.DataType.Type.DATE, exp.DataType.Type.DATETIME, exp.DataType.Type.TIMESTAMP,
    exp.DataType.Type.TIMESTAMPTZ,
}


@dataclass
class ColumnDef:
    name: str
    data_type: str = ""
    nullable: bool = True
    is_primary_key: bool = False
    type_category: str = TYPE_CATEGORY_OTHER  # NUMERIC / STRING / DATE / OTHER
    max_length: Optional[int] = None          # VARCHAR2(n)/CHAR(n) -- else None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class TableDef:
    name: str
    columns: dict[str, ColumnDef] = field(default_factory=dict)  # key = UPPER(name)
    primary_key: list[str] = field(default_factory=list)         # UPPER column names
    source: str = "ddl"  # "ddl" (imported) -- reserved for future import sources

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "columns": [c.to_dict() for c in self.columns.values()],
            "primary_key": self.primary_key,
            "source": self.source,
        }

    def has_column(self, column_name: str) -> bool:
        return column_name.upper() in self.columns


class SchemaImportError(Exception):
    pass


# --------------------------------------------------------------------------
# DDL parsing
# --------------------------------------------------------------------------

def _classify_data_type(dtype_node: Optional[exp.DataType]) -> tuple[str, Optional[int]]:
    """Classify a column's DataType node into a coarse category (NUMERIC /
    STRING / DATE / OTHER) and, for VARCHAR2/CHAR-family types, its
    declared max length. Used by the Oracle-error-catalog checks in
    engine.py (ORA-002 invalid number, ORA-004 value too large) -- these
    only need "is this numeric/string" and "how many characters fit",
    not the full Oracle type system.
    """
    if dtype_node is None:
        return TYPE_CATEGORY_OTHER, None

    base = dtype_node.this
    if base in _NUMERIC_TYPES:
        return TYPE_CATEGORY_NUMERIC, None
    if base in _DATE_TYPES:
        return TYPE_CATEGORY_DATE, None
    if base in _STRING_TYPES:
        max_length = None
        params = dtype_node.expressions or []
        if params:
            first = params[0]
            literal = first.this if hasattr(first, "this") else first
            try:
                max_length = int(str(literal.this if hasattr(literal, "this") else literal))
            except (TypeError, ValueError):
                max_length = None
        return TYPE_CATEGORY_STRING, max_length
    return TYPE_CATEGORY_OTHER, None


def _extract_table_from_create(stmt: exp.Create) -> Optional[TableDef]:
    if (stmt.args.get("kind") or "").upper() != "TABLE":
        return None

    schema_node = stmt.this
    table_node = schema_node.this if isinstance(schema_node, exp.Schema) else schema_node
    if not isinstance(table_node, exp.Table):
        return None
    table_name = table_node.name
    if not table_name:
        return None

    columns: dict[str, ColumnDef] = {}
    pk_columns: list[str] = []

    body = schema_node.expressions if isinstance(schema_node, exp.Schema) else []

    for item in body:
        if isinstance(item, exp.ColumnDef):
            col_name = item.this.this if item.this else None
            if not col_name:
                continue
            dtype_node = item.args.get("kind")
            dtype = dtype_node.sql(dialect=DIALECT) if dtype_node else ""
            type_category, max_length = _classify_data_type(dtype_node)
            nullable = True
            is_pk = False
            for c in item.constraints or []:
                kind = c.args.get("kind")
                if isinstance(kind, exp.NotNullColumnConstraint):
                    nullable = False
                if isinstance(kind, exp.PrimaryKeyColumnConstraint):
                    is_pk = True
                    nullable = False
            key = str(col_name).upper()
            columns[key] = ColumnDef(
                name=str(col_name), data_type=dtype, nullable=nullable, is_primary_key=is_pk,
                type_category=type_category, max_length=max_length,
            )
            if is_pk:
                pk_columns.append(key)

        elif isinstance(item, (exp.PrimaryKey, exp.Constraint)):
            pk_node = item if isinstance(item, exp.PrimaryKey) else None
            if pk_node is None:
                for sub in item.expressions or []:
                    if isinstance(sub, exp.PrimaryKey):
                        pk_node = sub
                        break
            if pk_node is not None:
                for col_expr in pk_node.expressions:
                    col = col_expr.find(exp.Column) or col_expr.find(exp.Identifier)
                    name = col.name if isinstance(col, exp.Column) else (
                        col.this if isinstance(col, exp.Identifier) else None)
                    if name:
                        key = str(name).upper()
                        pk_columns.append(key)
                        if key in columns:
                            columns[key].is_primary_key = True
                            columns[key].nullable = False

    if not columns:
        return None

    # de-duplicate PK column list while preserving order
    seen = set()
    ordered_pk = []
    for c in pk_columns:
        if c not in seen:
            seen.add(c)
            ordered_pk.append(c)

    return TableDef(name=table_name, columns=columns, primary_key=ordered_pk)


def parse_ddl(ddl_text: str) -> tuple[list[TableDef], list[str]]:
    """Parse one or more CREATE TABLE statements out of a block of DDL text.

    Returns (tables, errors). Non-CREATE-TABLE statements (e.g. CREATE
    INDEX, ALTER TABLE) are silently skipped in this MVP -- they are not
    an error, just not yet supported. A genuinely unparseable statement is
    reported as an error string but does not stop the rest of the batch
    from being processed.
    """
    ddl_text = (ddl_text or "").strip()
    if not ddl_text:
        return [], []

    tables: list[TableDef] = []
    errors: list[str] = []

    try:
        statements = sqlglot.parse(ddl_text, read=DIALECT)
    except ParseError as e:
        return [], [str(e)]

    for stmt in statements:
        if stmt is None:
            continue
        if not isinstance(stmt, exp.Create):
            continue
        try:
            table = _extract_table_from_create(stmt)
            if table is not None:
                tables.append(table)
        except Exception as e:  # defensive: never let one bad statement crash import
            errors.append(f"Could not parse statement: {e}")

    if not tables and not errors:
        errors.append("No CREATE TABLE statements were found in the supplied DDL.")

    return tables, errors


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

def _table_from_dict(d: dict) -> TableDef:
    columns = {}
    for c in d.get("columns", []):
        col = ColumnDef(
            name=c["name"],
            data_type=c.get("data_type", ""),
            nullable=bool(c.get("nullable", True)),
            is_primary_key=bool(c.get("is_primary_key", False)),
            type_category=c.get("type_category", TYPE_CATEGORY_OTHER),
            max_length=c.get("max_length"),
        )
        columns[col.name.upper()] = col
    return TableDef(
        name=d["name"],
        columns=columns,
        primary_key=[str(x).upper() for x in d.get("primary_key", [])],
        source=d.get("source", "ddl"),
    )


def load_schema(path: Optional[pathlib.Path] = None) -> dict[str, TableDef]:
    """Load the offline schema catalog from config/schema.json.

    Missing file -> empty schema (nothing to check against, no findings
    ever fire -- this feature is fully opt-in). Corrupt file -> empty
    schema as well; the file is left alone for the user to fix or
    re-import over.
    """
    cfg_path = path or _DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        return {}
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        tables = raw.get("tables", []) if isinstance(raw, dict) else raw
        result: dict[str, TableDef] = {}
        for d in tables:
            t = _table_from_dict(d)
            result[t.name.upper()] = t
        return result
    except (json.JSONDecodeError, KeyError, TypeError, OSError):
        return {}


def save_schema(tables: dict[str, TableDef], path: Optional[pathlib.Path] = None) -> None:
    cfg_path = path or _DEFAULT_CONFIG_PATH
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"tables": [t.to_dict() for t in tables.values()]}
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


# --------------------------------------------------------------------------
# Module-level catalog
# --------------------------------------------------------------------------

SCHEMA: dict[str, TableDef] = load_schema()


def reload_schema(path: Optional[pathlib.Path] = None) -> dict[str, TableDef]:
    global SCHEMA
    with _lock:
        SCHEMA = load_schema(path)
        return SCHEMA


def get_table(table_name: str) -> Optional[TableDef]:
    with _lock:
        return SCHEMA.get(table_name.upper())


def all_tables() -> list[TableDef]:
    with _lock:
        return list(SCHEMA.values())


def import_ddl(ddl_text: str, *, persist: bool = True) -> dict:
    """Parse DDL text and merge the resulting tables into the schema
    (a re-imported table replaces the previous definition of that table).
    Returns a summary dict describing what happened.
    """
    tables, errors = parse_ddl(ddl_text)
    with _lock:
        for t in tables:
            SCHEMA[t.name.upper()] = t
        if persist and tables:
            save_schema(SCHEMA)
    return {
        "imported": [t.name for t in tables],
        "errors": errors,
        "table_count": len(SCHEMA),
    }


def remove_table(table_name: str, *, persist: bool = True) -> bool:
    with _lock:
        key = table_name.upper()
        if key not in SCHEMA:
            return False
        del SCHEMA[key]
        if persist:
            save_schema(SCHEMA)
        return True


def clear_schema(*, persist: bool = True) -> None:
    with _lock:
        SCHEMA.clear()
        if persist:
            save_schema(SCHEMA)

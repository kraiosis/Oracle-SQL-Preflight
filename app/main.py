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
Oracle SQL Preflight Analyzer - FastAPI application

Local-only, offline-first web server (scope.md section 2.1, README.md
section 13 "Security model"). Binds to 127.0.0.1 by default and performs
no outbound network calls.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import time
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import Optional

from .analyzer.engine import analyze_sql, format_sql, overall_status
from .analyzer.rules import (
    all_rules,
    reload_rules,
    update_rule,
    RuleConfigError,
    VALID_SEVERITIES,
)
from .analyzer import schema as schema_mod

APP_DIR = pathlib.Path(__file__).resolve().parent
WEB_DIR = APP_DIR / "web"
DB_PATH = APP_DIR.parent / "config" / "history.sqlite3"

app = FastAPI(
    title="Oracle SQL Preflight Analyzer",
    version="0.1.0",
    description="Offline-first Oracle DBA SQL preflight and impact-analysis layer.",
)

app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))


# --------------------------------------------------------------------------
# Local SQLite history store (scope.md section 16)
# --------------------------------------------------------------------------

def _get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sql_text TEXT NOT NULL,
            status TEXT NOT NULL,
            findings_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    return conn


class AnalyzeRequest(BaseModel):
    sql: str


class FormatRequest(BaseModel):
    sql: str


class RuleUpdateRequest(BaseModel):
    enabled: Optional[bool] = None
    severity: Optional[str] = None


class SchemaImportRequest(BaseModel):
    ddl: str


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {})


@app.get("/api/health")
def health():
    return {"status": "ok", "mode": "offline", "ai": False, "version": "0.1.0"}


@app.get("/api/rules")
def list_rules():
    """Return the live rule catalog, loaded from config/rules.json.
    This is what the Rule catalog view in the UI renders (scope.md
    section 18, "Rule management": enable/disable, severity config,
    category filtering happens client-side over this list).
    """
    return {
        "valid_severities": list(VALID_SEVERITIES),
        "rules": [
            {
                "id": r.id,
                "category": r.category,
                "severity": r.severity,
                "title": r.title,
                "description": r.description,
                "recommendation": r.recommendation,
                "requires_database": r.requires_database,
                "enabled": r.enabled,
            }
            for r in all_rules()
        ],
    }


@app.patch("/api/rules/{rule_id}")
def patch_rule(rule_id: str, payload: RuleUpdateRequest):
    """Update a single rule's enabled state and/or severity, and persist
    the change back to config/rules.json immediately.
    """
    try:
        rule = update_rule(rule_id, severity=payload.severity, enabled=payload.enabled)
    except KeyError:
        return JSONResponse({"error": f"Unknown rule id: {rule_id}"}, status_code=404)
    except RuleConfigError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    return {
        "id": rule.id,
        "category": rule.category,
        "severity": rule.severity,
        "title": rule.title,
        "description": rule.description,
        "recommendation": rule.recommendation,
        "requires_database": rule.requires_database,
        "enabled": rule.enabled,
    }


@app.post("/api/rules/reload")
def reload_rule_catalog():
    """Re-read config/rules.json from disk -- useful after hand-editing
    the file directly instead of using the UI/API.
    """
    rules = reload_rules()
    return {"reloaded": True, "rule_count": len(rules)}


def _table_to_json(t: "schema_mod.TableDef") -> dict:
    return {
        "name": t.name,
        "primary_key": t.primary_key,
        "source": t.source,
        "columns": [
            {
                "name": c.name,
                "data_type": c.data_type,
                "nullable": c.nullable,
                "is_primary_key": c.is_primary_key,
            }
            for c in t.columns.values()
        ],
    }


@app.get("/api/schema")
def list_schema():
    """Return the offline schema catalog imported from DDL (scope.md
    section 3, "schema definition to validate queries and data"). This
    is entirely separate from a live Oracle connection (section 7/8,
    not implemented) -- it only reflects what the user has imported here.
    """
    tables = schema_mod.all_tables()
    return {
        "table_count": len(tables),
        "tables": [_table_to_json(t) for t in tables],
    }


@app.post("/api/schema/import")
def import_schema(payload: SchemaImportRequest):
    """Parse CREATE TABLE DDL and merge the resulting tables into the
    offline schema catalog, persisting to config/schema.json. Statements
    that aren't CREATE TABLE are skipped silently; a genuinely
    unparseable statement is reported back as an error string rather
    than failing the whole import.
    """
    result = schema_mod.import_ddl(payload.ddl, persist=True)
    return result


@app.delete("/api/schema/{table_name}")
def delete_schema_table(table_name: str):
    removed = schema_mod.remove_table(table_name, persist=True)
    if not removed:
        return JSONResponse(
            {"error": f"Table not found in imported schema: {table_name}"},
            status_code=404,
        )
    return {"removed": table_name, "table_count": len(schema_mod.all_tables())}


@app.post("/api/schema/clear")
def clear_schema_endpoint():
    schema_mod.clear_schema(persist=True)
    return {"cleared": True, "table_count": 0}


@app.post("/api/analyze")
def api_analyze(payload: AnalyzeRequest):
    start = time.perf_counter()
    results = analyze_sql(payload.sql)
    elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
    status = overall_status(results)

    response = {
        "status": status,
        "elapsed_ms": elapsed_ms,
        "statement_count": len(results),
        "statements": [r.to_dict() for r in results],
    }

    # Save to local history (best-effort; history failures must never
    # block analysis, since analysis must keep working fully offline).
    try:
        conn = _get_db()
        with conn:
            conn.execute(
                "INSERT INTO history (sql_text, status, findings_json, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    payload.sql,
                    status,
                    json.dumps(response["statements"]),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        conn.close()
    except Exception:
        pass

    return JSONResponse(response)


@app.post("/api/format")
def api_format(payload: FormatRequest):
    return format_sql(payload.sql)


@app.get("/api/history")
def api_history(limit: int = 25):
    try:
        conn = _get_db()
        cur = conn.execute(
            "SELECT id, sql_text, status, created_at FROM history "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        rows = [
            {"id": r[0], "sql": r[1], "status": r[2], "created_at": r[3]}
            for r in cur.fetchall()
        ]
        conn.close()
        return {"history": rows}
    except Exception as e:
        return JSONResponse({"history": [], "error": str(e)}, status_code=200)


def main():
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    main()

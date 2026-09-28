"""
Tests for the offline schema module (schema.py) and the schema-aware
engine checks it enables (SCHEMA-001, SCHEMA-002). Covers scope.md
section 3, "schema definition to validate queries and data".
"""

import sys
import pathlib
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.analyzer import schema as schema_mod
from app.analyzer.engine import analyze_sql

EMPLOYEES_DDL = """
CREATE TABLE employees (
    employee_id NUMBER(10) NOT NULL,
    first_name VARCHAR2(50),
    last_name VARCHAR2(50) NOT NULL,
    department_id NUMBER(10),
    salary NUMBER(10,2),
    hire_date DATE,
    CONSTRAINT emp_pk PRIMARY KEY (employee_id)
);
"""


def rule_ids(result_list):
    ids = set()
    for r in result_list:
        for f in r.findings:
            ids.add(f.rule_id)
    return ids


def with_schema(ddl, fn):
    """Run fn() with a temporary, in-memory-only (non-persisted) schema
    loaded, then always clear it afterward so tests never leak state or
    touch the real config/schema.json.
    """
    schema_mod.import_ddl(ddl, persist=False)
    try:
        return fn()
    finally:
        schema_mod.clear_schema(persist=False)


# --------------------------------------------------------------------------
# DDL parsing
# --------------------------------------------------------------------------

def test_parse_ddl_extracts_table_columns_and_pk():
    tables, errors = schema_mod.parse_ddl(EMPLOYEES_DDL)
    assert errors == []
    assert len(tables) == 1
    t = tables[0]
    assert t.name == "employees"
    assert t.primary_key == ["EMPLOYEE_ID"]
    assert t.has_column("salary")
    assert t.has_column("SALARY")
    assert not t.has_column("nonexistent")
    assert t.columns["LAST_NAME"].nullable is False
    assert t.columns["FIRST_NAME"].nullable is True


def test_parse_ddl_inline_primary_key():
    ddl = """
    CREATE TABLE departments (
        id NUMBER(10) PRIMARY KEY,
        name VARCHAR2(100) NOT NULL
    );
    """
    tables, errors = schema_mod.parse_ddl(ddl)
    assert errors == []
    t = tables[0]
    assert t.primary_key == ["ID"]
    assert t.columns["ID"].is_primary_key is True


def test_parse_ddl_table_level_primary_key_constraint():
    ddl = "CREATE TABLE t (a NUMBER, b NUMBER, PRIMARY KEY (a, b));"
    tables, errors = schema_mod.parse_ddl(ddl)
    assert errors == []
    assert tables[0].primary_key == ["A", "B"]


def test_parse_ddl_multiple_tables():
    ddl = EMPLOYEES_DDL + "\nCREATE TABLE departments (id NUMBER PRIMARY KEY, name VARCHAR2(100));"
    tables, errors = schema_mod.parse_ddl(ddl)
    assert errors == []
    names = {t.name for t in tables}
    assert names == {"employees", "departments"}


def test_parse_ddl_ignores_non_create_table_statements_without_error():
    ddl = EMPLOYEES_DDL + "\nCREATE INDEX idx1 ON employees(last_name);"
    tables, errors = schema_mod.parse_ddl(ddl)
    assert errors == []
    assert len(tables) == 1  # the index statement is skipped, not an error


def test_parse_ddl_invalid_sql_reported_as_error_not_exception():
    tables, errors = schema_mod.parse_ddl("CREATE TABLE !!! ((( garbage")
    assert tables == []
    assert len(errors) == 1


def test_parse_ddl_empty_input():
    tables, errors = schema_mod.parse_ddl("")
    assert tables == []
    assert errors == []


def test_parse_ddl_no_create_table_found():
    tables, errors = schema_mod.parse_ddl("SELECT 1 FROM dual;")
    assert tables == []
    assert len(errors) == 1


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

def test_load_schema_missing_file_returns_empty():
    with tempfile.TemporaryDirectory() as d:
        path = pathlib.Path(d) / "schema.json"
        loaded = schema_mod.load_schema(path)
        assert loaded == {}


def test_save_and_load_round_trip():
    with tempfile.TemporaryDirectory() as d:
        path = pathlib.Path(d) / "schema.json"
        tables, _ = schema_mod.parse_ddl(EMPLOYEES_DDL)
        by_name = {t.name.upper(): t for t in tables}
        schema_mod.save_schema(by_name, path)

        loaded = schema_mod.load_schema(path)
        assert "EMPLOYEES" in loaded
        assert loaded["EMPLOYEES"].primary_key == ["EMPLOYEE_ID"]
        assert loaded["EMPLOYEES"].has_column("salary")


def test_corrupt_schema_file_returns_empty_without_crashing():
    with tempfile.TemporaryDirectory() as d:
        path = pathlib.Path(d) / "schema.json"
        path.write_text("{ not valid json")
        loaded = schema_mod.load_schema(path)
        assert loaded == {}


# --------------------------------------------------------------------------
# In-memory catalog operations (import / remove / clear), non-persisted
# --------------------------------------------------------------------------

def test_import_ddl_merges_into_live_catalog():
    def check():
        assert schema_mod.get_table("employees") is not None
        assert schema_mod.get_table("EMPLOYEES") is not None
        assert schema_mod.get_table("nonexistent") is None
    with_schema(EMPLOYEES_DDL, check)


def test_import_ddl_replaces_existing_table_definition():
    schema_mod.import_ddl(EMPLOYEES_DDL, persist=False)
    try:
        assert schema_mod.get_table("employees").has_column("salary")
        # Re-import a narrower definition of the same table.
        schema_mod.import_ddl(
            "CREATE TABLE employees (employee_id NUMBER PRIMARY KEY);",
            persist=False,
        )
        t = schema_mod.get_table("employees")
        assert t.has_column("salary") is False
        assert t.primary_key == ["EMPLOYEE_ID"]
    finally:
        schema_mod.clear_schema(persist=False)


def test_remove_table():
    schema_mod.import_ddl(EMPLOYEES_DDL, persist=False)
    try:
        assert schema_mod.remove_table("employees", persist=False) is True
        assert schema_mod.get_table("employees") is None
        assert schema_mod.remove_table("employees", persist=False) is False
    finally:
        schema_mod.clear_schema(persist=False)


# --------------------------------------------------------------------------
# Engine integration: SCHEMA-001 (unknown column)
# --------------------------------------------------------------------------

def test_unknown_column_detected_in_update_set():
    def check():
        results = analyze_sql("UPDATE employees SET statuss = 'A' WHERE employee_id = 1")
        assert "SCHEMA-001" in rule_ids(results)
    with_schema(EMPLOYEES_DDL, check)


def test_unknown_column_detected_in_where():
    def check():
        results = analyze_sql("DELETE FROM employees WHERE emp_id = 1")
        assert "SCHEMA-001" in rule_ids(results)
    with_schema(EMPLOYEES_DDL, check)


def test_known_columns_not_flagged():
    def check():
        results = analyze_sql(
            "UPDATE employees SET salary = 50000 WHERE employee_id = 1"
        )
        assert "SCHEMA-001" not in rule_ids(results)
    with_schema(EMPLOYEES_DDL, check)


def test_unknown_table_produces_no_schema_findings():
    """Tables not present in the imported schema must be silently
    skipped -- partial schema coverage should never cause false positives."""
    def check():
        results = analyze_sql("UPDATE some_other_table SET x = 1 WHERE y = 2")
        ids = rule_ids(results)
        assert "SCHEMA-001" not in ids
        assert "SCHEMA-002" not in ids
    with_schema(EMPLOYEES_DDL, check)


def test_no_schema_loaded_produces_no_schema_findings():
    # No with_schema() wrapper -- default/empty schema state.
    schema_mod.clear_schema(persist=False)
    results = analyze_sql("UPDATE employees SET whatever_column = 1 WHERE x = 2")
    ids = rule_ids(results)
    assert "SCHEMA-001" not in ids
    assert "SCHEMA-002" not in ids


def test_unknown_column_respects_table_alias():
    def check():
        results = analyze_sql(
            "UPDATE employees e SET e.statuss = 'A' WHERE e.employee_id = 1"
        )
        assert "SCHEMA-001" in rule_ids(results)
    with_schema(EMPLOYEES_DDL, check)


# --------------------------------------------------------------------------
# Engine integration: SCHEMA-002 (WHERE doesn't reference primary key)
# --------------------------------------------------------------------------

def test_missing_pk_predicate_detected():
    def check():
        results = analyze_sql(
            "UPDATE employees SET salary = 50000 WHERE department_id = 20"
        )
        assert "SCHEMA-002" in rule_ids(results)
    with_schema(EMPLOYEES_DDL, check)


def test_pk_predicate_present_not_flagged():
    def check():
        results = analyze_sql(
            "UPDATE employees SET salary = 50000 WHERE employee_id = 1"
        )
        assert "SCHEMA-002" not in rule_ids(results)
    with_schema(EMPLOYEES_DDL, check)


def test_pk_predicate_with_alias_not_flagged():
    """Regression test: an aliased PK reference (e.employee_id) must be
    recognized as covering the table's primary key, not just the bare
    table-name-qualified or unqualified form."""
    def check():
        results = analyze_sql(
            "UPDATE employees e SET e.salary = 50000 WHERE e.employee_id = 1"
        )
        assert "SCHEMA-002" not in rule_ids(results)
    with_schema(EMPLOYEES_DDL, check)


def test_table_without_pk_never_flags_schema_002():
    ddl = "CREATE TABLE logs (message VARCHAR2(200), created_at DATE);"
    def check():
        results = analyze_sql(
            "UPDATE logs SET message = 'x' WHERE created_at = DATE '2024-01-01'"
        )
        assert "SCHEMA-002" not in rule_ids(results)
    with_schema(ddl, check)


def test_missing_where_entirely_does_not_double_fire_schema_002():
    """DML-001 (missing WHERE) already covers this case with a CRITICAL
    finding; SCHEMA-002 requires a WHERE clause to evaluate, so it should
    simply not apply rather than duplicate the warning."""
    def check():
        results = analyze_sql("UPDATE employees SET salary = 50000")
        ids = rule_ids(results)
        assert "DML-001" in ids
        assert "SCHEMA-002" not in ids
    with_schema(EMPLOYEES_DDL, check)


if __name__ == "__main__":
    import inspect
    mod = sys.modules[__name__]
    fns = [f for name, f in inspect.getmembers(mod) if name.startswith("test_")]
    passed, failed = 0, 0
    for fn in fns:
        try:
            fn()
            passed += 1
        except AssertionError as e:
            failed += 1
            print(f"FAILED: {fn.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"ERROR:  {fn.__name__}: {e}")
    print(f"\n{passed} passed, {failed} failed out of {len(fns)} tests")
    sys.exit(1 if failed else 0)

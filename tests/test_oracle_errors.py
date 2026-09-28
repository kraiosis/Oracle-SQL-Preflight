"""
Tests for the Oracle error catalog checks (scope.md section 6): ORA-001
(division by zero, no schema needed) and ORA-002/003/004 (invalid
number, NULL into NOT NULL, value too large -- all schema-dependent,
built on top of the offline schema module in schema.py).
"""

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.analyzer import schema as schema_mod
from app.analyzer.engine import analyze_sql

EMPLOYEES_DDL = """
CREATE TABLE employees (
    employee_id NUMBER(10) NOT NULL,
    first_name VARCHAR2(10),
    last_name VARCHAR2(50) NOT NULL,
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
    schema_mod.import_ddl(ddl, persist=False)
    try:
        return fn()
    finally:
        schema_mod.clear_schema(persist=False)


# --------------------------------------------------------------------------
# ORA-001 -- division by zero (no schema required)
# --------------------------------------------------------------------------

def test_division_by_zero_literal_detected_in_update_set():
    results = analyze_sql("UPDATE employees SET salary = salary / 0 WHERE employee_id = 1")
    assert "ORA-001" in rule_ids(results)


def test_division_by_zero_detected_in_select():
    results = analyze_sql("SELECT salary / 0 FROM employees")
    assert "ORA-001" in rule_ids(results)


def test_division_by_nonzero_literal_not_flagged():
    results = analyze_sql("UPDATE employees SET salary = salary / 2 WHERE employee_id = 1")
    assert "ORA-001" not in rule_ids(results)


def test_division_by_column_not_flagged():
    """A column divisor could be non-zero at runtime -- must not guess."""
    results = analyze_sql(
        "UPDATE employees SET salary = salary / employee_id WHERE employee_id = 1"
    )
    assert "ORA-001" not in rule_ids(results)


def test_division_by_zero_detected_in_delete_where():
    results = analyze_sql("DELETE FROM employees WHERE salary / 0 > 1")
    assert "ORA-001" in rule_ids(results)


def test_division_by_zero_needs_no_schema():
    """This check must fire even with no offline schema imported."""
    schema_mod.clear_schema(persist=False)
    results = analyze_sql("UPDATE unknown_table SET x = y / 0 WHERE z = 1")
    assert "ORA-001" in rule_ids(results)


# --------------------------------------------------------------------------
# ORA-002 -- invalid number literal for a NUMBER column
# --------------------------------------------------------------------------

def test_invalid_number_in_update_set_detected():
    def check():
        results = analyze_sql(
            "UPDATE employees SET salary = 'abc' WHERE employee_id = 1"
        )
        assert "ORA-002" in rule_ids(results)
    with_schema(EMPLOYEES_DDL, check)


def test_valid_numeric_string_not_flagged():
    """Oracle accepts a numeric-looking string literal for a NUMBER
    column via implicit conversion, so this must not be flagged."""
    def check():
        results = analyze_sql(
            "UPDATE employees SET salary = '5000' WHERE employee_id = 1"
        )
        assert "ORA-002" not in rule_ids(results)
    with_schema(EMPLOYEES_DDL, check)


def test_invalid_number_in_where_clause_detected():
    def check():
        results = analyze_sql(
            "UPDATE employees SET first_name = 'X' WHERE salary = 'notanumber'"
        )
        assert "ORA-002" in rule_ids(results)
    with_schema(EMPLOYEES_DDL, check)


def test_numeric_literal_on_string_column_not_flagged():
    """Only the NUMBER-column-gets-non-numeric-string case is modeled;
    the reverse (number into a string column) is always valid in Oracle."""
    def check():
        results = analyze_sql(
            "UPDATE employees SET first_name = 123 WHERE employee_id = 1"
        )
        assert "ORA-002" not in rule_ids(results)
    with_schema(EMPLOYEES_DDL, check)


def test_no_schema_loaded_suppresses_ora_002():
    schema_mod.clear_schema(persist=False)
    results = analyze_sql("UPDATE employees SET salary = 'abc' WHERE employee_id = 1")
    assert "ORA-002" not in rule_ids(results)


# --------------------------------------------------------------------------
# ORA-003 -- NULL assigned to a NOT NULL column
# --------------------------------------------------------------------------

def test_null_into_not_null_column_detected():
    def check():
        results = analyze_sql(
            "UPDATE employees SET employee_id = NULL WHERE employee_id = 1"
        )
        assert "ORA-003" in rule_ids(results)
    with_schema(EMPLOYEES_DDL, check)


def test_null_into_nullable_column_not_flagged():
    def check():
        results = analyze_sql(
            "UPDATE employees SET salary = NULL WHERE employee_id = 1"
        )
        assert "ORA-003" not in rule_ids(results)
    with_schema(EMPLOYEES_DDL, check)


def test_null_omitted_column_not_flagged():
    """Only explicit NULL literals are checked -- omitted columns are not
    flagged, since a DEFAULT clause (not modeled here) could apply."""
    def check():
        results = analyze_sql(
            "INSERT INTO employees (employee_id) VALUES (1)"
        )
        assert "ORA-003" not in rule_ids(results)
    with_schema(EMPLOYEES_DDL, check)


# --------------------------------------------------------------------------
# ORA-004 -- string literal longer than declared column size
# --------------------------------------------------------------------------

def test_string_too_long_detected():
    def check():
        results = analyze_sql(
            "UPDATE employees SET first_name = 'ThisNameIsWayTooLong' WHERE employee_id = 1"
        )
        assert "ORA-004" in rule_ids(results)
    with_schema(EMPLOYEES_DDL, check)


def test_string_within_length_not_flagged():
    def check():
        results = analyze_sql(
            "UPDATE employees SET first_name = 'Bob' WHERE employee_id = 1"
        )
        assert "ORA-004" not in rule_ids(results)
    with_schema(EMPLOYEES_DDL, check)


def test_string_exactly_at_length_not_flagged():
    def check():
        # first_name is VARCHAR2(10); exactly 10 chars must be allowed.
        results = analyze_sql(
            "UPDATE employees SET first_name = '1234567890' WHERE employee_id = 1"
        )
        assert "ORA-004" not in rule_ids(results)
    with_schema(EMPLOYEES_DDL, check)


# --------------------------------------------------------------------------
# INSERT statement coverage
# --------------------------------------------------------------------------

def test_insert_invalid_number_detected():
    def check():
        results = analyze_sql(
            "INSERT INTO employees (employee_id, salary) VALUES (1, 'abc')"
        )
        assert "ORA-002" in rule_ids(results)
    with_schema(EMPLOYEES_DDL, check)


def test_insert_null_into_not_null_detected():
    def check():
        results = analyze_sql(
            "INSERT INTO employees (employee_id, salary) VALUES (NULL, 5000)"
        )
        assert "ORA-003" in rule_ids(results)
    with_schema(EMPLOYEES_DDL, check)


def test_insert_string_too_long_detected():
    def check():
        results = analyze_sql(
            "INSERT INTO employees (employee_id, first_name) VALUES (1, 'ThisNameIsWayTooLong')"
        )
        assert "ORA-004" in rule_ids(results)
    with_schema(EMPLOYEES_DDL, check)


def test_insert_unknown_column_detected():
    def check():
        results = analyze_sql(
            "INSERT INTO employees (employee_id, statuss) VALUES (1, 'A')"
        )
        assert "SCHEMA-001" in rule_ids(results)
    with_schema(EMPLOYEES_DDL, check)


def test_insert_clean_values_not_flagged():
    def check():
        results = analyze_sql(
            "INSERT INTO employees (employee_id, salary, first_name) "
            "VALUES (1, 5000, 'Bob')"
        )
        ids = rule_ids(results)
        assert "ORA-002" not in ids
        assert "ORA-003" not in ids
        assert "ORA-004" not in ids
        assert "SCHEMA-001" not in ids
    with_schema(EMPLOYEES_DDL, check)


def test_insert_positional_no_column_list_matching_count():
    """No explicit column list: fall back to the schema's own column
    order, but only when the VALUES count matches exactly."""
    ddl = "CREATE TABLE t (a NUMBER, b VARCHAR2(5));"
    def check():
        results = analyze_sql("INSERT INTO t VALUES (1, 'toolongvalue')")
        assert "ORA-004" in rule_ids(results)
    with_schema(ddl, check)


def test_insert_positional_mismatched_count_skips_silently():
    """Column count doesn't match the schema -- ambiguous, must not guess."""
    ddl = "CREATE TABLE t (a NUMBER, b VARCHAR2(5), c DATE);"
    def check():
        results = analyze_sql("INSERT INTO t VALUES (1, 'toolongvalue')")
        ids = rule_ids(results)
        assert "ORA-004" not in ids
        assert "SCHEMA-001" not in ids
    with_schema(ddl, check)


def test_insert_multi_row_values_each_checked():
    def check():
        results = analyze_sql(
            "INSERT INTO employees (employee_id, salary) VALUES (1, 5000), (2, 'notanumber')"
        )
        assert "ORA-002" in rule_ids(results)
    with_schema(EMPLOYEES_DDL, check)


def test_insert_into_unrelated_table_silent():
    def check():
        results = analyze_sql("INSERT INTO other_table (x) VALUES ('abc')")
        ids = rule_ids(results)
        assert "ORA-002" not in ids
        assert "SCHEMA-001" not in ids
    with_schema(EMPLOYEES_DDL, check)


def test_insert_select_does_not_crash_or_false_positive():
    """INSERT ... SELECT has no VALUES clause -- must be skipped cleanly,
    not raise or guess at anything."""
    def check():
        results = analyze_sql(
            "INSERT INTO employees (employee_id) SELECT employee_id FROM employees"
        )
        assert results[0].parse_ok is True
        ids = rule_ids(results)
        assert "ORA-002" not in ids
        assert "ORA-003" not in ids
        assert "ORA-004" not in ids
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

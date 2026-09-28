"""
Test suite for the Oracle SQL Preflight analyzer engine.

Per scope.md section 26 ("Testing strategy"), each rule is tested with a
positive example (should fire) and a negative example (should not fire),
plus boundary/false-positive checks where relevant.
"""

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.analyzer.engine import analyze_sql, format_sql, overall_status
from app.analyzer import schema as schema_mod

# This suite tests the base rule engine in isolation from any offline
# schema the user may have imported for real (config/schema.json) --
# without this, a user who has imported a schema with tables named
# "employees" etc. would see unrelated SCHEMA-001/SCHEMA-002 findings
# leak into these tests. Schema-specific behavior has its own suite:
# tests/test_schema.py.
schema_mod.clear_schema(persist=False)


def rule_ids(result_list):
    ids = set()
    for r in result_list:
        for f in r.findings:
            ids.add(f.rule_id)
    return ids


# --------------------------------------------------------------------------
# PARSE-001
# --------------------------------------------------------------------------

def test_parse_error_detected():
    results = analyze_sql("UPDATTE employees SET x = 1 !!!")
    assert "PARSE-001" in rule_ids(results)
    assert results[0].parse_ok is False


def test_valid_sql_parses_cleanly():
    results = analyze_sql("SELECT id FROM employees")
    assert results[0].parse_ok is True
    assert "PARSE-001" not in rule_ids(results)


# --------------------------------------------------------------------------
# DML-001 — UPDATE missing WHERE
# --------------------------------------------------------------------------

def test_update_missing_where_detected():
    results = analyze_sql("UPDATE employees SET salary = salary * 1.10")
    assert "DML-001" in rule_ids(results)
    findings = [f for f in results[0].findings if f.rule_id == "DML-001"]
    assert findings[0].severity == "CRITICAL"


def test_update_with_where_not_flagged():
    results = analyze_sql("UPDATE employees SET status = 'X' WHERE employee_id = 1")
    assert "DML-001" not in rule_ids(results)


# --------------------------------------------------------------------------
# DML-002 — DELETE missing WHERE
# --------------------------------------------------------------------------

def test_delete_missing_where_detected():
    results = analyze_sql("DELETE FROM employees")
    assert "DML-002" in rule_ids(results)
    findings = [f for f in results[0].findings if f.rule_id == "DML-002"]
    assert findings[0].severity == "CRITICAL"


def test_delete_with_where_not_flagged():
    results = analyze_sql("DELETE FROM employees WHERE employee_id = 1")
    assert "DML-002" not in rule_ids(results)


# --------------------------------------------------------------------------
# DML-SET-WHERE-001 — SET/WHERE no-op
# --------------------------------------------------------------------------

def test_set_where_noop_detected():
    results = analyze_sql(
        "UPDATE employees SET status = 'ACTIVE' WHERE status = 'ACTIVE'"
    )
    assert "DML-SET-WHERE-001" in rule_ids(results)


def test_set_where_different_values_not_flagged():
    """README section 9: different literal values should NOT trigger the no-op rule."""
    results = analyze_sql(
        "UPDATE employees SET status = 'INACTIVE' WHERE status = 'ACTIVE'"
    )
    assert "DML-SET-WHERE-001" not in rule_ids(results)


def test_set_where_noop_multi_column():
    results = analyze_sql(
        "UPDATE employees SET status = 'ACTIVE', department_id = 20 "
        "WHERE status = 'ACTIVE' AND department_id = 20"
    )
    assert "DML-SET-WHERE-001" in rule_ids(results)


# --------------------------------------------------------------------------
# DML-WHERE-002 — contradictory equality
# --------------------------------------------------------------------------

def test_contradictory_equality_detected():
    results = analyze_sql(
        "UPDATE employees SET status = 'ACTIVE' "
        "WHERE department_id = 10 AND department_id = 20"
    )
    assert "DML-WHERE-002" in rule_ids(results)


def test_same_equality_twice_not_contradictory():
    """Same column equal to the SAME literal twice is redundant, not a contradiction."""
    results = analyze_sql(
        "UPDATE employees SET status = 'ACTIVE' "
        "WHERE department_id = 10 AND department_id = 10"
    )
    assert "DML-WHERE-002" not in rule_ids(results)


def test_different_columns_not_contradictory():
    results = analyze_sql(
        "UPDATE employees SET status = 'ACTIVE' "
        "WHERE department_id = 10 AND manager_id = 20"
    )
    assert "DML-WHERE-002" not in rule_ids(results)


# --------------------------------------------------------------------------
# DML-WHERE-003 — constant-false predicate
# --------------------------------------------------------------------------

def test_constant_false_numeric_detected():
    results = analyze_sql("UPDATE employees SET status = 'ACTIVE' WHERE 1 = 2")
    assert "DML-WHERE-003" in rule_ids(results)


def test_constant_false_string_detected():
    results = analyze_sql(
        "UPDATE employees SET x = 1 WHERE status = 'A' AND status = 'B'"
    )
    # Two different literals for the same column: contradiction rule fires.
    assert "DML-WHERE-002" in rule_ids(results)


def test_normal_predicate_not_constant_false():
    results = analyze_sql("UPDATE employees SET status = 'A' WHERE employee_id = 5")
    assert "DML-WHERE-003" not in rule_ids(results)


# --------------------------------------------------------------------------
# DML-WHERE-004 — always-true predicate
# --------------------------------------------------------------------------

def test_always_true_detected():
    results = analyze_sql("UPDATE employees SET status = 'A' WHERE 1 = 1")
    assert "DML-WHERE-004" in rule_ids(results)


# --------------------------------------------------------------------------
# DML-WHERE-005 — NULL equality
# --------------------------------------------------------------------------

def test_null_equality_detected():
    results = analyze_sql("UPDATE employees SET status = 'A' WHERE manager_id = NULL")
    assert "DML-WHERE-005" in rule_ids(results)


def test_is_null_not_flagged():
    results = analyze_sql("UPDATE employees SET status = 'A' WHERE manager_id IS NULL")
    assert "DML-WHERE-005" not in rule_ids(results)


# --------------------------------------------------------------------------
# DML-WHERE-006 — column compared to itself
# --------------------------------------------------------------------------

def test_self_comparison_detected():
    results = analyze_sql(
        "UPDATE employees SET status = 'A' WHERE manager_id = manager_id"
    )
    assert "DML-WHERE-006" in rule_ids(results)


# --------------------------------------------------------------------------
# PERF-001 — SELECT *
# --------------------------------------------------------------------------

def test_select_star_detected():
    results = analyze_sql("SELECT * FROM employees")
    assert "PERF-001" in rule_ids(results)


def test_explicit_columns_not_flagged():
    results = analyze_sql("SELECT employee_id, status FROM employees")
    assert "PERF-001" not in rule_ids(results)


# --------------------------------------------------------------------------
# DML-003 / DML-004 — INSERT / MERGE recognition
# --------------------------------------------------------------------------

def test_insert_recognized():
    results = analyze_sql(
        "INSERT INTO employees (id, name) VALUES (1, 'X')"
    )
    assert "DML-003" in rule_ids(results)
    assert results[0].statement_type == "INSERT"


def test_merge_recognized():
    results = analyze_sql(
        "MERGE INTO employees e USING src s ON (e.id = s.id) "
        "WHEN MATCHED THEN UPDATE SET e.status = s.status "
        "WHEN NOT MATCHED THEN INSERT (id) VALUES (s.id)"
    )
    assert "DML-004" in rule_ids(results)
    assert results[0].statement_type == "MERGE"


# --------------------------------------------------------------------------
# Multi-statement handling
# --------------------------------------------------------------------------

def test_multiple_statements_each_analyzed():
    sql = "DELETE FROM a; UPDATE b SET x = 1 WHERE x = 1;"
    results = analyze_sql(sql)
    assert len(results) == 2
    ids = rule_ids(results)
    assert "DML-002" in ids
    assert "DML-SET-WHERE-001" in ids


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------

def test_format_valid_sql():
    result = format_sql("select id,status from employees where id=1")
    assert result["ok"] is True
    assert "SELECT" in result["formatted"]


def test_format_invalid_sql():
    result = format_sql("SELEKT * FRM employees !!!")
    assert result["ok"] is False


# --------------------------------------------------------------------------
# Overall status roll-up
# --------------------------------------------------------------------------

def test_overall_status_critical_wins():
    results = analyze_sql("UPDATE employees SET status = 'A'")
    assert overall_status(results) == "CRITICAL"


def test_overall_status_ok_when_no_findings():
    results = analyze_sql("UPDATE employees SET status = 'A' WHERE employee_id = 1")
    assert overall_status(results) == "OK"


def test_empty_sql_returns_no_statements():
    results = analyze_sql("   ")
    assert results == []


# --------------------------------------------------------------------------
# DML-SET-SELF-001 — unconditional self-assignment
# --------------------------------------------------------------------------

def test_self_assignment_detected():
    results = analyze_sql("UPDATE employees SET salary = salary WHERE employee_id = 1")
    assert "DML-SET-SELF-001" in rule_ids(results)


def test_self_assignment_detected_even_without_where():
    """Self-assignment is a no-op regardless of WHERE, so it must still
    fire even when DML-001 (missing WHERE) also fires."""
    results = analyze_sql("UPDATE employees SET salary = salary")
    ids = rule_ids(results)
    assert "DML-SET-SELF-001" in ids
    assert "DML-001" in ids


def test_different_column_assignment_not_self_assignment():
    results = analyze_sql(
        "UPDATE employees SET salary = base_salary WHERE employee_id = 1"
    )
    assert "DML-SET-SELF-001" not in rule_ids(results)


# --------------------------------------------------------------------------
# DML-SET-FUNC-001 — function-wrapped self reference
# --------------------------------------------------------------------------

def test_nvl_self_reference_detected():
    results = analyze_sql(
        "UPDATE employees SET salary = NVL(salary, 0) WHERE employee_id = 1"
    )
    assert "DML-SET-FUNC-001" in rule_ids(results)
    f = [f for r in results for f in r.findings if f.rule_id == "DML-SET-FUNC-001"][0]
    assert f.confidence == "POSSIBLE"


def test_upper_self_reference_detected():
    results = analyze_sql(
        "UPDATE employees SET status = UPPER(status) WHERE employee_id = 1"
    )
    assert "DML-SET-FUNC-001" in rule_ids(results)


def test_trunc_self_reference_detected():
    results = analyze_sql(
        "UPDATE employees SET hire_date = TRUNC(hire_date) WHERE employee_id = 1"
    )
    assert "DML-SET-FUNC-001" in rule_ids(results)


def test_function_on_different_column_not_flagged():
    results = analyze_sql(
        "UPDATE employees SET status = UPPER(raw_status) WHERE employee_id = 1"
    )
    assert "DML-SET-FUNC-001" not in rule_ids(results)


def test_function_result_not_confused_with_self_assignment():
    results = analyze_sql(
        "UPDATE employees SET status = UPPER(status) WHERE employee_id = 1"
    )
    assert "DML-SET-SELF-001" not in rule_ids(results)


# --------------------------------------------------------------------------
# DML-WHERE-007 — contradictory numeric range
# --------------------------------------------------------------------------

def test_range_contradiction_detected():
    results = analyze_sql(
        "UPDATE employees SET status = 'A' WHERE amount > 100 AND amount < 50"
    )
    assert "DML-WHERE-007" in rule_ids(results)


def test_range_contradiction_strict_touching_bounds_detected():
    """amount > 100 AND amount < 100: no value satisfies both."""
    results = analyze_sql(
        "UPDATE employees SET status = 'A' WHERE amount > 100 AND amount < 100"
    )
    assert "DML-WHERE-007" in rule_ids(results)


def test_range_inclusive_touching_bounds_not_contradictory():
    """amount >= 100 AND amount <= 100: exactly 100 satisfies both."""
    results = analyze_sql(
        "UPDATE employees SET status = 'A' WHERE amount >= 100 AND amount <= 100"
    )
    assert "DML-WHERE-007" not in rule_ids(results)


def test_normal_range_not_contradictory():
    results = analyze_sql(
        "UPDATE employees SET status = 'A' WHERE amount > 10 AND amount < 100"
    )
    assert "DML-WHERE-007" not in rule_ids(results)


def test_range_contradiction_detected_with_literal_on_left():
    """100 < amount AND amount < 50 should normalize the same as
    amount > 100 AND amount < 50."""
    results = analyze_sql(
        "UPDATE employees SET status = 'A' WHERE 100 < amount AND amount < 50"
    )
    assert "DML-WHERE-007" in rule_ids(results)


def test_single_bound_not_contradictory():
    results = analyze_sql(
        "UPDATE employees SET status = 'A' WHERE amount > 100"
    )
    assert "DML-WHERE-007" not in rule_ids(results)


# --------------------------------------------------------------------------
# JOIN-001 — possible Cartesian join
# --------------------------------------------------------------------------

def test_comma_join_no_predicate_detected():
    results = analyze_sql("SELECT * FROM employees e, departments d")
    assert "JOIN-001" in rule_ids(results)


def test_comma_join_with_predicate_not_flagged():
    results = analyze_sql(
        "SELECT * FROM employees e, departments d WHERE e.department_id = d.id"
    )
    assert "JOIN-001" not in rule_ids(results)


def test_comma_join_unrelated_where_still_flagged():
    """A WHERE clause that filters but never connects the two tables
    should still be flagged -- filtering is not joining."""
    results = analyze_sql(
        "SELECT * FROM employees e, departments d WHERE e.status = 'A'"
    )
    assert "JOIN-001" in rule_ids(results)


def test_explicit_join_with_on_not_flagged():
    results = analyze_sql(
        "SELECT * FROM employees e JOIN departments d ON e.department_id = d.id"
    )
    assert "JOIN-001" not in rule_ids(results)


def test_explicit_join_missing_on_detected():
    results = analyze_sql("SELECT * FROM employees e JOIN departments d")
    assert "JOIN-001" in rule_ids(results)


def test_explicit_cross_join_not_flagged():
    """CROSS JOIN is declared intent and must never be flagged."""
    results = analyze_sql("SELECT * FROM employees e CROSS JOIN departments d")
    assert "JOIN-001" not in rule_ids(results)


def test_three_tables_one_unconnected_detected():
    results = analyze_sql(
        "SELECT * FROM employees e, departments d, locations l "
        "WHERE e.department_id = d.id"
    )
    ids = rule_ids(results)
    assert "JOIN-001" in ids


def test_three_tables_all_connected_not_flagged():
    results = analyze_sql(
        "SELECT * FROM employees e, departments d, locations l "
        "WHERE e.department_id = d.id AND d.location_id = l.id"
    )
    assert "JOIN-001" not in rule_ids(results)


def test_single_table_not_flagged():
    results = analyze_sql("SELECT * FROM employees")
    assert "JOIN-001" not in rule_ids(results)


def test_subquery_in_from_skips_join_check_without_false_positive():
    """Subqueries are out of scope for this structural check -- it should
    skip silently rather than raise or guess."""
    results = analyze_sql(
        "SELECT * FROM (SELECT * FROM employees) e, departments d"
    )
    assert "JOIN-001" not in rule_ids(results)


def test_duplicate_alias_skips_join_check():
    results = analyze_sql(
        "SELECT * FROM employees e, employees e WHERE e.id = e.id"
    )
    assert "JOIN-001" not in rule_ids(results)


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

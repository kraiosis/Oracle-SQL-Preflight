"""
Tests for correlated-subquery labeling and bind-variable analysis
(scope.md sections 3 and 12: "subquery analysis", "correlated
subqueries", "bind-variable analysis"):

  SUBQ-001  Correlated subquery detected
  BIND-001  Bind variable compared against columns of different types
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
# SUBQ-001 -- correlated subquery
# --------------------------------------------------------------------------

def test_correlated_exists_detected():
    results = analyze_sql(
        "SELECT id FROM employees e WHERE EXISTS "
        "(SELECT 1 FROM depts d WHERE d.id = e.department_id)"
    )
    assert "SUBQ-001" in rule_ids(results)


def test_correlated_not_exists_detected():
    results = analyze_sql(
        "SELECT id FROM employees e WHERE NOT EXISTS "
        "(SELECT 1 FROM depts d WHERE d.mgr_id = e.employee_id)"
    )
    assert "SUBQ-001" in rule_ids(results)


def test_correlated_in_detected():
    results = analyze_sql(
        "SELECT id FROM employees e WHERE e.department_id IN "
        "(SELECT id FROM depts d WHERE d.mgr = e.manager_id)"
    )
    assert "SUBQ-001" in rule_ids(results)


def test_correlated_scalar_subquery_in_select_list_detected():
    results = analyze_sql(
        "SELECT (SELECT max(sal) FROM other o WHERE o.dept = e.department_id) "
        "FROM employees e"
    )
    assert "SUBQ-001" in rule_ids(results)


def test_uncorrelated_subquery_not_flagged():
    results = analyze_sql(
        "SELECT id FROM employees WHERE department_id IN (SELECT id FROM depts)"
    )
    assert "SUBQ-001" not in rule_ids(results)


def test_subquery_in_from_clause_not_flagged():
    """A FROM-clause derived table can't correlate back into the same
    FROM clause in Oracle -- this must never be checked/flagged here."""
    results = analyze_sql(
        "SELECT * FROM employees e, "
        "(SELECT id FROM depts d WHERE d.id = e.department_id) sub"
    )
    assert "SUBQ-001" not in rule_ids(results)


def test_two_level_correlation_to_grandparent_not_flagged():
    """Correlating past the immediate parent, straight to a grandparent
    scope, is legal Oracle SQL that this bounded (one-level) check is not
    expected to catch -- a false negative, never a false positive."""
    results = analyze_sql(
        "SELECT id FROM employees e WHERE EXISTS "
        "(SELECT 1 FROM depts d WHERE EXISTS "
        "(SELECT 1 FROM t WHERE t.x = e.department_id))"
    )
    assert "SUBQ-001" not in rule_ids(results)


def test_correlated_subquery_in_update_where_detected():
    results = analyze_sql(
        "UPDATE employees e SET e.salary = 0 WHERE EXISTS "
        "(SELECT 1 FROM depts d WHERE d.id = e.employee_id)"
    )
    assert "SUBQ-001" in rule_ids(results)


def test_no_outer_table_reference_not_flagged():
    """A subquery that references only its own tables, even if a
    same-named column exists elsewhere, is not correlated."""
    results = analyze_sql(
        "SELECT id FROM employees e WHERE EXISTS "
        "(SELECT 1 FROM depts d WHERE d.mgr_id = d.employee_id)"
    )
    assert "SUBQ-001" not in rule_ids(results)


# --------------------------------------------------------------------------
# BIND-001 -- bind variable compared against columns of different types
# --------------------------------------------------------------------------

def test_bind_type_mismatch_detected():
    def check():
        results = analyze_sql(
            "SELECT * FROM employees e WHERE e.salary = :val AND e.first_name = :val"
        )
        assert "BIND-001" in rule_ids(results)
    with_schema(EMPLOYEES_DDL, check)


def test_bind_same_type_category_not_flagged():
    def check():
        results = analyze_sql(
            "SELECT * FROM employees e WHERE e.employee_id = :id AND e.salary = :id"
        )
        assert "BIND-001" not in rule_ids(results)
    with_schema(EMPLOYEES_DDL, check)


def test_bind_used_once_not_flagged():
    def check():
        results = analyze_sql(
            "SELECT * FROM employees e WHERE e.salary = :val"
        )
        assert "BIND-001" not in rule_ids(results)
    with_schema(EMPLOYEES_DDL, check)


def test_bind_unqualified_column_skipped():
    """No table alias means the column can't be safely resolved -- must
    not guess, even though it looks like the same mismatch pattern."""
    def check():
        results = analyze_sql(
            "SELECT * FROM employees WHERE salary = :val AND first_name = :val"
        )
        assert "BIND-001" not in rule_ids(results)
    with_schema(EMPLOYEES_DDL, check)


def test_bind_bare_table_name_qualifier_resolved():
    """A column qualified with the real table name (no alias) must still
    resolve correctly."""
    def check():
        results = analyze_sql(
            "SELECT * FROM employees WHERE employees.salary = :val "
            "AND employees.first_name = :val"
        )
        assert "BIND-001" in rule_ids(results)
    with_schema(EMPLOYEES_DDL, check)


def test_bind_mismatch_in_update_where_detected():
    def check():
        results = analyze_sql(
            "UPDATE employees e SET e.salary = 0 "
            "WHERE e.first_name = :val AND e.employee_id = :val"
        )
        assert "BIND-001" in rule_ids(results)
    with_schema(EMPLOYEES_DDL, check)


def test_bind_no_schema_loaded_not_flagged():
    schema_mod.clear_schema(persist=False)
    results = analyze_sql(
        "SELECT * FROM employees e WHERE e.salary = :val AND e.first_name = :val"
    )
    assert "BIND-001" not in rule_ids(results)


def test_bind_unknown_column_skipped():
    def check():
        results = analyze_sql(
            "UPDATE employees e SET e.salary = 0 "
            "WHERE e.nonexistent_col = :val AND e.first_name = :val"
        )
        # SCHEMA-001 covers the unknown column (DML target-table check);
        # BIND-001 must not guess about a type category it has no data for.
        ids = rule_ids(results)
        assert "SCHEMA-001" in ids
        assert "BIND-001" not in ids
    with_schema(EMPLOYEES_DDL, check)


def test_ambiguous_alias_across_scopes_skipped():
    """The same alias 'x' refers to two different real tables in two
    different query blocks -- must not guess which one a bind's column
    belongs to, so this must not fire even though the pattern looks
    superficially like a type mismatch."""
    ddl = """
    CREATE TABLE t1 (id NUMBER, name VARCHAR2(20));
    CREATE TABLE t2 (id NUMBER, amount NUMBER);
    """
    def check():
        results = analyze_sql(
            "SELECT * FROM t1 x WHERE x.name = :val "
            "UNION "
            "SELECT * FROM t2 x WHERE x.amount = :val"
        )
        assert "BIND-001" not in rule_ids(results)
    with_schema(ddl, check)


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

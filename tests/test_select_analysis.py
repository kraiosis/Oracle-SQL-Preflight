"""
Tests for the SELECT/subquery/CTE/set-operation analysis expansion
(scope.md section 3, "Planned expansion": SELECT analysis, subquery
analysis, CTE analysis, UNION/INTERSECT/MINUS analysis):

  CTE-001  Unused CTE (WITH-clause subquery never referenced)
  ORA-005  Mismatched column counts across UNION/INTERSECT/MINUS branches
  ORA-006  Scalar subquery may return more than one row (ORA-01427)
"""

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.analyzer.engine import analyze_sql


def rule_ids(result_list):
    ids = set()
    for r in result_list:
        for f in r.findings:
            ids.add(f.rule_id)
    return ids


def statement_types(result_list):
    return [r.statement_type for r in result_list]


# --------------------------------------------------------------------------
# CTE-001 -- unused CTE
# --------------------------------------------------------------------------

def test_unused_cte_detected():
    results = analyze_sql("WITH cte1 AS (SELECT id FROM t1) SELECT * FROM t2")
    assert "CTE-001" in rule_ids(results)


def test_used_cte_not_flagged():
    results = analyze_sql("WITH cte1 AS (SELECT id FROM t1) SELECT * FROM cte1")
    assert "CTE-001" not in rule_ids(results)


def test_cte_used_by_later_cte_not_flagged():
    """A CTE referenced only by a later CTE (not the main query) is still
    used -- this must not be flagged."""
    results = analyze_sql(
        "WITH a AS (SELECT id FROM t1), b AS (SELECT id FROM a) SELECT * FROM b"
    )
    assert "CTE-001" not in rule_ids(results)


def test_one_used_one_unused_cte_only_unused_flagged():
    results = analyze_sql(
        "WITH used_cte AS (SELECT id FROM t1), unused_cte AS (SELECT id FROM t2) "
        "SELECT * FROM used_cte"
    )
    findings = [f for r in results for f in r.findings if f.rule_id == "CTE-001"]
    assert len(findings) == 1
    assert "UNUSED_CTE" in findings[0].evidence.upper()


def test_no_with_clause_not_flagged():
    results = analyze_sql("SELECT * FROM employees")
    assert "CTE-001" not in rule_ids(results)


def test_unused_cte_in_union_top_level_with_clause():
    """The WITH clause on a set-operation query attaches to the top-level
    node, not either branch -- must still be checked."""
    results = analyze_sql(
        "WITH cte1 AS (SELECT id FROM t1) "
        "SELECT id FROM t2 UNION SELECT id FROM t3"
    )
    assert "CTE-001" in rule_ids(results)


def test_cte_used_in_union_branch_not_flagged():
    results = analyze_sql(
        "WITH cte1 AS (SELECT id FROM t1) "
        "SELECT id FROM cte1 UNION SELECT id FROM t3"
    )
    assert "CTE-001" not in rule_ids(results)


# --------------------------------------------------------------------------
# ORA-005 -- mismatched column counts across set-operation branches
# --------------------------------------------------------------------------

def test_union_mismatched_columns_detected():
    results = analyze_sql("SELECT a, b FROM t1 UNION SELECT x FROM t2")
    assert "ORA-005" in rule_ids(results)


def test_union_matched_columns_not_flagged():
    results = analyze_sql("SELECT a, b FROM t1 UNION SELECT x, y FROM t2")
    assert "ORA-005" not in rule_ids(results)


def test_union_all_mismatched_columns_detected():
    results = analyze_sql("SELECT a FROM t1 UNION ALL SELECT x, y FROM t2")
    assert "ORA-005" in rule_ids(results)


def test_minus_mismatched_columns_detected():
    results = analyze_sql("SELECT a, b, c FROM t1 MINUS SELECT x FROM t2")
    assert "ORA-005" in rule_ids(results)


def test_intersect_matched_columns_not_flagged():
    results = analyze_sql("SELECT a, b FROM t1 INTERSECT SELECT x, y FROM t2")
    assert "ORA-005" not in rule_ids(results)


def test_union_with_select_star_branch_skipped():
    """A SELECT * branch makes the column count unknowable offline --
    must not guess, so the whole check is skipped for this statement."""
    results = analyze_sql("SELECT * FROM t1 UNION SELECT x, y FROM t2")
    assert "ORA-005" not in rule_ids(results)


def test_nested_triple_union_mismatch_detected():
    """(A UNION B) UNION ALL C, where C has a different column count."""
    results = analyze_sql(
        "SELECT a FROM t1 UNION SELECT b FROM t2 UNION ALL SELECT c, d FROM t3"
    )
    assert "ORA-005" in rule_ids(results)


def test_nested_triple_union_all_matching_not_flagged():
    results = analyze_sql(
        "SELECT a FROM t1 UNION SELECT b FROM t2 UNION ALL SELECT c FROM t3"
    )
    assert "ORA-005" not in rule_ids(results)


def test_setop_statement_type_labels():
    """Statement-type labels should read UNION / UNION ALL / MINUS /
    INTERSECT (Oracle's own terms), not sqlglot's internal class names."""
    r1 = analyze_sql("SELECT a FROM t1 UNION SELECT a FROM t2")
    r2 = analyze_sql("SELECT a FROM t1 UNION ALL SELECT a FROM t2")
    r3 = analyze_sql("SELECT a FROM t1 MINUS SELECT a FROM t2")
    r4 = analyze_sql("SELECT a FROM t1 INTERSECT SELECT a FROM t2")
    assert statement_types(r1) == ["UNION"]
    assert statement_types(r2) == ["UNION ALL"]
    assert statement_types(r3) == ["MINUS"]
    assert statement_types(r4) == ["INTERSECT"]


def test_union_branch_still_gets_select_level_checks():
    """A UNION query's branches should still be checked for PERF-001/
    JOIN-001/WHERE-level findings, not treated as one opaque statement."""
    results = analyze_sql(
        "SELECT * FROM t1 UNION SELECT id FROM t2 WHERE 1 = 2"
    )
    ids = rule_ids(results)
    assert "PERF-001" in ids   # SELECT * in the first branch
    assert "DML-WHERE-003" in ids  # constant-false predicate in the second branch


# --------------------------------------------------------------------------
# ORA-006 -- scalar subquery may return more than one row
# --------------------------------------------------------------------------

def test_scalar_subquery_no_guard_detected():
    results = analyze_sql(
        "UPDATE employees SET salary = (SELECT sal FROM other WHERE dept = 10) "
        "WHERE employee_id = 1"
    )
    assert "ORA-006" in rule_ids(results)


def test_scalar_subquery_with_bare_aggregate_not_flagged():
    """A bare aggregate with no GROUP BY always returns exactly one row."""
    results = analyze_sql(
        "UPDATE employees SET salary = (SELECT max(sal) FROM other) "
        "WHERE employee_id = 1"
    )
    assert "ORA-006" not in rule_ids(results)


def test_scalar_subquery_with_group_by_aggregate_still_flagged():
    """An aggregate WITH a GROUP BY can still return many rows -- the
    single-row guarantee only holds for an ungrouped aggregate."""
    results = analyze_sql(
        "UPDATE employees SET salary = "
        "(SELECT max(sal) FROM other GROUP BY dept) WHERE employee_id = 1"
    )
    assert "ORA-006" in rule_ids(results)


def test_scalar_subquery_with_rownum_eq_1_not_flagged():
    results = analyze_sql(
        "UPDATE employees SET salary = (SELECT sal FROM other WHERE ROWNUM = 1) "
        "WHERE employee_id = 1"
    )
    assert "ORA-006" not in rule_ids(results)


def test_scalar_subquery_with_fetch_first_1_not_flagged():
    results = analyze_sql(
        "UPDATE employees SET salary = "
        "(SELECT sal FROM other ORDER BY sal FETCH FIRST 1 ROWS ONLY) "
        "WHERE employee_id = 1"
    )
    assert "ORA-006" not in rule_ids(results)


def test_scalar_subquery_with_limit_greater_than_1_still_flagged():
    """LIMIT/FETCH of more than one row does not guarantee single-row."""
    results = analyze_sql(
        "UPDATE employees SET salary = "
        "(SELECT sal FROM other ORDER BY sal FETCH FIRST 3 ROWS ONLY) "
        "WHERE employee_id = 1"
    )
    assert "ORA-006" in rule_ids(results)


def test_in_subquery_not_flagged():
    """IN legitimately allows a multi-row subquery -- must not be flagged."""
    results = analyze_sql(
        "SELECT * FROM employees WHERE department_id IN (SELECT id FROM depts)"
    )
    assert "ORA-006" not in rule_ids(results)


def test_exists_subquery_not_flagged():
    results = analyze_sql(
        "SELECT id FROM employees e WHERE EXISTS "
        "(SELECT 1 FROM depts d WHERE d.id = e.department_id)"
    )
    assert "ORA-006" not in rule_ids(results)


def test_any_subquery_not_flagged():
    results = analyze_sql(
        "SELECT id FROM employees WHERE salary > ANY (SELECT sal FROM other)"
    )
    assert "ORA-006" not in rule_ids(results)


def test_subquery_in_from_clause_not_flagged():
    """A derived table in FROM is not a scalar-comparison operand."""
    results = analyze_sql("SELECT id FROM (SELECT id FROM t1) sub")
    assert "ORA-006" not in rule_ids(results)


def test_scalar_subquery_in_select_where_comparison_detected():
    results = analyze_sql(
        "SELECT id FROM employees WHERE salary = (SELECT sal FROM other)"
    )
    assert "ORA-006" in rule_ids(results)


def test_scalar_subquery_not_double_counted():
    """The same subquery instance must not produce more than one finding
    even though find_all could, in principle, visit related nodes."""
    results = analyze_sql(
        "SELECT id FROM employees WHERE salary = (SELECT sal FROM other WHERE dept = 1)"
    )
    findings = [f for r in results for f in r.findings if f.rule_id == "ORA-006"]
    assert len(findings) == 1


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

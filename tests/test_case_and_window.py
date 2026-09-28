"""
Tests for CASE expression analysis and analytic/window function checks
(scope.md section 3: "CASE analysis", "analytic/window function checks"):

  CASE-001  CASE expression has no ELSE branch
  CASE-002  Duplicate WHEN value in CASE expression
  ORA-007   Analytic/window function used in WHERE/HAVING/GROUP BY (ORA-30483)
  ORA-008   RANK/DENSE_RANK/NTILE with no ORDER BY in OVER() (ORA-30485)
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


# --------------------------------------------------------------------------
# CASE-001 -- missing ELSE
# --------------------------------------------------------------------------

def test_case_no_else_detected():
    results = analyze_sql(
        "SELECT CASE WHEN status = 'A' THEN 1 WHEN status = 'B' THEN 2 END x FROM t"
    )
    assert "CASE-001" in rule_ids(results)


def test_case_with_else_not_flagged():
    results = analyze_sql(
        "SELECT CASE WHEN status = 'A' THEN 1 ELSE 0 END x FROM t"
    )
    assert "CASE-001" not in rule_ids(results)


def test_simple_case_no_else_detected():
    results = analyze_sql("SELECT CASE dept WHEN 10 THEN 'A' END x FROM t")
    assert "CASE-001" in rule_ids(results)


def test_case_in_where_clause_detected():
    results = analyze_sql(
        "SELECT id FROM t WHERE CASE WHEN status = 'A' THEN 1 WHEN status = 'B' THEN 0 END = 1"
    )
    assert "CASE-001" in rule_ids(results)


def test_case_in_update_set_detected():
    results = analyze_sql(
        "UPDATE t SET flag = CASE WHEN status = 'A' THEN 1 WHEN status = 'B' THEN 2 END "
        "WHERE id = 1"
    )
    assert "CASE-001" in rule_ids(results)


# --------------------------------------------------------------------------
# CASE-002 -- duplicate WHEN value
# --------------------------------------------------------------------------

def test_searched_case_duplicate_when_detected():
    results = analyze_sql(
        "SELECT CASE WHEN status = 'A' THEN 1 WHEN status = 'A' THEN 2 ELSE 0 END x FROM t"
    )
    assert "CASE-002" in rule_ids(results)


def test_simple_case_duplicate_when_detected():
    results = analyze_sql(
        "SELECT CASE dept WHEN 10 THEN 'A' WHEN 10 THEN 'B' ELSE 'C' END x FROM t"
    )
    assert "CASE-002" in rule_ids(results)


def test_case_no_duplicate_not_flagged():
    results = analyze_sql(
        "SELECT CASE WHEN status = 'A' THEN 1 WHEN status = 'B' THEN 2 ELSE 0 END x FROM t"
    )
    assert "CASE-002" not in rule_ids(results)


def test_simple_case_no_duplicate_not_flagged():
    results = analyze_sql(
        "SELECT CASE dept WHEN 10 THEN 'A' WHEN 20 THEN 'B' ELSE 'C' END x FROM t"
    )
    assert "CASE-002" not in rule_ids(results)


def test_case_ambiguous_when_shape_skips_duplicate_check():
    """A WHEN condition that isn't a plain column = literal comparison
    makes the duplicate check bail out entirely rather than guess -- here
    'dept > 5' can't be safely compared against 'status = A' for equality
    of intent, so CASE-002 must not fire even though CASE-001 still can."""
    results = analyze_sql(
        "SELECT CASE WHEN status = 'A' THEN 1 WHEN dept > 5 THEN 2 END x FROM t"
    )
    ids = rule_ids(results)
    assert "CASE-002" not in ids
    assert "CASE-001" in ids


def test_case_three_distinct_values_not_flagged():
    results = analyze_sql(
        "SELECT CASE dept WHEN 10 THEN 'A' WHEN 20 THEN 'B' WHEN 30 THEN 'C' END x FROM t"
    )
    assert "CASE-002" not in rule_ids(results)


# --------------------------------------------------------------------------
# ORA-007 -- analytic function in WHERE/HAVING/GROUP BY
# --------------------------------------------------------------------------

def test_window_in_where_detected():
    results = analyze_sql(
        "SELECT sal FROM employees WHERE ROW_NUMBER() OVER (ORDER BY sal) = 1"
    )
    assert "ORA-007" in rule_ids(results)


def test_window_in_having_detected():
    results = analyze_sql(
        "SELECT dept, sum(sal) s FROM employees GROUP BY dept "
        "HAVING RANK() OVER (ORDER BY sum(sal)) = 1"
    )
    assert "ORA-007" in rule_ids(results)


def test_window_in_group_by_detected():
    results = analyze_sql(
        "SELECT dept FROM employees GROUP BY dept, RANK() OVER (ORDER BY dept)"
    )
    assert "ORA-007" in rule_ids(results)


def test_window_via_wrapping_subquery_not_flagged():
    """The correct/legal pattern: compute the window function in a
    subquery's SELECT list, then filter on it in the outer query."""
    results = analyze_sql(
        "SELECT * FROM (SELECT sal, ROW_NUMBER() OVER (ORDER BY sal) rn "
        "FROM employees) WHERE rn = 1"
    )
    assert "ORA-007" not in rule_ids(results)


def test_window_in_select_list_not_flagged():
    results = analyze_sql(
        "SELECT sal, ROW_NUMBER() OVER (ORDER BY sal) rn FROM employees"
    )
    assert "ORA-007" not in rule_ids(results)


def test_window_in_order_by_not_flagged():
    results = analyze_sql(
        "SELECT sal FROM employees ORDER BY ROW_NUMBER() OVER (ORDER BY sal)"
    )
    assert "ORA-007" not in rule_ids(results)


def test_window_in_outer_where_with_scalar_subquery_in_select_not_flagged():
    """A window function inside an unrelated subquery's SELECT list, even
    though that subquery is itself referenced from the outer WHERE, is
    still legal -- the window is in the subquery's own scope, not the
    outer WHERE's scope."""
    results = analyze_sql(
        "SELECT id FROM employees WHERE dept_rank = "
        "(SELECT RANK() OVER (ORDER BY sal) FROM other WHERE ROWNUM = 1)"
    )
    assert "ORA-007" not in rule_ids(results)


# --------------------------------------------------------------------------
# ORA-008 -- RANK/DENSE_RANK/NTILE without ORDER BY
# --------------------------------------------------------------------------

def test_rank_without_order_by_detected():
    results = analyze_sql("SELECT RANK() OVER (PARTITION BY dept) x FROM employees")
    assert "ORA-008" in rule_ids(results)


def test_rank_with_order_by_not_flagged():
    results = analyze_sql(
        "SELECT RANK() OVER (PARTITION BY dept ORDER BY sal) x FROM employees"
    )
    assert "ORA-008" not in rule_ids(results)


def test_dense_rank_without_order_by_detected():
    results = analyze_sql("SELECT DENSE_RANK() OVER (PARTITION BY dept) x FROM employees")
    assert "ORA-008" in rule_ids(results)


def test_ntile_without_order_by_detected():
    results = analyze_sql("SELECT NTILE(4) OVER (PARTITION BY dept) x FROM employees")
    assert "ORA-008" in rule_ids(results)


def test_row_number_without_order_by_not_flagged():
    """ROW_NUMBER doesn't require ORDER BY at parse time in Oracle (though
    it's poor practice) -- only RANK/DENSE_RANK/NTILE do."""
    results = analyze_sql("SELECT ROW_NUMBER() OVER (PARTITION BY dept) x FROM employees")
    assert "ORA-008" not in rule_ids(results)


def test_plain_aggregate_window_without_order_by_not_flagged():
    results = analyze_sql("SELECT SUM(sal) OVER (PARTITION BY dept) x FROM employees")
    assert "ORA-008" not in rule_ids(results)


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

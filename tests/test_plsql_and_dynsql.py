"""
Tests for PL/SQL block recognition, dynamic SQL detection, CALL
statements, and multi-statement script splitting (scope.md section 3's
remaining V0.2 items: "PL/SQL block parsing", "stored procedure calls",
"dynamic SQL warnings", "multi-statement scripts"):

  PLSQL-001   PL/SQL block detected -- not analyzed
  DYNSQL-001  Dynamic SQL (EXECUTE IMMEDIATE) detected -- not analyzed
  DYNSQL-002  Dynamic SQL built via string concatenation
  CALL-001    CALL statement recognized -- not analyzed

Also covers the splitter module directly, and the per-statement parse
isolation fix (one bad statement no longer discards the whole script).
"""

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.analyzer.engine import analyze_sql
from app.analyzer import splitter as splitter_mod


def rule_ids(results):
    ids = []
    for r in results:
        ids.extend(f.rule_id for f in r.findings)
    return ids


def statement_types(results):
    return [r.statement_type for r in results]


# ---------------------------------------------------------------------
# PLSQL-001: recognized block kinds
# ---------------------------------------------------------------------

def test_anonymous_begin_block_no_slash():
    results = analyze_sql("BEGIN\n  NULL;\nEND;")
    assert len(results) == 1
    assert results[0].statement_type == "PL/SQL BLOCK"
    assert "PLSQL-001" in rule_ids(results)
    assert results[0].parse_ok


def test_declare_block_no_slash_terminator_uses_recommendation_override():
    results = analyze_sql("DECLARE\n  x NUMBER;\nBEGIN\n  x := 1;\nEND;")
    assert len(results) == 1
    r = results[0]
    assert r.statement_type == "PL/SQL BLOCK (DECLARE)"
    finding = next(f for f in r.findings if f.rule_id == "PLSQL-001")
    assert finding.recommendation is not None
    assert "/" in finding.recommendation


def test_declare_block_with_slash_terminator_no_override_needed():
    sql = "DECLARE\n  x NUMBER;\nBEGIN\n  x := 1;\nEND;\n/\n"
    results = analyze_sql(sql)
    assert len(results) == 1
    finding = next(f for f in results[0].findings if f.rule_id == "PLSQL-001")
    # A slash terminator was found, so the block boundary is known --
    # the recommendation should be the plain default, not the
    # no-terminator-found override text.
    assert "No '/' terminator was found" not in (finding.recommendation or "")


def test_procedure_recognized_and_labeled():
    sql = "CREATE OR REPLACE PROCEDURE my_proc IS\nBEGIN\n  NULL;\nEND;\n/\n"
    results = analyze_sql(sql)
    assert len(results) == 1
    assert results[0].statement_type == "PROCEDURE"
    assert "PLSQL-001" in rule_ids(results)


def test_function_recognized_and_labeled():
    sql = (
        "CREATE OR REPLACE FUNCTION my_func RETURN NUMBER IS\n"
        "BEGIN\n  RETURN 1;\nEND;\n/\n"
    )
    results = analyze_sql(sql)
    assert results[0].statement_type == "FUNCTION"
    assert "PLSQL-001" in rule_ids(results)


def test_package_recognized_and_labeled():
    sql = "CREATE OR REPLACE PACKAGE my_pkg IS\n  PROCEDURE p1;\nEND my_pkg;\n/\n"
    results = analyze_sql(sql)
    assert results[0].statement_type == "PACKAGE"
    assert "PLSQL-001" in rule_ids(results)


def test_package_body_recognized_and_labeled():
    sql = (
        "CREATE OR REPLACE PACKAGE BODY my_pkg IS\n"
        "  PROCEDURE p1 IS BEGIN NULL; END;\n"
        "END my_pkg;\n/\n"
    )
    results = analyze_sql(sql)
    assert results[0].statement_type == "PACKAGE BODY"
    assert "PLSQL-001" in rule_ids(results)


def test_trigger_recognized_and_labeled():
    sql = (
        "CREATE OR REPLACE TRIGGER my_trg BEFORE INSERT ON employees\n"
        "BEGIN\n  NULL;\nEND;\n/\n"
    )
    results = analyze_sql(sql)
    assert results[0].statement_type == "TRIGGER"
    assert "PLSQL-001" in rule_ids(results)


# ---------------------------------------------------------------------
# DYNSQL-001 / DYNSQL-002: dynamic SQL detection
# ---------------------------------------------------------------------

def test_execute_immediate_pure_literal_no_concat_warning():
    results = analyze_sql("EXECUTE IMMEDIATE 'DELETE FROM employees'")
    ids = rule_ids(results)
    assert "DYNSQL-001" in ids
    assert "DYNSQL-002" not in ids
    assert results[0].statement_type == "EXECUTE IMMEDIATE"


def test_execute_immediate_with_concatenation_flags_injection_risk():
    results = analyze_sql(
        "EXECUTE IMMEDIATE 'DELETE FROM employees WHERE id = ' || v_id"
    )
    ids = rule_ids(results)
    assert "DYNSQL-001" in ids
    assert "DYNSQL-002" in ids


def test_execute_immediate_nested_inside_procedure_body():
    sql = (
        "CREATE OR REPLACE PROCEDURE my_proc(p_id IN NUMBER) IS\n"
        "BEGIN\n"
        "  EXECUTE IMMEDIATE 'DELETE FROM employees WHERE id = ' || p_id;\n"
        "END;\n/\n"
    )
    results = analyze_sql(sql)
    assert len(results) == 1
    ids = rule_ids(results)
    assert "PLSQL-001" in ids
    assert "DYNSQL-001" in ids
    assert "DYNSQL-002" in ids


def test_execute_immediate_nested_pure_literal_no_concat_warning():
    sql = (
        "CREATE OR REPLACE PROCEDURE my_proc IS\n"
        "BEGIN\n"
        "  EXECUTE IMMEDIATE 'DELETE FROM employees';\n"
        "END;\n/\n"
    )
    results = analyze_sql(sql)
    ids = rule_ids(results)
    assert "PLSQL-001" in ids
    assert "DYNSQL-001" in ids
    assert "DYNSQL-002" not in ids


# ---------------------------------------------------------------------
# CALL-001
# ---------------------------------------------------------------------

def test_call_statement_recognized():
    results = analyze_sql("CALL my_proc(1, 2)")
    assert len(results) == 1
    assert results[0].statement_type == "CALL"
    assert "CALL-001" in rule_ids(results)


# ---------------------------------------------------------------------
# Multi-statement scripts: mixed SQL + PL/SQL, parse-failure isolation
# ---------------------------------------------------------------------

def test_multi_statement_script_with_procedure_body():
    sql = """
    UPDATE employees SET salary = 1 WHERE employee_id = 1;

    CREATE OR REPLACE PROCEDURE my_proc IS
    BEGIN
      UPDATE employees SET salary = salary * 1.1;
      DELETE FROM logs WHERE id = 1;
    END;
    /

    SELECT * FROM employees;
    """
    results = analyze_sql(sql)
    assert len(results) == 3
    types = statement_types(results)
    assert types == ["UPDATE", "PROCEDURE", "SELECT"]
    # The internal semicolons inside the procedure body must not have
    # fragmented it into extra statements.
    assert "PLSQL-001" in rule_ids([results[1]])
    assert "PERF-001" in rule_ids([results[2]])  # SELECT *


def test_bad_statement_does_not_discard_rest_of_script():
    sql = "SELECT 1 FROM dual; THIS IS NOT VALID SQL AT ALL ///; SELECT 2 FROM dual;"
    results = analyze_sql(sql)
    assert len(results) == 3
    assert results[0].parse_ok
    assert results[0].statement_type == "SELECT"
    assert not results[1].parse_ok
    assert "PARSE-001" in rule_ids([results[1]])
    assert results[2].parse_ok
    assert results[2].statement_type == "SELECT"


# ---------------------------------------------------------------------
# splitter.py: quote/comment-aware scanning edge cases
# ---------------------------------------------------------------------

def test_scan_to_char_skips_escaped_quote_semicolon():
    text = "UPDATE t SET x = 'it''s a test; still one' WHERE y = 1;"
    end, found = splitter_mod.scan_to_char(text, 0, ";")
    assert found
    assert text[end] == ";"
    assert end == len(text) - 1


def test_scan_to_char_skips_line_comment_semicolon():
    text = "SELECT 1 -- comment; with a fake terminator\nFROM dual;"
    end, found = splitter_mod.scan_to_char(text, 0, ";")
    assert found
    assert text[:end].strip().endswith("dual")


def test_scan_to_char_skips_block_comment_semicolon():
    text = "SELECT 1 /* comment; with a fake terminator */ FROM dual;"
    end, found = splitter_mod.scan_to_char(text, 0, ";")
    assert found
    assert text[:end].strip().endswith("dual")


def test_scan_to_char_skips_quoted_identifier_semicolon():
    text = 'SELECT * FROM "My Table;Name";'
    end, found = splitter_mod.scan_to_char(text, 0, ";")
    assert found
    assert text[end] == ";"
    assert end == len(text) - 1


def test_split_statements_drops_empty_chunks():
    chunks = splitter_mod.split_statements(";;  ;\nSELECT 1 FROM dual;;;")
    assert len(chunks) == 1
    assert chunks[0].sql == "SELECT 1 FROM dual"


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

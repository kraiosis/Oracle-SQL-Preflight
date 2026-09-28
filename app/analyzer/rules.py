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
Oracle SQL Preflight Analyzer - Rule Catalog

Rule metadata (severity, enabled/disabled, category, description,
recommendation) is now data, not code, per scope.md section 18
("Rule management"):

    - enable/disable      -> "enabled" field
    - severity config     -> "severity" field
    - category filtering  -> "category" field (filtering happens in the UI/API)

The catalog is loaded from config/rules.json. If that file is missing or
invalid, the engine still starts up cleanly using DEFAULT_RULES (the
same rule set shipped in v0.1), and config/rules.json is written out so
it becomes the editable source of truth from then on.

The engine (engine.py) is responsible for detection logic; this module
owns the metadata every finding must carry (scope.md section 2.4,
"Evidence-based findings").
"""

from __future__ import annotations

import json
import pathlib
import threading
from dataclasses import dataclass, asdict
from typing import Optional

VALID_SEVERITIES = ("CRITICAL", "ERROR", "WARNING", "INFO")
VALID_CATEGORIES = ("PARSE", "DML", "LOGIC", "PERF", "JOIN", "SCHEMA", "ORACLE", "SELECT", "CASE", "BIND", "PLSQL", "INFO")

_DEFAULT_CONFIG_PATH = (
    pathlib.Path(__file__).resolve().parents[2] / "config" / "rules.json"
)

_lock = threading.RLock()


@dataclass
class Rule:
    id: str
    category: str            # PARSE, DML, LOGIC, PERF, INFO
    severity: str             # CRITICAL, ERROR, WARNING, INFO
    title: str
    description: str
    recommendation: Optional[str] = None
    requires_database: bool = False
    enabled: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------
# Built-in fallback catalog (identical to the v0.1 rule set). Used only if
# config/rules.json cannot be read, so the analyzer never fails to start
# because of a missing/corrupt config file (scope.md 2.5, conservative /
# fail-safe behavior).
# --------------------------------------------------------------------------

DEFAULT_RULES: list[dict] = [
    {
        "id": "PARSE-001", "category": "PARSE", "severity": "CRITICAL", "enabled": True,
        "title": "SQL could not be parsed",
        "description": "The statement could not be parsed using the Oracle SQL dialect.",
        "recommendation": "Fix the SQL syntax and re-run analysis.",
        "requires_database": False,
    },
    {
        "id": "PARSE-002", "category": "PARSE", "severity": "INFO", "enabled": True,
        "title": "Statement type not yet analyzed",
        "description": "The statement parsed successfully but this statement type has no "
                        "dedicated logic rules in the current MVP.",
        "recommendation": "No action required. Support for this statement type is planned.",
        "requires_database": False,
    },
    {
        "id": "DML-001", "category": "DML", "severity": "CRITICAL", "enabled": True,
        "title": "UPDATE has no WHERE clause",
        "description": "This statement can update every row in the target table.",
        "recommendation": "Add a WHERE clause that targets only the intended rows, or "
                           "confirm that a full-table update is intentional.",
        "requires_database": False,
    },
    {
        "id": "DML-002", "category": "DML", "severity": "CRITICAL", "enabled": True,
        "title": "DELETE has no WHERE clause",
        "description": "This statement can delete every row in the target table.",
        "recommendation": "Add a WHERE clause that targets only the intended rows, or "
                           "confirm that a full-table delete is intentional.",
        "requires_database": False,
    },
    {
        "id": "DML-SET-WHERE-001", "category": "DML", "severity": "WARNING", "enabled": True,
        "title": "Potential SET/WHERE no-op",
        "description": "A column is assigned the same literal value that the WHERE clause "
                        "requires it to already equal. Rows matching WHERE may already hold "
                        "this value for this column.",
        "recommendation": "Verify whether this column needs to be updated at all for rows "
                           "that already satisfy the WHERE clause.",
        "requires_database": False,
    },
    {
        "id": "DML-SET-SELF-001", "category": "DML", "severity": "WARNING", "enabled": True,
        "title": "Self-assignment (unconditional no-op)",
        "description": "A column is assigned to itself with no transformation (e.g. "
                        "SET salary = salary). This assignment never changes the value of "
                        "any matched row, regardless of the WHERE clause or the underlying "
                        "data.",
        "recommendation": "Remove this assignment, or replace it with the transformation "
                           "you actually intended.",
        "requires_database": False,
    },
    {
        "id": "DML-SET-FUNC-001", "category": "DML", "severity": "INFO", "enabled": True,
        "title": "Possible function-wrapped no-op assignment",
        "description": "A column is assigned the result of a function applied to itself "
                        "(e.g. SET status = UPPER(status), SET salary = NVL(salary, 0)). "
                        "This is a no-op for any row where the function does not change the "
                        "existing value -- whether that is the case depends on the data, "
                        "which is unknown offline.",
        "recommendation": "Confirm whether this assignment is expected to change values for "
                           "the rows it will match; the expression cannot be proven a no-op "
                           "without seeing the data.",
        "requires_database": True,
    },
    {
        "id": "DML-WHERE-002", "category": "LOGIC", "severity": "WARNING", "enabled": True,
        "title": "Contradictory WHERE equality",
        "description": "The same column is required by AND-ed equality predicates to equal "
                        "more than one different literal value. No row can satisfy this.",
        "recommendation": "Review the WHERE clause; as written no row can satisfy both "
                           "conditions simultaneously.",
        "requires_database": False,
    },
    {
        "id": "DML-WHERE-007", "category": "LOGIC", "severity": "WARNING", "enabled": True,
        "title": "Contradictory numeric range",
        "description": "AND-ed numeric range predicates on the same column (using >, >=, <, "
                        "<=) leave no possible value -- the lower bound excludes every value "
                        "the upper bound allows. No row can satisfy this.",
        "recommendation": "Review the range bounds on this column; as written they cannot "
                           "both hold for any value.",
        "requires_database": False,
    },
    {
        "id": "DML-WHERE-003", "category": "LOGIC", "severity": "WARNING", "enabled": True,
        "title": "Constant-false predicate",
        "description": "The WHERE clause contains a predicate that is always false "
                        "(e.g. 1 = 2, or two different literals compared for equality). "
                        "The statement may affect zero rows.",
        "recommendation": "Review the WHERE clause; this condition can never be true.",
        "requires_database": False,
    },
    {
        "id": "DML-WHERE-004", "category": "LOGIC", "severity": "INFO", "enabled": True,
        "title": "Always-true predicate",
        "description": "The WHERE clause contains a predicate that is always true "
                        "(e.g. 1 = 1). This may be intentional, but combined with other "
                        "conditions it does not filter any rows.",
        "recommendation": "Confirm this condition is intentional (e.g. temporary debugging "
                           "code left in the statement).",
        "requires_database": False,
    },
    {
        "id": "DML-WHERE-005", "category": "LOGIC", "severity": "WARNING", "enabled": True,
        "title": "Equality compared to NULL",
        "description": "A predicate uses '= NULL' or '<> NULL'. In Oracle, NULL comparisons "
                        "with = or <> never evaluate to TRUE. IS NULL / IS NOT NULL should "
                        "be used instead.",
        "recommendation": "Replace '= NULL' / '<> NULL' with IS NULL / IS NOT NULL.",
        "requires_database": False,
    },
    {
        "id": "DML-WHERE-006", "category": "LOGIC", "severity": "INFO", "enabled": True,
        "title": "Column compared to itself",
        "description": "A predicate compares a column to itself (e.g. column = column). "
                        "Due to Oracle NULL semantics this excludes any row where the "
                        "column is NULL, which may be unintended.",
        "recommendation": "Confirm whether NULL-valued rows should be excluded here.",
        "requires_database": False,
    },
    {
        "id": "DML-003", "category": "DML", "severity": "INFO", "enabled": True,
        "title": "INSERT statement recognized",
        "description": "INSERT statements are recognized structurally. Data-dependent "
                        "validation (datatypes, constraints, sequence/identity behavior) "
                        "requires Oracle metadata and is not available offline.",
        "recommendation": "Connect to an Oracle DEV/TEST environment for datatype and "
                           "constraint validation (planned feature).",
        "requires_database": True,
    },
    {
        "id": "DML-004", "category": "DML", "severity": "INFO", "enabled": True,
        "title": "MERGE statement recognized",
        "description": "MERGE statements are recognized structurally. Match-condition "
                        "cardinality and branch-level risk analysis require Oracle metadata "
                        "and/or data and are not available offline.",
        "recommendation": "Connect to an Oracle DEV/TEST environment for match-condition "
                           "cardinality analysis (planned feature).",
        "requires_database": True,
    },
    {
        "id": "PERF-001", "category": "PERF", "severity": "INFO", "enabled": True,
        "title": "SELECT * detected",
        "description": "The statement selects all columns with SELECT *. This is not "
                        "necessarily wrong, but it can hide the true column dependency "
                        "surface and may retrieve more data than needed.",
        "recommendation": "Consider listing only the columns actually needed.",
        "requires_database": False,
    },
    {
        "id": "JOIN-001", "category": "JOIN", "severity": "WARNING", "enabled": True,
        "title": "Possible Cartesian join (missing join predicate)",
        "description": "A table in the FROM/JOIN list has no equality predicate -- in an "
                        "ON clause or the WHERE clause -- connecting it to any other table "
                        "in the query. This can produce a Cartesian product, multiplying "
                        "the result set by that table's row count.",
        "recommendation": "Add a join predicate (ON or WHERE) connecting this table to the "
                           "rest of the query, or use an explicit CROSS JOIN to state that "
                           "a full cross join is intentional.",
        "requires_database": False,
    },
    {
        "id": "SCHEMA-001", "category": "SCHEMA", "severity": "ERROR", "enabled": True,
        "title": "Unknown column for target table",
        "description": "This column is not defined on the target table in the offline "
                        "schema you imported (Schema tab / config/schema.json). If the "
                        "imported schema is accurate and current, this statement would "
                        "fail with an Oracle invalid-identifier error (ORA-00904).",
        "recommendation": "Check the column name for a typo, or re-import the table's DDL "
                           "if the schema you imported is out of date.",
        "requires_database": False,
    },
    {
        "id": "SCHEMA-002", "category": "SCHEMA", "severity": "INFO", "enabled": True,
        "title": "WHERE clause does not reference the primary key",
        "description": "The target table has a primary key defined in the imported schema, "
                        "but none of its primary-key columns appear as an equality "
                        "predicate in the WHERE clause. This does not mean the statement is "
                        "wrong -- many valid updates/deletes filter on other columns -- but "
                        "it is worth a second look before running against more than one row.",
        "recommendation": "Confirm this statement is meant to affect a set of rows rather "
                           "than a single row identified by primary key.",
        "requires_database": False,
    },
    {
        "id": "ORA-001", "category": "ORACLE", "severity": "CRITICAL", "enabled": True,
        "title": "Division by zero (ORA-01476)",
        "description": "An expression divides by the literal 0. Oracle raises "
                        "ORA-01476: divisor is equal to zero for this on every execution -- "
                        "it does not depend on data, so this is not a maybe.",
        "recommendation": "Remove the division by zero, or guard it (e.g. NULLIF(denominator, 0)) "
                           "if a zero denominator is a real, expected case.",
        "requires_database": False,
    },
    {
        "id": "ORA-002", "category": "ORACLE", "severity": "ERROR", "enabled": True,
        "title": "Non-numeric literal compared against a NUMBER column (ORA-01722)",
        "description": "A NUMBER-family column (per the offline schema you imported) is "
                        "compared or assigned a string literal that cannot be interpreted as "
                        "a number. Oracle would raise ORA-01722: invalid number for this at "
                        "execution time.",
        "recommendation": "Remove the quotes if this was meant to be a numeric literal, or "
                           "check whether the imported schema for this column is out of date.",
        "requires_database": False,
    },
    {
        "id": "ORA-003", "category": "ORACLE", "severity": "ERROR", "enabled": True,
        "title": "NULL assigned to a NOT NULL column (ORA-01400 / ORA-01407)",
        "description": "A column marked NOT NULL in the offline schema you imported is "
                        "explicitly assigned NULL. Oracle would raise ORA-01400 (INSERT) or "
                        "ORA-01407 (UPDATE) for this, unless the imported schema is out of "
                        "date. This check only looks at explicit NULL literals -- it does not "
                        "know about DEFAULT clauses, so an omitted column is never flagged.",
        "recommendation": "Provide a real value for this column, or check whether the "
                           "imported schema's NOT NULL constraint is still accurate.",
        "requires_database": False,
    },
    {
        "id": "ORA-004", "category": "ORACLE", "severity": "ERROR", "enabled": True,
        "title": "String literal longer than the column's declared size (ORA-12899)",
        "description": "A string literal is longer than the VARCHAR2/CHAR length declared "
                        "for this column in the offline schema you imported. Oracle would "
                        "raise ORA-12899: value too large for column for this. This check "
                        "compares character counts and does not account for BYTE-length "
                        "columns or multi-byte character sets, so it can be off for those.",
        "recommendation": "Shorten the value, widen the column, or check whether the "
                           "imported schema's column length is still accurate.",
        "requires_database": False,
    },
    {
        "id": "ORA-005", "category": "ORACLE", "severity": "ERROR", "enabled": True,
        "title": "Mismatched column counts across UNION/INTERSECT/MINUS branches (ORA-01789)",
        "description": "The branches of a UNION, UNION ALL, INTERSECT, or MINUS query select "
                        "different numbers of columns. Oracle would raise ORA-01789: query "
                        "block has incorrect number of result columns for this -- this is "
                        "provable from the SQL text alone, not a maybe.",
        "recommendation": "Make every branch select the same number of columns.",
        "requires_database": False,
    },
    {
        "id": "ORA-006", "category": "ORACLE", "severity": "WARNING", "enabled": True,
        "title": "Scalar subquery may return more than one row (ORA-01427)",
        "description": "A subquery is used where Oracle requires at most one row (as the "
                        "operand of =, <>, >, <, >=, or <=), and nothing in its structure "
                        "(a bare aggregate with no GROUP BY, ROWNUM = 1, or FETCH FIRST 1 "
                        "ROWS ONLY) guarantees it returns only one row. If it returns more "
                        "than one row at runtime, Oracle raises ORA-01427: single-row "
                        "subquery returns more than one row. This is not proof the subquery "
                        "IS multi-row -- a unique index or other constraint not visible "
                        "offline may already guarantee it -- only that this check cannot "
                        "rule it out from the SQL alone.",
        "recommendation": "Confirm the subquery is guaranteed to return at most one row "
                           "(e.g. via a unique constraint), or add an explicit single-row "
                           "guard such as ROWNUM = 1 / FETCH FIRST 1 ROW ONLY / an aggregate.",
        "requires_database": True,
    },
    {
        "id": "CTE-001", "category": "SELECT", "severity": "INFO", "enabled": True,
        "title": "Unused CTE (WITH-clause subquery never referenced)",
        "description": "A subquery defined in the WITH clause is never referenced anywhere "
                        "else in the statement -- not by the main query, and not by any "
                        "other CTE. This is provable from the SQL text alone: the name "
                        "simply does not appear as a table reference anywhere in the "
                        "statement.",
        "recommendation": "Remove the unused CTE, or check whether a reference to it was "
                           "meant to be added elsewhere in the statement.",
        "requires_database": False,
    },
    {
        "id": "CASE-001", "category": "CASE", "severity": "INFO", "enabled": True,
        "title": "CASE expression has no ELSE branch",
        "description": "This CASE expression has no ELSE. Any row that matches none of the "
                        "WHEN conditions silently evaluates to NULL, which is easy to miss "
                        "downstream.",
        "recommendation": "Add an explicit ELSE branch (even ELSE NULL) if that is really the "
                           "intended fallback, so the behavior is visible rather than implicit.",
        "requires_database": False,
    },
    {
        "id": "CASE-002", "category": "CASE", "severity": "WARNING", "enabled": True,
        "title": "Duplicate WHEN value in CASE expression",
        "description": "The same WHEN value (simple CASE) or WHEN column = literal condition "
                        "(searched CASE) appears more than once in this CASE expression. "
                        "Oracle always takes the first matching WHEN, so every later branch "
                        "with the same value can never be reached.",
        "recommendation": "Remove the unreachable WHEN branch, or fix the value if it was "
                           "meant to test something else.",
        "requires_database": False,
    },
    {
        "id": "ORA-007", "category": "ORACLE", "severity": "ERROR", "enabled": True,
        "title": "Analytic/window function used in WHERE, HAVING, or GROUP BY (ORA-30483)",
        "description": "An analytic (window) function is used directly in this query "
                        "block's own WHERE, HAVING, or GROUP BY clause. Oracle only allows an "
                        "analytic function in the SELECT list or ORDER BY of the query block "
                        "that computes it -- Oracle raises ORA-30483: window functions are "
                        "not allowed here for this.",
        "recommendation": "Compute the analytic function in a subquery or WITH clause and "
                           "filter on the resulting column in the outer query instead.",
        "requires_database": False,
    },
    {
        "id": "ORA-008", "category": "ORACLE", "severity": "ERROR", "enabled": True,
        "title": "RANK/DENSE_RANK/NTILE with no ORDER BY in OVER() (ORA-30485)",
        "description": "RANK, DENSE_RANK, and NTILE are only meaningful relative to an "
                        "ordering, and Oracle requires an ORDER BY inside the OVER() clause "
                        "for these functions -- without one, Oracle raises ORA-30485: missing "
                        "window specification for this analytic function.",
        "recommendation": "Add an ORDER BY inside the OVER() clause, e.g. "
                           "OVER (PARTITION BY ... ORDER BY ...).",
        "requires_database": False,
    },
    {
        "id": "SUBQ-001", "category": "PERF", "severity": "INFO", "enabled": True,
        "title": "Correlated subquery detected",
        "description": "This subquery references a column from its enclosing query's own "
                        "FROM/JOIN list, making it a correlated subquery: Oracle re-evaluates "
                        "it once per row of the outer query rather than once overall. This is "
                        "not necessarily wrong -- it is often exactly what's intended (e.g. "
                        "EXISTS checks) -- but it is worth knowing when reasoning about "
                        "performance on a large outer row set.",
        "recommendation": "Confirm the per-row re-evaluation is intended; for large outer "
                           "row sets, consider whether the same result could be obtained with "
                           "a JOIN instead.",
        "requires_database": False,
    },
    {
        "id": "BIND-001", "category": "BIND", "severity": "WARNING", "enabled": True,
        "title": "Bind variable compared against columns of different types",
        "description": "The same named bind variable (:name) is compared against columns "
                        "that the offline schema you imported classifies under different "
                        "type categories (e.g. one NUMBER-family column and one VARCHAR2/CHAR "
                        "column). Reusing one bind variable name for genuinely different "
                        "kinds of values is a common copy/paste mistake. This only fires when "
                        "both columns are covered by the imported schema and qualified with a "
                        "table name/alias -- anything it cannot resolve is silently skipped.",
        "recommendation": "Use a distinct bind variable name for each logically different "
                           "value, or confirm the same value is genuinely meant to be compared "
                           "against both columns.",
        "requires_database": False,
    },
    {
        "id": "PLSQL-001", "category": "PLSQL", "severity": "INFO", "enabled": True,
        "title": "PL/SQL block detected -- not analyzed",
        "description": "This is an anonymous PL/SQL block (DECLARE/BEGIN...END) or the body "
                        "of a CREATE PROCEDURE/FUNCTION/PACKAGE/TRIGGER. This tool's parser "
                        "(SQLGlot) parses SQL, not PL/SQL, so nothing inside this block was "
                        "checked by any rule in this catalog -- this finding only confirms the "
                        "block was recognized and its boundaries (up to the next '/' on its "
                        "own line, or the end of the script) correctly identified.",
        "recommendation": "Review this block's logic manually or in an Oracle-aware IDE. Any "
                           "SQL statements inside it are not covered by this analyzer.",
        "requires_database": True,
    },
    {
        "id": "DYNSQL-001", "category": "PLSQL", "severity": "INFO", "enabled": True,
        "title": "Dynamic SQL (EXECUTE IMMEDIATE) detected -- not analyzed",
        "description": "This statement executes SQL text that is built or supplied at "
                        "runtime, so the SQL actually run cannot be checked by this offline "
                        "analyzer -- only that an EXECUTE IMMEDIATE call exists here.",
        "recommendation": "If the executed SQL text is fixed or known, consider analyzing it "
                           "separately by pasting it directly.",
        "requires_database": True,
    },
    {
        "id": "DYNSQL-002", "category": "PLSQL", "severity": "WARNING", "enabled": True,
        "title": "Dynamic SQL built via string concatenation",
        "description": "This EXECUTE IMMEDIATE statement builds its SQL text by "
                        "concatenating pieces with || rather than using a single literal "
                        "string. If any concatenated piece comes from unvalidated external "
                        "input, this is a SQL-injection risk.",
        "recommendation": "Prefer bind variables (an EXECUTE IMMEDIATE ... USING clause) over "
                           "concatenating values directly into the SQL text, especially for "
                           "anything derived from user input.",
        "requires_database": False,
    },
    {
        "id": "CALL-001", "category": "PLSQL", "severity": "INFO", "enabled": True,
        "title": "CALL statement recognized -- not analyzed",
        "description": "This CALL statement invokes a stored procedure or function. "
                        "Validating its arguments (count, types, in/out modes) requires the "
                        "procedure's own signature from Oracle metadata, which is not "
                        "available offline.",
        "recommendation": "Connect to an Oracle DEV/TEST environment for signature "
                           "validation (planned feature).",
        "requires_database": True,
    },
]


class RuleConfigError(Exception):
    pass


def _validate_rule_dict(d: dict) -> None:
    for required in ("id", "category", "severity", "title", "description"):
        if not d.get(required):
            raise RuleConfigError(f"Rule missing required field '{required}': {d}")
    if d["severity"] not in VALID_SEVERITIES:
        raise RuleConfigError(
            f"Rule {d['id']}: invalid severity '{d['severity']}', "
            f"must be one of {VALID_SEVERITIES}"
        )


def _parse_rules(raw: dict) -> dict[str, Rule]:
    items = raw.get("rules") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise RuleConfigError("rules.json must contain a top-level 'rules' list")

    parsed: dict[str, Rule] = {}
    for item in items:
        _validate_rule_dict(item)
        rule = Rule(
            id=item["id"],
            category=item["category"],
            severity=item["severity"],
            title=item["title"],
            description=item["description"],
            recommendation=item.get("recommendation"),
            requires_database=bool(item.get("requires_database", False)),
            enabled=bool(item.get("enabled", True)),
        )
        parsed[rule.id] = rule
    return parsed


def _write_default_config(path: pathlib.Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"rules": DEFAULT_RULES}, f, indent=2)
    except OSError:
        # Best-effort only. Offline analysis must keep working even if the
        # config directory is not writable.
        pass


def load_rules(path: Optional[pathlib.Path] = None) -> dict[str, Rule]:
    """Load the rule catalog from a JSON file.

    Falls back to DEFAULT_RULES (in-memory) if the file is missing,
    unreadable, or fails validation, so offline analysis never breaks
    because of a bad config file. When the file is simply missing, it is
    written out from the defaults so it becomes editable going forward.
    """
    cfg_path = path or _DEFAULT_CONFIG_PATH

    if not cfg_path.exists():
        _write_default_config(cfg_path)
        return _parse_rules({"rules": DEFAULT_RULES})

    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return _parse_rules(raw)
    except (json.JSONDecodeError, RuleConfigError, OSError):
        # Corrupt/invalid config: do not crash the analyzer. Use defaults
        # for this run and leave the broken file alone so the user can
        # fix or inspect it.
        return _parse_rules({"rules": DEFAULT_RULES})


def save_rules(rules: dict[str, Rule], path: Optional[pathlib.Path] = None) -> None:
    """Persist the current in-memory catalog back to config/rules.json."""
    cfg_path = path or _DEFAULT_CONFIG_PATH
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"rules": [r.to_dict() for r in rules.values()]}
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


# --------------------------------------------------------------------------
# Module-level catalog (loaded once at import time, reloadable at runtime)
# --------------------------------------------------------------------------

RULES: dict[str, Rule] = load_rules()


def reload_rules(path: Optional[pathlib.Path] = None) -> dict[str, Rule]:
    """Re-read config/rules.json from disk. Safe to call anytime, e.g.
    after a user hand-edits the file or via POST /api/rules/reload.
    """
    global RULES
    with _lock:
        RULES = load_rules(path)
        return RULES


def get_rule(rule_id: str) -> Optional[Rule]:
    with _lock:
        return RULES.get(rule_id)


def all_rules() -> list[Rule]:
    with _lock:
        return list(RULES.values())


def is_enabled(rule_id: str) -> bool:
    rule = get_rule(rule_id)
    return bool(rule and rule.enabled)


def update_rule(rule_id: str, *, severity: Optional[str] = None,
                 enabled: Optional[bool] = None, persist: bool = True) -> Rule:
    """Update severity and/or enabled state for one rule and persist the
    whole catalog back to config/rules.json (scope.md 18: "enable/disable,
    severity configuration").
    """
    with _lock:
        rule = RULES.get(rule_id)
        if rule is None:
            raise KeyError(f"Unknown rule id: {rule_id}")
        if severity is not None:
            if severity not in VALID_SEVERITIES:
                raise RuleConfigError(
                    f"Invalid severity '{severity}', must be one of {VALID_SEVERITIES}"
                )
            rule.severity = severity
        if enabled is not None:
            rule.enabled = enabled
        if persist:
            save_rules(RULES)
        return rule

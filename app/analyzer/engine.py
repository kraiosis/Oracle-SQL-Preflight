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
Oracle SQL Preflight Analyzer - Analysis Engine

Design principles enforced here (see scope.md section 2):

  * Offline first  - no network access is used or required.
  * AI-free core   - purely rule-based / deterministic. Same SQL + same
                      rules => same result, always.
  * Evidence-based - every finding carries rule id, severity, evidence,
                      an explanation, a recommendation, and whether it is
                      static or database-dependent.
  * Conservative   - the engine only ever claims PROVEN / POSSIBLE /
                      UNKNOWN. It never invents database facts
                      (row counts, execution plans, etc.) in offline mode.

Rule metadata (severity, enabled/disabled) now comes from
config/rules.json via rules.py (scope.md section 18, "Rule management").
This module only contains detection *logic* -- it asks rules.py for the
current severity/enabled state of each rule id every time a finding is
about to be created, so edits to rules.json take effect immediately
(including via POST /api/rules/reload), without restarting the server.

This module intentionally implements only the MVP (v0.1) rule set
described in scope.md section 3 / README.md section 3. Everything else
in scope.md ("Planned expansion") is future work and is NOT implemented
here, so the tool never silently overclaims coverage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Optional

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from .rules import get_rule
from . import schema as schema_mod
from . import splitter as splitter_mod

DIALECT = "oracle"

CONFIDENCE_PROVEN = "PROVEN"
CONFIDENCE_POSSIBLE = "POSSIBLE"
CONFIDENCE_UNKNOWN = "UNKNOWN"
CONFIDENCE_DB_VERIFIED = "DATABASE-VERIFIED"
CONFIDENCE_SCHEMA = "SCHEMA-VERIFIED"
# SCHEMA-VERIFIED means "checked against a schema the user imported into
# this tool" (schema.py / config/schema.json) -- NOT a live database. It
# is deliberately a different tier from DATABASE-VERIFIED (scope.md 2.5)
# because a user-provided schema can be stale; only an actual Oracle
# connection (scope.md section 7/8, not implemented yet) earns
# DATABASE-VERIFIED.


@dataclass
class Finding:
    rule_id: str
    severity: str
    category: str
    title: str
    evidence: str
    why_it_matters: str
    recommendation: str
    confidence: str
    basis: str  # "static" or "database"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class StatementResult:
    index: int
    statement_type: str
    sql: str
    formatted_sql: Optional[str]
    findings: list[Finding] = field(default_factory=list)
    parse_ok: bool = True
    parse_error: Optional[str] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["findings"] = [f.to_dict() for f in self.findings]
        return d


def _make_finding(rule_id: str, evidence: str, confidence: str, basis: str = "static",
                   recommendation_override: Optional[str] = None) -> Optional[Finding]:
    """Build a Finding from the current rule catalog state.

    Returns None (instead of a Finding) if the rule is disabled in
    config/rules.json -- callers use _add()/_only() below so a disabled
    rule silently produces no finding, without special-casing every call
    site.
    """
    rule = get_rule(rule_id)
    if rule is None:
        raise KeyError(f"Unknown rule id: {rule_id}")
    if not rule.enabled:
        return None
    return Finding(
        rule_id=rule.id,
        severity=rule.severity,          # picked up live from rules.json
        category=rule.category,
        title=rule.title,
        evidence=evidence,
        why_it_matters=rule.description,
        recommendation=recommendation_override or rule.recommendation
        or "Review the finding and confirm it is intentional.",
        confidence=confidence,
        basis=basis,
    )


def _add(findings: list[Finding], finding: Optional[Finding]) -> None:
    """Append a finding only if it was actually produced (rule enabled)."""
    if finding is not None:
        findings.append(finding)


def _only(*findings: Optional[Finding]) -> list[Finding]:
    """Build a findings list from one or more possibly-None findings."""
    return [f for f in findings if f is not None]


# --------------------------------------------------------------------------
# WHERE-clause predicate helpers
# --------------------------------------------------------------------------

def _flatten_and(node: Optional[exp.Expression]) -> list[exp.Expression]:
    """Flatten a top-level chain of AND conditions into a flat list.
    Only ANDs are flattened (never ORs) because OR does not imply all
    conditions must simultaneously hold, so contradiction/no-op logic
    does not apply across an OR boundary.
    """
    if node is None:
        return []
    if isinstance(node, exp.And):
        return _flatten_and(node.left) + _flatten_and(node.right)
    if isinstance(node, exp.Paren):
        return _flatten_and(node.this)
    return [node]


def _literal_value(node: exp.Expression):
    """Return a hashable python value for a Literal node, else None."""
    if isinstance(node, exp.Literal):
        if node.is_string:
            return ("str", node.this)
        try:
            return ("num", float(node.this))
        except (TypeError, ValueError):
            return ("num", node.this)
    if isinstance(node, exp.Null):
        return ("null", None)
    if isinstance(node, exp.Boolean):
        return ("bool", node.this)
    return None


def _literal_display(node: exp.Expression) -> str:
    if isinstance(node, exp.Literal) and node.is_string:
        return f"'{node.this}'"
    return node.sql(dialect=DIALECT)


def _column_key(node: exp.Expression) -> Optional[str]:
    """Return a normalized dotted column name for a Column node, else None."""
    if isinstance(node, exp.Column):
        parts = [p.this for p in node.parts]
        return ".".join(str(p).upper() for p in parts)
    return None


def _is_null_literal(node: exp.Expression) -> bool:
    return isinstance(node, exp.Null)


def _analyze_where_predicates(where: Optional[exp.Where]) -> list[Finding]:
    findings: list[Finding] = []
    if where is None:
        return findings

    conditions = _flatten_and(where.this)

    # Track literal equalities per column to detect contradictions,
    # and remember them for SET/WHERE no-op comparison by the caller.
    equalities_by_col: dict[str, list[tuple]] = {}

    # Track normalized range bounds per column for contradiction detection:
    # {col: {"lower": [(op, value, node), ...], "upper": [(op, value, node), ...]}}
    # where op in {'>','>='} for lower bounds and {'<','<='} for upper bounds,
    # both expressed relative to the column (i.e. already normalized for
    # "literal < column" style predicates).
    ranges_by_col: dict[str, dict[str, list[tuple]]] = {}

    for cond in conditions:
        # Constant-false / constant-true literal-vs-literal comparisons,
        # e.g. 1 = 2, 1 = 1, 'A' = 'B'
        if isinstance(cond, (exp.EQ, exp.NEQ)):
            left, right = cond.left, cond.right

            # NULL comparisons: column = NULL / column <> NULL
            if _is_null_literal(left) or _is_null_literal(right):
                other = right if _is_null_literal(left) else left
                if isinstance(other, exp.Column):
                    _add(findings, _make_finding(
                        "DML-WHERE-005",
                        evidence=cond.sql(dialect=DIALECT),
                        confidence=CONFIDENCE_PROVEN,
                    ))
                continue

            lval = _literal_value(left)
            rval = _literal_value(right)
            if lval is not None and rval is not None:
                if isinstance(cond, exp.EQ):
                    if lval == rval:
                        _add(findings, _make_finding(
                            "DML-WHERE-004",
                            evidence=cond.sql(dialect=DIALECT),
                            confidence=CONFIDENCE_PROVEN,
                        ))
                    else:
                        _add(findings, _make_finding(
                            "DML-WHERE-003",
                            evidence=cond.sql(dialect=DIALECT),
                            confidence=CONFIDENCE_PROVEN,
                        ))
                else:  # NEQ
                    if lval != rval:
                        _add(findings, _make_finding(
                            "DML-WHERE-004",
                            evidence=cond.sql(dialect=DIALECT),
                            confidence=CONFIDENCE_PROVEN,
                        ))
                    else:
                        _add(findings, _make_finding(
                            "DML-WHERE-003",
                            evidence=cond.sql(dialect=DIALECT),
                            confidence=CONFIDENCE_PROVEN,
                        ))
                continue

            # column = column (self / same-name comparison)
            if isinstance(cond, exp.EQ) and isinstance(left, exp.Column) and isinstance(right, exp.Column):
                if _column_key(left) == _column_key(right):
                    _add(findings, _make_finding(
                        "DML-WHERE-006",
                        evidence=cond.sql(dialect=DIALECT),
                        confidence=CONFIDENCE_POSSIBLE,
                    ))
                continue

            # column = literal -> track for contradiction detection
            if isinstance(cond, exp.EQ):
                col_node = left if isinstance(left, exp.Column) else (
                    right if isinstance(right, exp.Column) else None)
                lit_node = right if col_node is left else left
                if col_node is not None and _literal_value(lit_node) is not None:
                    key = _column_key(col_node)
                    equalities_by_col.setdefault(key, []).append(
                        (_literal_value(lit_node), lit_node, cond))

        elif isinstance(cond, (exp.GT, exp.LT, exp.GTE, exp.LTE)):
            # Normalize "literal <op> column" and "column <op> literal" into
            # a single direction relative to the column, e.g.
            #   100 < amount   ==  amount > 100
            #   amount <= 50   stays  amount <= 50
            left, right = cond.left, cond.right
            col_node = None
            lit_val = None
            op = None

            base_op = {
                exp.GT: ">", exp.GTE: ">=", exp.LT: "<", exp.LTE: "<=",
            }[type(cond)]
            flipped_op = {
                ">": "<", ">=": "<=", "<": ">", "<=": ">=",
            }[base_op]

            if isinstance(left, exp.Column) and _literal_value(right) is not None:
                col_node, lit_val, op = left, _literal_value(right), base_op
            elif isinstance(right, exp.Column) and _literal_value(left) is not None:
                # literal <op> column  ->  column <flipped_op> literal
                col_node, lit_val, op = right, _literal_value(left), flipped_op

            # Only numeric literals participate in range-contradiction math.
            if col_node is not None and lit_val is not None and lit_val[0] == "num":
                key = _column_key(col_node)
                bucket = ranges_by_col.setdefault(key, {"lower": [], "upper": []})
                side = "lower" if op in (">", ">=") else "upper"
                bucket[side].append((op, lit_val[1], cond))

    # Contradictory equality: same column required to equal >1 distinct literal
    for col, entries in equalities_by_col.items():
        distinct_values = {v for v, _, _ in entries}
        if len(distinct_values) > 1:
            evidence = " AND ".join(c.sql(dialect=DIALECT) for _, _, c in entries)
            _add(findings, _make_finding(
                "DML-WHERE-002",
                evidence=evidence,
                confidence=CONFIDENCE_PROVEN,
            ))

    # Contradictory numeric range: a lower bound that excludes every value
    # the upper bound allows, e.g. amount > 100 AND amount < 50, or
    # amount > 10 AND amount < 10 (strict bound excludes the touching point).
    for col, bucket in ranges_by_col.items():
        for lower_op, lower_val, lower_node in bucket["lower"]:
            for upper_op, upper_val, upper_node in bucket["upper"]:
                contradictory = False
                if lower_val > upper_val:
                    contradictory = True
                elif lower_val == upper_val and (lower_op == ">" or upper_op == "<"):
                    contradictory = True
                if contradictory:
                    evidence = (f"{lower_node.sql(dialect=DIALECT)} AND "
                                f"{upper_node.sql(dialect=DIALECT)}")
                    _add(findings, _make_finding(
                        "DML-WHERE-007",
                        evidence=evidence,
                        confidence=CONFIDENCE_PROVEN,
                    ))

    return findings


def _where_literal_equalities(where: Optional[exp.Where]) -> dict[str, tuple]:
    """Return {COLUMN: (literal_value, literal_node)} for simple top-level
    'column = literal' equality predicates in a WHERE clause (AND-ed only).
    Used for SET/WHERE no-op comparison.
    """
    result: dict[str, tuple] = {}
    if where is None:
        return result
    for cond in _flatten_and(where.this):
        if isinstance(cond, exp.EQ):
            left, right = cond.left, cond.right
            col_node = left if isinstance(left, exp.Column) else (
                right if isinstance(right, exp.Column) else None)
            lit_node = right if col_node is left else left
            if col_node is not None and isinstance(lit_node, exp.Literal):
                result[_column_key(col_node)] = (_literal_value(lit_node), lit_node)
    return result


# --------------------------------------------------------------------------
# Schema-aware checks (scope.md section 3, "schema definition to validate
# queries and data" -- offline, user-imported schema; see schema.py).
#
# Every check here is opt-in and silent when the target table is not in
# the imported schema, so a partial/empty schema never produces a false
# positive (scope.md 26: prioritize avoiding false positives).
# --------------------------------------------------------------------------

def _columns_referencing_table(node: exp.Expression, table_alias: Optional[str],
                                table_name: str) -> list[exp.Column]:
    """Return Column nodes in `node` that refer to the statement's target
    table -- either unqualified (single-table UPDATE/DELETE context) or
    qualified with the table's own name/alias.
    """
    wanted = {table_name.upper()}
    if table_alias:
        wanted.add(table_alias.upper())
    out = []
    for c in node.find_all(exp.Column):
        if not c.table:
            out.append(c)  # unqualified -- assume it means the target table
        elif str(c.table).upper() in wanted:
            out.append(c)
    return out


def _schema_column_findings(table_def: "schema_mod.TableDef", columns: list[exp.Column]) -> list[Finding]:
    findings: list[Finding] = []
    seen: set[str] = set()
    for c in columns:
        col_name = c.name
        if not col_name:
            continue
        key = col_name.upper()
        if key in seen:
            continue
        if not table_def.has_column(key):
            seen.add(key)
            _add(findings, _make_finding(
                "SCHEMA-001",
                evidence=f"{table_def.name}.{col_name}",
                confidence=CONFIDENCE_SCHEMA,
                basis="static",
            ))
    return findings


def _schema_pk_predicate_finding(table_def: "schema_mod.TableDef",
                                  where: Optional[exp.Where], table_name: str,
                                  alias: Optional[str]) -> Optional[Finding]:
    if not table_def.primary_key or where is None:
        return None
    where_eq = _where_literal_equalities(where)
    # Also count PK columns compared to a bind/column (not just a literal)
    # as "covered" -- we only want to flag when the PK is absent entirely,
    # not second-guess how it's compared.
    covered = set(where_eq.keys())
    for cond in _flatten_and(where.this):
        if isinstance(cond, exp.EQ):
            for side in (cond.left, cond.right):
                if isinstance(side, exp.Column):
                    covered.add(_column_key(side))

    qualifiers = {table_name.upper()}
    if alias:
        qualifiers.add(alias.upper())

    def _pk_is_covered(pk: str) -> bool:
        if pk in covered:
            return True
        return any(f"{q}.{pk}" in covered for q in qualifiers)

    if any(_pk_is_covered(pk) for pk in table_def.primary_key):
        return None
    return _make_finding(
        "SCHEMA-002",
        evidence=f"{table_def.name} primary key: ({', '.join(table_def.primary_key)})",
        confidence=CONFIDENCE_SCHEMA,
        basis="static",
    )


def _schema_findings_for_dml(target_table: exp.Table, where: Optional[exp.Where],
                              set_assignments: list[exp.EQ]) -> list[Finding]:
    """Shared schema-aware checks for UPDATE/DELETE: unknown-column
    references (SCHEMA-001), a WHERE clause that never mentions the
    table's primary key (SCHEMA-002), and the Oracle-error-catalog type
    checks (ORA-002/003/004) against SET assignments and WHERE equality
    literals.
    """
    table_name = target_table.name
    if not table_name:
        return []
    table_def = schema_mod.get_table(table_name)
    if table_def is None:
        return []  # table not in the imported schema -- stay silent

    findings: list[Finding] = []
    alias = target_table.alias or None

    set_columns = [e.left for e in set_assignments if isinstance(e.left, exp.Column)]

    columns: list[exp.Column] = list(set_columns)
    if where is not None:
        columns.extend(_columns_referencing_table(where.this, alias, table_name))

    findings.extend(_schema_column_findings(table_def, columns))
    _add(findings, _schema_pk_predicate_finding(table_def, where, table_name, alias))

    # ORA-002/003/004: type-aware checks against literal values. SET
    # assignments pair a column directly with its new value.
    for set_expr in set_assignments:
        set_col, set_val = set_expr.left, set_expr.right
        if isinstance(set_col, exp.Column):
            findings.extend(_check_literal_against_column(
                table_def, set_col.name, set_val, DIALECT))

    # ...and WHERE equality predicates pair a column with the literal it
    # is compared against (AND-ed top-level conditions only, same scope
    # as the other WHERE-based checks in this module).
    if where is not None:
        for cond in _flatten_and(where.this):
            if not isinstance(cond, exp.EQ):
                continue
            left, right = cond.left, cond.right
            col_node = left if isinstance(left, exp.Column) else (
                right if isinstance(right, exp.Column) else None)
            val_node = right if col_node is left else left
            if col_node is None:
                continue
            col_table = str(col_node.table).upper() if col_node.table else None
            if col_table is not None and col_table not in {table_name.upper(), (alias or "").upper()}:
                continue
            findings.extend(_check_literal_against_column(
                table_def, col_node.name, val_node, DIALECT))

    return findings


def _check_literal_against_column(table_def: "schema_mod.TableDef", col_name: str,
                                   value_node: exp.Expression, dialect: str) -> list[Finding]:
    """ORA-002/003/004: check one (column, assigned-or-compared value)
    pair against the column's declared type in the imported schema.
    Silent for any column the schema doesn't know about, any value that
    isn't a plain literal/NULL, or any type category we don't model.
    """
    col_def = table_def.columns.get(col_name.upper())
    if col_def is None:
        return []  # unknown column already reported by SCHEMA-001

    findings: list[Finding] = []
    evidence = f"{table_def.name}.{col_def.name} = {value_node.sql(dialect=dialect)}"

    if isinstance(value_node, exp.Null):
        if not col_def.nullable:
            _add(findings, _make_finding(
                "ORA-003",
                evidence=evidence,
                confidence=CONFIDENCE_SCHEMA,
            ))
        return findings

    if not isinstance(value_node, exp.Literal):
        return findings  # expression/bind/column -- cannot safely evaluate offline

    if col_def.type_category == schema_mod.TYPE_CATEGORY_NUMERIC and value_node.is_string:
        try:
            float(value_node.this)
        except (TypeError, ValueError):
            _add(findings, _make_finding(
                "ORA-002",
                evidence=evidence,
                confidence=CONFIDENCE_SCHEMA,
            ))

    if (col_def.type_category == schema_mod.TYPE_CATEGORY_STRING
            and value_node.is_string and col_def.max_length is not None
            and len(str(value_node.this)) > col_def.max_length):
        _add(findings, _make_finding(
            "ORA-004",
            evidence=f"{evidence}  (length {len(str(value_node.this))} > "
                     f"{col_def.max_length})",
            confidence=CONFIDENCE_SCHEMA,
        ))

    return findings


def _check_division_by_zero(stmt: exp.Expression) -> list[Finding]:
    """ORA-001: a literal-zero divisor anywhere in the statement. Only
    fires for a bare numeric-zero literal divisor -- a column, a bind, or
    any other expression could be non-zero at runtime, so those are left
    alone (scope.md 26: prioritize avoiding false positives).
    """
    findings: list[Finding] = []
    for div in stmt.find_all(exp.Div):
        right = div.right
        val = _literal_value(right) if isinstance(right, exp.Literal) else None
        if val is not None and val[0] == "num" and val[1] == 0:
            _add(findings, _make_finding(
                "ORA-001",
                evidence=div.sql(dialect=DIALECT),
                confidence=CONFIDENCE_PROVEN,
            ))
    return findings


# --------------------------------------------------------------------------
# Join analyzer (scope.md section 13)
#
# MVP scope: purely structural. For each table referenced in FROM/JOIN,
# check whether *some* equality predicate (in a JOIN's ON clause, or
# anywhere in the WHERE clause) mentions that table together with another
# table in the same query. A table with no such predicate is flagged as a
# possible Cartesian join risk. This is a per-table connectivity check
# (union-find over table aliases), not a full per-pair validation -- it
# deliberately favors false negatives over false positives (scope.md 26:
# "The analyzer should prioritize avoiding false positives").
#
# Explicit CROSS JOIN / NATURAL JOIN are treated as declared intent and
# are never flagged. Subqueries/derived tables in FROM are out of scope
# for this MVP version of the join analyzer (scope.md 3, "Planned
# expansion": subquery analysis) -- the whole check is skipped rather than
# guessing at a subquery's own table structure.
# --------------------------------------------------------------------------

class _UnionFind:
    def __init__(self, items: list[str]):
        self.parent = {item: item for item in items}

    def find(self, x: str) -> str:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


def _table_ref(node: exp.Expression) -> Optional[str]:
    """Return an uppercased alias-or-name for a FROM/JOIN table entry, or
    None if it isn't a plain table (subquery, function, lateral, etc.).
    """
    if isinstance(node, exp.Table):
        name = node.alias_or_name
        return str(name).upper() if name else None
    return None


def _union_columns_in_predicate(node: exp.Expression, uf: "_UnionFind",
                                 known: set[str]) -> None:
    """Walk an expression tree and union any two distinct known table
    aliases that appear together in an equality comparison anywhere
    inside it (covers WHERE clauses regardless of AND/OR nesting, and
    JOIN ON clauses).
    """
    for eq in node.find_all(exp.EQ):
        cols = [c for c in (eq.left, eq.right) if isinstance(c, exp.Column) and c.table]
        tables = {str(c.table).upper() for c in cols}
        tables &= known
        if len(tables) == 2:
            a, b = tuple(tables)
            uf.union(a, b)


def _analyze_joins(stmt: exp.Select) -> list[Finding]:
    findings: list[Finding] = []

    from_clause = stmt.args.get("from")
    if from_clause is None:
        return findings
    base_table = from_clause.this

    joins = stmt.args.get("joins") or []

    # Bail out entirely (no findings, no guessing) if any FROM/JOIN entry
    # isn't a plain table -- subqueries, lateral views, etc. are out of
    # scope for this structural-only MVP check.
    all_nodes = [base_table] + [j.this for j in joins]
    refs = [_table_ref(n) for n in all_nodes]
    if any(r is None for r in refs) or len(refs) < 2:
        return findings
    if len(set(refs)) != len(refs):
        # Duplicate/self-join aliasing -- ambiguous for this simple
        # connectivity check, skip rather than risk a false positive.
        return findings

    known = set(refs)
    uf = _UnionFind(list(known))

    # Tables that declared an explicit cross join are exempt from the
    # connectivity requirement (declared intent, not a mistake).
    exempt: set[str] = set()

    for j, ref in zip(joins, refs[1:]):
        kind = (j.args.get("kind") or "").upper()
        on = j.args.get("on")
        if kind in ("CROSS", "NATURAL"):
            exempt.add(ref)
            continue
        if on is not None:
            _union_columns_in_predicate(on, uf, known)

    where = stmt.args.get("where")
    if where is not None:
        _union_columns_in_predicate(where.this, uf, known)

    base_ref = refs[0]
    root = uf.find(base_ref)
    disconnected = [r for r in known if r not in exempt and uf.find(r) != root]

    # If the base table itself ended up isolated (e.g. every other table
    # is exempt/cross-joined), nothing to report -- there is no "rest of
    # the query" for it to be missing a predicate against.
    if not disconnected:
        return findings

    for ref in disconnected:
        _add(findings, _make_finding(
            "JOIN-001",
            evidence=f"Table '{ref}' has no join predicate connecting it "
                     f"to the rest of the FROM/JOIN list",
            confidence=CONFIDENCE_PROVEN,
        ))

    return findings


# --------------------------------------------------------------------------
# Statement-level analyzers
# --------------------------------------------------------------------------

def _analyze_update(stmt: exp.Update) -> list[Finding]:
    findings: list[Finding] = []
    where = stmt.args.get("where")

    if where is None:
        _add(findings, _make_finding(
            "DML-001",
            evidence=stmt.sql(dialect=DIALECT).split("SET")[0].strip(),
            confidence=CONFIDENCE_PROVEN,
        ))
        where_eq: dict[str, tuple] = {}
    else:
        findings.extend(_analyze_where_predicates(where))
        where_eq = _where_literal_equalities(where)

    # SET-clause reasoning. Runs regardless of whether a WHERE clause is
    # present -- self-assignment and function-wrapped-self-reference are
    # properties of the assignment alone (scope.md 4.1).
    for set_expr in stmt.expressions:
        if not isinstance(set_expr, exp.EQ):
            continue
        set_col = set_expr.left
        set_val = set_expr.right
        if not isinstance(set_col, exp.Column):
            continue
        col_key = _column_key(set_col)

        # 1) Unconditional self-assignment: SET col = col (no transformation).
        #    This is a no-op for every matched row, independent of WHERE and
        #    of the underlying data -- so it is PROVEN, not just POSSIBLE.
        if isinstance(set_val, exp.Column) and _column_key(set_val) == col_key:
            _add(findings, _make_finding(
                "DML-SET-SELF-001",
                evidence=f"SET {set_col.sql(dialect=DIALECT)} = {set_val.sql(dialect=DIALECT)}",
                confidence=CONFIDENCE_PROVEN,
            ))
            continue

        # 2) Function-wrapped self reference: SET col = FUNC(..., col, ...),
        #    e.g. NVL(salary, 0), UPPER(status), TRUNC(date_col). Whether
        #    this changes the value depends on the data, so it is only
        #    ever POSSIBLE offline (scope.md 4.1: "distinguish literal
        #    equality from expressions it cannot safely prove").
        if isinstance(set_val, exp.Func):
            wrapped_cols = set_val.find_all(exp.Column)
            if any(_column_key(c) == col_key for c in wrapped_cols):
                _add(findings, _make_finding(
                    "DML-SET-FUNC-001",
                    evidence=f"SET {set_col.sql(dialect=DIALECT)} = {set_val.sql(dialect=DIALECT)}",
                    confidence=CONFIDENCE_POSSIBLE,
                    basis="database",
                ))
            continue

        # 3) SET/WHERE no-op: SET col = <literal> where WHERE col = <same literal>
        if col_key in where_eq and isinstance(set_val, exp.Literal):
            where_val, where_lit_node = where_eq[col_key]
            set_val_v = _literal_value(set_val)
            if set_val_v is not None and set_val_v == where_val:
                evidence = (f"SET {set_col.sql(dialect=DIALECT)} = "
                            f"{_literal_display(set_val)}  "
                            f"WHERE {set_col.sql(dialect=DIALECT)} = "
                            f"{_literal_display(where_lit_node)}")
                _add(findings, _make_finding(
                    "DML-SET-WHERE-001",
                    evidence=evidence,
                    confidence=CONFIDENCE_POSSIBLE,
                ))

    # Offline schema-aware checks (scope.md 3: "schema definition to
    # validate queries and data"). Silent no-op if the target table was
    # never imported into config/schema.json.
    target_table = stmt.this
    if isinstance(target_table, exp.Table):
        set_nodes = [e for e in stmt.expressions if isinstance(e, exp.EQ)]
        findings.extend(_schema_findings_for_dml(target_table, where, set_nodes))

    return findings


def _analyze_delete(stmt: exp.Delete) -> list[Finding]:
    findings: list[Finding] = []
    where = stmt.args.get("where")
    if where is None:
        head = stmt.sql(dialect=DIALECT)
        _add(findings, _make_finding(
            "DML-002",
            evidence=head,
            confidence=CONFIDENCE_PROVEN,
        ))
    else:
        findings.extend(_analyze_where_predicates(where))

    target_table = stmt.this
    if isinstance(target_table, exp.Table):
        findings.extend(_schema_findings_for_dml(target_table, where, []))

    return findings


def _analyze_cte_usage(stmt: exp.Select) -> list[Finding]:
    """CTE-001: a WITH-clause subquery (scope.md 3, "CTE analysis") that is
    never referenced anywhere else in the statement.

    Safety: this only ever fires on a *zero* occurrence count of the CTE's
    name as a table reference anywhere in the whole statement tree
    (including inside other CTE bodies, which supports a later CTE
    referencing an earlier one, and inside the CTE's own body, which
    supports Oracle's recursive subquery-factoring self-reference). A
    zero-occurrence result is unambiguous -- there is no reading of the
    SQL under which the name is used -- so this cannot false-positive on
    a real usage the way a heuristic count-based check could.
    """
    with_clause = stmt.args.get("with")
    if with_clause is None:
        return []
    ctes = with_clause.expressions
    if not ctes:
        return []

    cte_aliases: dict[str, str] = {}
    for cte in ctes:
        alias = cte.alias
        if alias:
            cte_aliases[str(alias).upper()] = str(alias)

    used_names = {str(t.name).upper() for t in stmt.find_all(exp.Table) if t.name}

    findings: list[Finding] = []
    for key, alias in cte_aliases.items():
        if key not in used_names:
            _add(findings, _make_finding(
                "CTE-001",
                evidence=f"WITH {alias} AS (...) is never referenced",
                confidence=CONFIDENCE_PROVEN,
            ))
    return findings


_SCALAR_COMPARISONS = (exp.EQ, exp.NEQ, exp.GT, exp.LT, exp.GTE, exp.LTE)
_AGG_FUNCS = (exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max)


def _select_expr_is_aggregate(e: exp.Expression) -> bool:
    node = e.this if isinstance(e, exp.Alias) else e
    return isinstance(node, _AGG_FUNCS)


def _has_rownum_single_row_guard(where: Optional[exp.Where]) -> bool:
    """True if WHERE contains ROWNUM = 1 or ROWNUM <= 1 anywhere -- Oracle's
    standard idiom for capping a query to exactly one row.
    """
    if where is None:
        return False
    for node in where.find_all(exp.EQ, exp.LTE):
        left = node.this
        right = node.expression
        if isinstance(left, exp.Column) and str(left.name).upper() == "ROWNUM":
            val = _literal_value(right) if isinstance(right, exp.Literal) else None
            if val is not None and val[0] == "num" and val[1] == 1:
                return True
    return False


def _subquery_is_provably_single_row(select: exp.Select) -> bool:
    """True only when the subquery is guaranteed to return at most one row
    by its own structure alone -- conservative by design (scope.md 26):
    a subquery that isn't caught by any of these guards is not thereby
    proven multi-row, only unproven single-row, which is exactly the
    "POSSIBLE" wording ORA-006 uses.
    """
    if not isinstance(select, exp.Select):
        return True  # not a plain SELECT (e.g. a set operation) -- skip, don't guess
    exprs = select.expressions
    if exprs and not select.args.get("group") and all(
        _select_expr_is_aggregate(e) for e in exprs
    ):
        return True  # a bare aggregate with no GROUP BY always returns exactly one row
    limit = select.args.get("limit")
    if isinstance(limit, exp.Fetch):
        val = _literal_value(limit.args.get("count")) if isinstance(
            limit.args.get("count"), exp.Literal) else None
        if val is not None and val[0] == "num" and val[1] == 1:
            return True
    elif isinstance(limit, exp.Limit):
        val = _literal_value(limit.expression) if isinstance(
            limit.expression, exp.Literal) else None
        if val is not None and val[0] == "num" and val[1] == 1:
            return True
    if _has_rownum_single_row_guard(select.args.get("where")):
        return True
    return False


def _check_scalar_subquery_multirow(stmt: exp.Expression) -> list[Finding]:
    """ORA-006: a subquery used where Oracle requires at most one row (a
    plain scalar comparison: =, <>, >, <, >=, <=) with no structural
    guarantee that it returns only one. If it returns more than one row at
    runtime, Oracle raises ORA-01427.

    Scope: only the comparison-operator case is checked, not every place a
    scalar subquery can appear (e.g. a bare subquery in the SELECT list) --
    the comparison case is the one sqlglot's AST makes unambiguous to spot,
    and IN/ANY/ALL/EXISTS/FROM-clause subqueries (all legitimately
    multi-row) are excluded by construction since they never appear as the
    direct operand of one of these comparison node types.
    """
    findings: list[Finding] = []
    seen: set[int] = set()
    for cmp_node in stmt.find_all(*_SCALAR_COMPARISONS):
        for operand in (cmp_node.left, cmp_node.right):
            if not isinstance(operand, exp.Subquery):
                continue
            inner = operand.this
            if id(inner) in seen:
                continue
            if _subquery_is_provably_single_row(inner):
                continue
            seen.add(id(inner))
            _add(findings, _make_finding(
                "ORA-006",
                evidence=cmp_node.sql(dialect=DIALECT)[:160],
                confidence=CONFIDENCE_POSSIBLE,
                basis="database",
            ))
    return findings


# --------------------------------------------------------------------------
# CASE expression analysis (scope.md section 3, "CASE analysis")
# --------------------------------------------------------------------------

def _analyze_case_expressions(stmt: exp.Expression) -> list[Finding]:
    """CASE-001: no ELSE branch -- any row matching none of the WHEN
    conditions silently evaluates to NULL, which is easy to miss.

    CASE-002: the same WHEN value/condition appears more than once. The
    second (and any later) occurrence can never be reached, since the
    first matching WHEN always wins. Only checked for the two shapes that
    are unambiguous to compare -- a simple CASE's WHEN literal, or a
    searched CASE's WHEN <column> = <literal> -- any other WHEN shape
    (an expression, a range, OR-connected conditions, ...) makes the
    whole check bail out for that CASE rather than risk a false positive
    from comparing conditions it cannot safely prove distinct or equal.
    """
    findings: list[Finding] = []
    for case in stmt.find_all(exp.Case):
        ifs = case.args.get("ifs") or []
        if not ifs:
            continue

        if case.args.get("default") is None:
            _add(findings, _make_finding(
                "CASE-001",
                evidence=case.sql(dialect=DIALECT)[:160],
                confidence=CONFIDENCE_PROVEN,
            ))

        operand = case.this  # simple CASE's operand, or None for a searched CASE
        keys = []
        ambiguous = False
        for branch in ifs:
            cond = branch.this
            if operand is not None:
                # Simple CASE: "CASE dept WHEN 10 THEN ..."
                val = _literal_value(cond) if isinstance(cond, exp.Literal) else None
                if val is None:
                    ambiguous = True
                    break
                keys.append(val)
            else:
                # Searched CASE: "CASE WHEN col = 10 THEN ..."
                if not isinstance(cond, exp.EQ):
                    ambiguous = True
                    break
                left, right = cond.left, cond.right
                col_node = left if isinstance(left, exp.Column) else (
                    right if isinstance(right, exp.Column) else None)
                lit_node = right if col_node is left else left
                if col_node is None or not isinstance(lit_node, exp.Literal):
                    ambiguous = True
                    break
                keys.append((_column_key(col_node), _literal_value(lit_node)))

        if not ambiguous and keys and len(set(keys)) != len(keys):
            _add(findings, _make_finding(
                "CASE-002",
                evidence=case.sql(dialect=DIALECT)[:160],
                confidence=CONFIDENCE_PROVEN,
            ))

    return findings


# --------------------------------------------------------------------------
# Analytic/window function checks (scope.md section 3,
# "analytic/window function checks")
# --------------------------------------------------------------------------

_ORDER_REQUIRED_WINDOW_FUNCS = (exp.Rank, exp.DenseRank, exp.Ntile)


def _windows_in_own_scope(node: exp.Expression) -> list[exp.Window]:
    """Window nodes reachable from `node` without crossing into a nested
    subquery's own SELECT -- a window function inside a subquery's SELECT
    list is legal there even if the subquery itself sits inside this
    query's WHERE/HAVING/GROUP BY, so descent stops at that boundary
    instead of flagging it too.
    """
    return [n for n in node.dfs(prune=lambda x: isinstance(x, exp.Select))
            if isinstance(n, exp.Window)]


def _check_window_functions(stmt: exp.Expression) -> list[Finding]:
    """ORA-007: an analytic/window function used directly in this query
    block's own WHERE, HAVING, or GROUP BY (ORA-30483) -- Oracle only
    allows an analytic function in the SELECT list or ORDER BY of the
    query block that computes it; filtering on its result requires
    wrapping the query in a subquery first.

    ORA-008: RANK/DENSE_RANK/NTILE with no ORDER BY in its OVER() clause
    (ORA-30485) -- these functions are only meaningful relative to an
    ordering, and Oracle rejects the statement outright without one.
    """
    findings: list[Finding] = []

    for clause_cls in (exp.Where, exp.Having, exp.Group):
        for clause in stmt.find_all(clause_cls):
            for win in _windows_in_own_scope(clause):
                _add(findings, _make_finding(
                    "ORA-007",
                    evidence=win.sql(dialect=DIALECT)[:160],
                    confidence=CONFIDENCE_PROVEN,
                ))

    for win in stmt.find_all(exp.Window):
        if (isinstance(win.this, _ORDER_REQUIRED_WINDOW_FUNCS)
                and win.args.get("order") is None):
            _add(findings, _make_finding(
                "ORA-008",
                evidence=win.sql(dialect=DIALECT)[:160],
                confidence=CONFIDENCE_PROVEN,
            ))

    return findings


# --------------------------------------------------------------------------
# Correlated-subquery labeling (scope.md sections 3 and 12: "subquery
# analysis", "correlated subqueries")
#
# A correlated subquery references a column from an enclosing query's own
# FROM/JOIN list -- it is re-evaluated once per outer row, which is worth
# knowing (not inherently wrong) for anyone reasoning about performance.
# This is bounded to exactly one level: a subquery is only checked against
# its *immediate* enclosing SELECT's table aliases, not any grandparent
# scope. A subquery that correlates to a grandparent scope while skipping
# its immediate parent is real, legal Oracle SQL that this check will
# simply miss -- a false negative, never a false positive (scope.md 26).
# --------------------------------------------------------------------------

def _table_aliases_of_select(select: exp.Select) -> set[str]:
    """Uppercased aliases/names of tables directly in this SELECT's own
    FROM/JOIN list. A derived table's own alias is visible in this scope;
    whatever is *inside* that derived table is a different scope.
    """
    aliases: set[str] = set()
    nodes: list[exp.Expression] = []
    from_clause = select.args.get("from")
    if from_clause is not None:
        nodes.append(from_clause.this)
    for j in select.args.get("joins") or []:
        nodes.append(j.this)
    for n in nodes:
        if isinstance(n, exp.Table):
            name = n.alias_or_name
            if name:
                aliases.add(str(name).upper())
        elif isinstance(n, exp.Subquery) and n.alias:
            aliases.add(str(n.alias).upper())
    return aliases


def _nodes_in_own_scope(node: exp.Expression) -> list[exp.Expression]:
    """Every node reachable from `node` without crossing into a nested
    subquery's own SELECT scope (see _windows_in_own_scope for the same
    pattern applied to window functions).

    `node` itself is never a Select here -- see _select_child_sources()
    below, which is always used to get the starting points for this walk.
    Calling this directly on a Select root would immediately prune at the
    root itself and yield nothing.
    """
    return list(node.dfs(prune=lambda x: isinstance(x, exp.Select)))


def _select_child_sources(select: exp.Select) -> list[exp.Expression]:
    """The direct child expressions of a SELECT's own projection / WHERE /
    GROUP BY / HAVING / JOIN-ON -- the starting points for a scope-bounded
    walk of that SELECT's own body via _nodes_in_own_scope(). Deliberately
    excludes the FROM clause: a FROM-clause derived table cannot correlate
    back into the same FROM clause in Oracle, so it is never a source of
    correlation for this SELECT (though its own alias is still counted by
    _table_aliases_of_select for the *next* level down).
    """
    sources: list[exp.Expression] = []
    for key in ("expressions", "where", "group", "having"):
        val = select.args.get(key)
        if val is None:
            continue
        sources.extend(val if isinstance(val, list) else [val])
    for j in select.args.get("joins") or []:
        on = j.args.get("on")
        if on is not None:
            sources.append(on)
    return sources


def _direct_subqueries_of_select(select: exp.Select) -> list[exp.Select]:
    """The SELECT bodies of subqueries appearing directly in this SELECT's
    own body (see _select_child_sources) -- not subqueries nested a
    further level down (each nesting level is handled by its own turn
    through the outer stmt.find_all(exp.Select) loop in
    _analyze_subquery_correlation).
    """
    result: list[exp.Select] = []
    for src in _select_child_sources(select):
        for n in _nodes_in_own_scope(src):
            # A subquery's inner query is exp.Select either way: wrapped in
            # exp.Subquery for =/IN/ANY/ALL/FROM-clause forms, or bare
            # under EXISTS/NOT EXISTS. Both end up as the exp.Select node
            # itself once _nodes_in_own_scope's Select-boundary prune is
            # reached, so checking for exp.Select alone covers both.
            if isinstance(n, exp.Select):
                result.append(n)
    return result


def _is_correlated_subquery(sub_select: exp.Select, outer_aliases: set[str]) -> bool:
    own_aliases = _table_aliases_of_select(sub_select)
    for src in _select_child_sources(sub_select):
        for n in _nodes_in_own_scope(src):
            if not isinstance(n, exp.Column) or not n.table:
                continue
            tbl = str(n.table).upper()
            if tbl in own_aliases:
                continue
            if tbl in outer_aliases:
                return True
    return False


def _analyze_subquery_correlation(stmt: exp.Expression) -> list[Finding]:
    findings: list[Finding] = []

    for select in stmt.find_all(exp.Select):
        outer_aliases = _table_aliases_of_select(select)
        if not outer_aliases:
            continue
        for sub_select in _direct_subqueries_of_select(select):
            if _is_correlated_subquery(sub_select, outer_aliases):
                _add(findings, _make_finding(
                    "SUBQ-001",
                    evidence=sub_select.sql(dialect=DIALECT)[:160],
                    confidence=CONFIDENCE_PROVEN,
                ))

    # UPDATE/DELETE aren't exp.Select, but a subquery in their own WHERE
    # (or, for UPDATE, SET) can correlate back to the single target table
    # exactly the same way -- so they get their own turn as an outer scope.
    for dml in stmt.find_all(exp.Update, exp.Delete):
        target = dml.this
        if not isinstance(target, exp.Table) or not target.name:
            continue
        outer_aliases = {str(target.alias_or_name).upper()}
        sources: list[exp.Expression] = []
        where = dml.args.get("where")
        if where is not None:
            sources.append(where)
        if isinstance(dml, exp.Update):
            sources.extend(dml.expressions)
        for src in sources:
            for n in _nodes_in_own_scope(src):
                if isinstance(n, exp.Select) and _is_correlated_subquery(n, outer_aliases):
                    _add(findings, _make_finding(
                        "SUBQ-001",
                        evidence=n.sql(dialect=DIALECT)[:160],
                        confidence=CONFIDENCE_PROVEN,
                    ))

    return findings


# --------------------------------------------------------------------------
# Bind-variable analysis (scope.md section 3, "bind-variable analysis")
#
# Only a named Oracle bind (:name) is checked -- SQLGlot's Oracle dialect
# does not parse the numbered form (:1, :2), so that shape never reaches
# this analyzer at all (it surfaces as PARSE-001 instead). Reuses the
# offline schema catalog (schema.py) rather than duplicating type logic:
# only fires when the SAME bind name is compared against columns of two
# different imported, known type categories -- reusing one bind name
# across genuinely different value types is a common copy/paste bug, and
# this is silent whenever either column isn't covered by the imported
# schema, so it never guesses.
# --------------------------------------------------------------------------

def _collect_bind_column_pairs(stmt: exp.Expression) -> list[tuple[str, exp.Column]]:
    pairs: list[tuple[str, exp.Column]] = []
    for cmp_node in stmt.find_all(*_SCALAR_COMPARISONS):
        left, right = cmp_node.left, cmp_node.right
        bind, col = None, None
        if isinstance(left, exp.Placeholder) and isinstance(right, exp.Column):
            bind, col = left, right
        elif isinstance(right, exp.Placeholder) and isinstance(left, exp.Column):
            bind, col = right, left
        if bind is not None and bind.this:
            pairs.append((str(bind.this).upper(), col))
    return pairs


def _build_alias_table_map(stmt: exp.Expression) -> dict[str, Optional[str]]:
    """Map each alias/table-name (uppercased) appearing in any FROM/JOIN
    (any SELECT scope) or DML target table anywhere in the statement to
    the real, uppercased table name it refers to -- a column's `.table`
    is normally the alias, not the schema's table name, so this is
    needed before a schema lookup can succeed at all. If the same alias
    resolves to two different real tables in different scopes, it maps
    to None (ambiguous) rather than guessing which one a given use means.
    """
    mapping: dict[str, Optional[str]] = {}

    def record(alias: str, real: str) -> None:
        if alias in mapping and mapping[alias] != real:
            mapping[alias] = None
        elif alias not in mapping:
            mapping[alias] = real

    for select in stmt.find_all(exp.Select):
        nodes = []
        from_clause = select.args.get("from")
        if from_clause is not None:
            nodes.append(from_clause.this)
        for j in select.args.get("joins") or []:
            nodes.append(j.this)
        for n in nodes:
            if isinstance(n, exp.Table) and n.name:
                record(str(n.alias_or_name).upper(), str(n.name).upper())

    for node in stmt.find_all(exp.Update, exp.Delete):
        target = node.this
        if isinstance(target, exp.Table) and target.name:
            record(str(target.alias_or_name).upper(), str(target.name).upper())

    return mapping


def _check_bind_variable_type_consistency(stmt: exp.Expression) -> list[Finding]:
    findings: list[Finding] = []
    by_bind: dict[str, list[exp.Column]] = {}
    for name, col in _collect_bind_column_pairs(stmt):
        by_bind.setdefault(name, []).append(col)
    if not by_bind:
        return findings

    alias_map = _build_alias_table_map(stmt)

    for name, cols in by_bind.items():
        categories_seen: dict[str, str] = {}  # type_category -> "TABLE.COLUMN" example
        for col in cols:
            alias = str(col.table).upper() if col.table else None
            if not alias:
                continue  # unqualified -- can't safely resolve which table, skip
            table_name = alias_map.get(alias)
            if not table_name:
                continue  # unknown or ambiguous alias -- skip rather than guess
            table_def = schema_mod.get_table(table_name)
            if table_def is None:
                continue
            col_def = table_def.columns.get(str(col.name).upper())
            if col_def is None or col_def.type_category == schema_mod.TYPE_CATEGORY_OTHER:
                continue
            categories_seen.setdefault(col_def.type_category, f"{table_def.name}.{col_def.name}")

        if len(categories_seen) > 1:
            evidence = f":{name} compared against " + ", ".join(
                f"{ref} ({cat})" for cat, ref in categories_seen.items())
            _add(findings, _make_finding(
                "BIND-001",
                evidence=evidence,
                confidence=CONFIDENCE_SCHEMA,
            ))
    return findings


def _analyze_select(stmt: exp.Select) -> list[Finding]:
    findings: list[Finding] = []
    for _star in stmt.find_all(exp.Star):
        _add(findings, _make_finding(
            "PERF-001",
            evidence="SELECT *",
            confidence=CONFIDENCE_PROVEN,
        ))
        break  # one finding per statement is enough
    where = stmt.args.get("where")
    if where is not None:
        findings.extend(_analyze_where_predicates(where))
    findings.extend(_analyze_joins(stmt))
    findings.extend(_analyze_cte_usage(stmt))
    return findings


# --------------------------------------------------------------------------
# Set-operation analyzer (UNION / UNION ALL / INTERSECT / MINUS)
# (scope.md section 3, "UNION/INTERSECT/MINUS analysis")
# --------------------------------------------------------------------------

def _flatten_setop_branches(node: exp.Expression) -> list[exp.Expression]:
    """Recursively flatten a (possibly nested) set-operation tree into its
    leaf query blocks, e.g. (A UNION B) UNION ALL C -> [A, B, C].
    """
    if isinstance(node, exp.SetOperation):
        return _flatten_setop_branches(node.this) + _flatten_setop_branches(node.expression)
    return [node]


def _analyze_setop(stmt: exp.SetOperation) -> list[Finding]:
    findings: list[Finding] = []
    branches = _flatten_setop_branches(stmt)

    # Run the ordinary SELECT-level checks against every leaf branch, so a
    # UNION/INTERSECT/MINUS query still gets PERF-001/JOIN-001/WHERE
    # coverage per branch instead of being treated as one opaque statement.
    for branch in branches:
        if isinstance(branch, exp.Select):
            findings.extend(_analyze_select(branch))

    # A WITH clause on a set-operation query attaches to the top-level
    # SetOperation node, not to either branch, so CTE-001 is checked here
    # rather than inside the per-branch _analyze_select() call above.
    findings.extend(_analyze_cte_usage(stmt))

    # ORA-005: mismatched column counts across branches (ORA-01789). Only
    # checked when every branch is a plain SELECT with an explicit column
    # list -- a SELECT * branch makes the count unknowable offline, so the
    # whole check is skipped rather than guessing (scope.md 26).
    select_branches = [b for b in branches if isinstance(b, exp.Select)]
    if (len(select_branches) == len(branches) and len(select_branches) > 1
            and not any(any(isinstance(e, exp.Star) for e in b.expressions)
                        for b in select_branches)):
        counts = [len(b.expressions) for b in select_branches]
        if len(set(counts)) > 1:
            evidence = " / ".join(str(c) for c in counts)
            _add(findings, _make_finding(
                "ORA-005",
                evidence=f"SELECT column counts across branches: {evidence}",
                confidence=CONFIDENCE_PROVEN,
            ))

    return findings


def _insert_target_table(stmt: exp.Insert) -> Optional[exp.Table]:
    node = stmt.this
    if isinstance(node, exp.Schema):
        node = node.this
    return node if isinstance(node, exp.Table) else None


def _insert_column_names(stmt: exp.Insert, table_def: "schema_mod.TableDef") -> Optional[list[str]]:
    """Return the ordered list of column names each VALUES position maps
    to: the explicit column list if the statement gave one, otherwise the
    imported schema's column order -- but only when we can be sure of
    that mapping (an explicit list, or a schema whose column count
    matches every VALUES row). Returns None when the mapping is
    ambiguous, so callers skip rather than guess.
    """
    node = stmt.this
    if isinstance(node, exp.Schema):
        return [str(c.this if isinstance(c, exp.Identifier) else c) for c in node.expressions]
    return list(table_def.columns.keys())  # schema's own declared order


def _schema_findings_for_insert(stmt: exp.Insert) -> list[Finding]:
    target_table = _insert_target_table(stmt)
    if target_table is None or not target_table.name:
        return []
    table_def = schema_mod.get_table(target_table.name)
    if table_def is None:
        return []  # table not in the imported schema -- stay silent

    values_node = stmt.args.get("expression")
    if not isinstance(values_node, exp.Values):
        return []  # INSERT ... SELECT / other forms -- out of scope here

    col_names = _insert_column_names(stmt, table_def)
    if col_names is None:
        return []

    findings: list[Finding] = []
    seen_unknown: set[str] = set()

    for row in values_node.expressions:
        if not isinstance(row, exp.Tuple):
            continue
        values = row.expressions
        if len(values) != len(col_names):
            continue  # column-count mismatch -- ambiguous, skip this row

        for col_name, value_node in zip(col_names, values):
            key = col_name.upper()
            if not table_def.has_column(key):
                if key not in seen_unknown:
                    seen_unknown.add(key)
                    _add(findings, _make_finding(
                        "SCHEMA-001",
                        evidence=f"{table_def.name}.{col_name}",
                        confidence=CONFIDENCE_SCHEMA,
                    ))
                continue
            findings.extend(_check_literal_against_column(
                table_def, col_name, value_node, DIALECT))

    return findings


def _analyze_insert(stmt: exp.Insert) -> list[Finding]:
    findings = _only(_make_finding(
        "DML-003",
        evidence=stmt.sql(dialect=DIALECT)[:120],
        confidence=CONFIDENCE_UNKNOWN,
        basis="database",
    ))
    findings.extend(_schema_findings_for_insert(stmt))
    return findings


def _analyze_merge(stmt: exp.Merge) -> list[Finding]:
    return _only(_make_finding(
        "DML-004",
        evidence=stmt.sql(dialect=DIALECT)[:120],
        confidence=CONFIDENCE_UNKNOWN,
        basis="database",
    ))


_STATEMENT_HANDLERS = {
    exp.Update: ("UPDATE", _analyze_update),
    exp.Delete: ("DELETE", _analyze_delete),
    exp.Select: ("SELECT", _analyze_select),
    exp.Insert: ("INSERT", _analyze_insert),
    exp.Merge: ("MERGE", _analyze_merge),
    # exp.Union / exp.Except / exp.Intersect all subclass exp.SetOperation.
    # Oracle spells exp.Except as MINUS -- _statement_type_name below maps
    # it back to that name for the report.
    exp.SetOperation: ("UNION", _analyze_setop),
}

_SETOP_DISPLAY_NAMES = {
    exp.Union: "UNION",
    exp.Except: "MINUS",
    exp.Intersect: "INTERSECT",
}


def _statement_type_name(stmt: exp.Expression) -> str:
    if isinstance(stmt, exp.SetOperation):
        for cls, name in _SETOP_DISPLAY_NAMES.items():
            if isinstance(stmt, cls):
                return name + (" ALL" if stmt.args.get("distinct") is False else "")
        return "SET-OPERATION"
    for cls, (name, _handler) in _STATEMENT_HANDLERS.items():
        if isinstance(stmt, cls):
            return name
    return type(stmt).__name__.upper()


_EXEC_IMMEDIATE_RE = re.compile(r"EXECUTE\s+IMMEDIATE\b", re.IGNORECASE)
_CALL_STMT_RE = re.compile(r"^\s*CALL\b", re.IGNORECASE)


def _dynsql_findings_in_text(text: str) -> list[Finding]:
    """DYNSQL-001/002 (scope.md 3, "dynamic SQL warnings"): find every
    EXECUTE IMMEDIATE occurrence in `text` -- there can be several inside
    one PL/SQL body -- and, for each one, an UNKNOWN-confidence note that
    its SQL text is built/supplied at runtime and cannot be checked
    offline, plus a PROVEN-confidence note when it concatenates that text
    with || rather than using a single literal string (a common
    SQL-injection-risk pattern when any concatenated piece comes from
    unvalidated input). The span checked for || runs from EXECUTE
    IMMEDIATE to the next top-level semicolon (quote/comment-aware via
    splitter.scan_to_char), so multi-line concatenation is still caught.
    """
    findings: list[Finding] = []
    for m in _EXEC_IMMEDIATE_RE.finditer(text):
        end, _found = splitter_mod.scan_to_char(text, m.start(), ";")
        span = text[m.start():end].strip()
        evidence = span[:160] + ("..." if len(span) > 160 else "")
        _add(findings, _make_finding(
            "DYNSQL-001", evidence=evidence, confidence=CONFIDENCE_UNKNOWN, basis="database",
        ))
        if "||" in span:
            _add(findings, _make_finding(
                "DYNSQL-002", evidence=evidence, confidence=CONFIDENCE_PROVEN,
            ))
    return findings


def _analyze_command(stmt: exp.Command, raw_sql: str) -> tuple[list[Finding], str]:
    """A statement SQLGlot recognized only well enough to fall back to
    its generic exp.Command node -- EXECUTE IMMEDIATE and CALL both land
    here under the installed SQLGlot version. Each gets its own honest,
    specific finding instead of the generic "not yet analyzed" PARSE-002.
    """
    dynsql = _dynsql_findings_in_text(raw_sql)
    if dynsql:
        return dynsql, "EXECUTE IMMEDIATE"
    if _CALL_STMT_RE.match(raw_sql):
        return _only(_make_finding(
            "CALL-001", evidence=raw_sql[:160], confidence=CONFIDENCE_UNKNOWN, basis="database",
        )), "CALL"
    return _only(_make_finding(
        "PARSE-002",
        evidence=str(stmt.this) if stmt.this else "COMMAND",
        confidence=CONFIDENCE_UNKNOWN,
    )), "COMMAND"


def _build_plsql_result(index: int, chunk: "splitter_mod.Chunk") -> StatementResult:
    """PLSQL-001 (scope.md 3, "PL/SQL block parsing"): an anonymous
    DECLARE/BEGIN block or a CREATE PROCEDURE/FUNCTION/PACKAGE/TRIGGER
    body -- recognized structurally by the splitter, but never parsed as
    SQL (SQLGlot parses SQL, not PL/SQL), so no rule in this catalog
    checks its contents. Still scanned as plain text for EXECUTE
    IMMEDIATE occurrences (DYNSQL-001/002), since dynamic SQL is most
    often written inside a procedure body rather than as its own
    top-level statement.
    """
    kind = splitter_mod.plsql_statement_kind(chunk.sql)
    evidence = chunk.sql[:160] + ("..." if len(chunk.sql) > 160 else "")
    recommendation_override = None
    if not chunk.had_slash_terminator:
        recommendation_override = (
            "No '/' terminator was found before the end of the script, so "
            "everything after this point was treated as part of this one "
            "block. If that isn't the actual intent, add a '/' on its own "
            "line where this block should end."
        )
    findings = _only(_make_finding(
        "PLSQL-001", evidence=evidence, confidence=CONFIDENCE_UNKNOWN, basis="database",
        recommendation_override=recommendation_override,
    ))
    findings.extend(_dynsql_findings_in_text(chunk.sql))
    return StatementResult(
        index=index,
        statement_type=kind,
        sql=chunk.sql,
        formatted_sql=None,
        findings=findings,
    )


def analyze_sql(sql_text: str) -> list[StatementResult]:
    """Analyze one or more SQL statements/script (scope.md 3,
    "multi-statement scripts").

    Returns a list of StatementResult, one per statement, in the order
    they appear in sql_text. The script is split into statements by
    splitter.split_statements() BEFORE any SQL parsing is attempted --
    this both keeps a PL/SQL block's own body intact as one unit (see
    splitter.py's module docstring for why SQLGlot's own splitting
    cannot be trusted for that) and means one bad statement in a script
    no longer discards every other, otherwise-analyzable statement in
    the same script the way a single sqlglot.parse() call over the whole
    text would.

    Never raises for an ordinary parse failure -- that becomes a
    PARSE-001 finding instead (unless the rule has been disabled), per
    the "conservative conclusions" design principle in scope.md 2.5.
    """
    sql_text = sql_text.strip()
    if not sql_text:
        return []

    results: list[StatementResult] = []

    for i, chunk in enumerate(splitter_mod.split_statements(sql_text)):
        if chunk.is_plsql:
            results.append(_build_plsql_result(i, chunk))
            continue

        try:
            stmt = sqlglot.parse_one(chunk.sql, read=DIALECT)
        except ParseError as e:
            results.append(StatementResult(
                index=i,
                statement_type="UNKNOWN",
                sql=chunk.sql,
                formatted_sql=None,
                parse_ok=False,
                parse_error=str(e),
                findings=_only(_make_finding(
                    "PARSE-001",
                    evidence=str(e).splitlines()[0] if str(e) else "Parse error",
                    confidence=CONFIDENCE_PROVEN,
                )),
            ))
            continue

        if stmt is None:
            continue

        if isinstance(stmt, exp.Command):
            findings, stype = _analyze_command(stmt, chunk.sql)
            results.append(StatementResult(
                index=i, statement_type=stype, sql=chunk.sql,
                formatted_sql=None, findings=findings,
            ))
            continue

        stmt_sql = stmt.sql(dialect=DIALECT, pretty=False)
        try:
            formatted = stmt.sql(dialect=DIALECT, pretty=True)
        except Exception:
            formatted = None

        stype = _statement_type_name(stmt)
        handler = None
        for cls, (_name, fn) in _STATEMENT_HANDLERS.items():
            if isinstance(stmt, cls):
                handler = fn
                break

        if handler is not None:
            findings = handler(stmt)
        else:
            findings = _only(_make_finding(
                "PARSE-002",
                evidence=stype,
                confidence=CONFIDENCE_UNKNOWN,
            ))

        # Cross-cutting Oracle-error checks that apply the same way to
        # every statement type (scope.md section 6, "Oracle error rule
        # catalog"). These run regardless of which statement handler fired.
        findings.extend(_check_division_by_zero(stmt))
        findings.extend(_check_scalar_subquery_multirow(stmt))
        findings.extend(_analyze_case_expressions(stmt))
        findings.extend(_check_window_functions(stmt))
        findings.extend(_analyze_subquery_correlation(stmt))
        findings.extend(_check_bind_variable_type_consistency(stmt))

        results.append(StatementResult(
            index=i,
            statement_type=stype,
            sql=stmt_sql,
            formatted_sql=formatted,
            findings=findings,
        ))

    return results


def format_sql(sql_text: str) -> dict:
    """Format SQL using the Oracle dialect. Returns a dict describing
    success/failure so the API layer never has to guess.
    """
    sql_text = sql_text.strip()
    if not sql_text:
        return {"ok": True, "formatted": ""}
    try:
        statements = sqlglot.parse(sql_text, read=DIALECT)
        parts = []
        for stmt in statements:
            if stmt is None:
                continue
            parts.append(stmt.sql(dialect=DIALECT, pretty=True))
        return {"ok": True, "formatted": ";\n\n".join(parts) + ";" if parts else ""}
    except ParseError as e:
        return {"ok": False, "error": str(e)}


def overall_status(statement_results: list[StatementResult]) -> str:
    """Roll up the highest severity across all statements into one status."""
    order = ["CRITICAL", "ERROR", "WARNING", "INFO"]
    seen = set()
    for r in statement_results:
        if not r.parse_ok:
            seen.add("CRITICAL")
        for f in r.findings:
            seen.add(f.severity)
    for sev in order:
        if sev in seen:
            return sev
    return "OK"

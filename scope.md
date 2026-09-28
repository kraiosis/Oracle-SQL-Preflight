# Oracle SQL Preflight Analyzer — Scope

**Document:** Product and Technical Scope  
**Current release:** 0.1.0 MVP  
**Target:** Offline-first Oracle DBA SQL analysis platform  
**Author:** Federico Guzman ([fedeguzman.com](https://fedeguzman.com) · [weblantropia.com](https://weblantropia.com) · [github.com/kraiosis](https://github.com/kraiosis))  
**Development:** AI-assisted with Claude (Anthropic) — see README.md, "Author & Credits"

---

# 1. Product vision

Build a local web-based Oracle DBA utility that acts as a **SQL preflight and impact-analysis layer** between writing SQL and executing SQL.

The system should answer progressively deeper questions:

```text
LEVEL 1
Can I parse this SQL?

        ↓

LEVEL 2
Does the SQL contain suspicious or contradictory logic?

        ↓

LEVEL 3
What might happen if it executes?

        ↓

LEVEL 4
What does the Oracle database say will happen?

        ↓

LEVEL 5
What would actually be affected in a controlled TEST/DEV environment?

        ↓

LEVEL 6
Can I safely approve or execute it?
```

The application must remain useful at Level 1/2 without Oracle and without AI.

---

# 2. Core design principles

## 2.1 Offline first

Core analysis must work without Internet access.

## 2.2 AI-free core

The analyzer must be deterministic.

The same SQL + same rules should produce the same result.

## 2.3 Simulation before execution

The application should distinguish:

```text
Analyze
Simulate
Execute
```

Execution should never occur merely because a statement was analyzed.

## 2.4 Evidence-based findings

Every finding should explain:

- Rule ID
- Severity
- Evidence
- Why it matters
- Recommendation
- Whether the finding is static or database-dependent

## 2.5 Conservative conclusions

The analyzer must distinguish:

```text
PROVEN
POSSIBLE
UNKNOWN
DATABASE-VERIFIED
```

For example:

```text
Potential zero-row UPDATE
```

is different from:

```text
Oracle verified: 0 rows match
```

---

# 3. Feature scope

## Phase 1 — Offline SQL foundation

### Implemented in MVP

- FastAPI local web server
- Browser SQL editor
- SQLGlot Oracle parser
- SQL formatting
- UPDATE analysis
- DELETE analysis
- INSERT recognition
- MERGE recognition
- SELECT * detection
- Missing WHERE detection
- SET/WHERE no-op detection
- Contradictory equality detection
- Constant-false predicate detection
- Rule IDs
- Severity levels
- Test suite
- Offline operation

### Planned expansion

- SELECT analysis
- JOIN analysis
- subquery analysis
- CTE analysis
- UNION/INTERSECT/MINUS analysis
- CASE analysis
- analytic/window function checks
- bind-variable analysis
- PL/SQL block parsing
- stored procedure calls
- dynamic SQL warnings
- multi-statement scripts
- schema definition to validate queries and data
- object format data source syntax validation like db.table, db.table@server

---

# 4. Oracle-specific logical analysis engine

This is the central differentiator.

## 4.1 UPDATE SET vs WHERE

Detect:

```sql
SET column = X
WHERE column = X
```

Possible finding:

```text
Potential no-op assignment
```

Future versions should understand more expressions:

```sql
SET salary = salary
SET salary = NVL(salary, 0)
SET status = UPPER(status)
SET date_col = TRUNC(date_col)
```

The engine should distinguish literal equality from expressions it cannot safely prove.

## 4.2 Contradictory predicates

Examples:

```sql
WHERE id = 10 AND id = 20
```

```sql
WHERE status = 'A'
  AND status = 'B'
```

```sql
WHERE amount > 100
  AND amount < 50
```

Future rules should identify additional contradictions.

## 4.3 Always-true predicates

Examples:

```sql
WHERE 1 = 1
```

Potentially suspicious conditions:

```sql
WHERE column = column
```

These require careful NULL semantics and should be reported with appropriate wording.

## 4.4 Always-false predicates

Examples:

```sql
WHERE 1 = 2
```

```sql
WHERE column = 'A'
  AND column = 'B'
```

where the values are demonstrably different.

## 4.5 NULL logic

Detect suspicious patterns:

```sql
WHERE column = NULL
```

versus:

```sql
WHERE column IS NULL
```

and:

```sql
WHERE column <> NULL
```

This should become a dedicated Oracle SQL correctness rule family.

---

# 5. DML safety engine

## UPDATE

Check:

- Missing WHERE
- Weak WHERE
- Contradictory WHERE
- Potential zero rows
- Potential no-op assignments
- SET columns repeated in WHERE
- Primary-key predicates
- Key-column modification
- Partition-key modification
- Indexed column modification
- Updating columns used by predicates
- Duplicate assignments
- Self assignments
- Suspicious mass update
- Nullable comparisons
- Implicit conversions
- Function-based predicates
- Subquery dependence

## DELETE

Check:

- Missing WHERE
- Contradictory WHERE
- Potential zero rows
- Potential mass delete
- Key predicate
- Partition predicate
- Subquery dependence

## INSERT

Check:

- Column/value count
- Target column list
- INSERT SELECT compatibility
- Potential datatype conversion
- NULL/default behavior
- Identity columns
- Sequence usage
- Duplicate-key risk when metadata is available

## MERGE

Check:

- Match condition
- Multiple-source-row risk
- UPDATE/INSERT branches
- Potential duplicate matches
- UPDATE of join columns
- DELETE branch
- Source cardinality
- Target cardinality

---

# 6. Oracle syntax/error intelligence

Build an Oracle rule catalog.

Examples to support where determinable:

```text
ORA-00904
ORA-00907
ORA-00917
ORA-00918
ORA-00933
ORA-00936
ORA-00937
ORA-00942
ORA-00947
ORA-00979
ORA-00984
ORA-01400
ORA-01403
ORA-01422
ORA-01427
ORA-01722
ORA-018xx date conversion errors
ORA-12899
ORA-2291
ORA-2292
```

Important:

Static analysis should say:

```text
Possible ORA-01722
```

when it cannot prove the error.

It should say:

```text
Oracle execution returned ORA-01722
```

only after actual database verification.

---

# 7. Oracle metadata connection

Add an optional Oracle connection manager.

Example:

```text
Environment: DEV
Host:
Port: 1521
Service:
Username:
Password:
```

Support environments such as:

```text
DEV
TEST
QA
UAT
STAGE
```

Credentials must be handled securely.

The connection layer should support:

- python-oracledb
- Oracle Easy Connect
- TNS aliases where available
- wallet-based authentication where appropriate
- connection testing
- read-only mode
- connection timeout
- environment labels

---

# 8. Database metadata analyzer

Once connected, retrieve metadata for:

- tables
- columns
- datatypes
- nullable attributes
- primary keys
- unique constraints
- foreign keys
- indexes
- partitions
- views
- synonyms
- sequences
- triggers
- statistics
- object ownership

This enables much more precise analysis.

Example:

```sql
UPDATE employees
SET department_id = 10
WHERE employee_id = 123;
```

Offline:

```text
employee_id existence: UNKNOWN
```

Oracle-connected:

```text
EMPLOYEE_ID: PRIMARY KEY
```

This provides substantially stronger analysis.

---

# 9. Safe impact simulation

This is a major feature.

For:

```sql
UPDATE employees
SET status = 'INACTIVE'
WHERE department_id = 10;
```

the tool can generate a controlled analysis such as:

```sql
SELECT COUNT(*)
FROM employees
WHERE department_id = 10;
```

Then potentially calculate effective changes using a generated comparison query.

Example conceptual output:

```text
Rows matching WHERE:       1,284
Rows whose values change:    947
Rows already INACTIVE:       337
```

This should be available only when connected to an appropriate TEST/DEV environment.

---

# 10. Transaction safety

Oracle-connected simulation should use transaction controls.

Potential workflow:

```text
BEGIN
   ↓
Analyze
   ↓
Generate impact SQL
   ↓
Optional controlled execution
   ↓
ROLLBACK
```

The application should never imply that ROLLBACK is a substitute for proper database safeguards.

For destructive testing, provide explicit confirmation and environment restrictions.

---

# 11. Explain Plan integration

Add:

```text
EXPLAIN PLAN
```

and display:

- operation
- object
- estimated rows
- cost
- cardinality
- access path
- join method
- predicate information
- index usage
- full scans
- partition pruning

The UI should clearly distinguish:

```text
Static rule finding
```

from:

```text
Oracle optimizer finding
```

---

# 12. Performance analyzer

Future rules:

- SELECT *
- Functions on indexed columns
- Implicit datatype conversions
- Leading wildcard LIKE
- OR conditions
- unnecessary DISTINCT
- unnecessary ORDER BY
- Cartesian joins
- missing join predicates
- excessive nested subqueries
- correlated subqueries
- non-sargable predicates
- partition pruning opportunities
- suspicious full-table scans
- index access concerns
- stale statistics where metadata supports the conclusion

Performance findings should not automatically mean "bad SQL"; they should identify conditions worth reviewing.

---

# 13. Join analyzer

Detect:

```sql
FROM A, B
```

without an obvious join condition.

Detect:

```sql
JOIN B ON A.id = B.id
```

and inspect:

- join columns
- datatype compatibility
- nullable keys
- unique/PK status
- possible one-to-many expansion
- possible many-to-many expansion
- missing predicates
- outer join conditions

Future database mode can estimate join cardinality.

---

# 14. Query impact analyzer

For SELECT:

```text
Estimated rows
Returned rows
Tables accessed
Indexes involved
Potential full scans
Join expansion
Sort operations
Aggregation
```

For DML:

```text
Rows matching WHERE
Rows changing
Rows unchanged
Rows potentially violating constraints
Affected tables
Affected indexes
```

---

# 15. Dependency analyzer

Identify dependencies on:

- tables
- views
- synonyms
- functions
- packages
- procedures
- sequences
- database links
- materialized views

Show a dependency graph:

```text
SQL
 |
 +--> VIEW_A
 |      |
 |      +--> TABLE_A
 |      +--> TABLE_B
 |
 +--> FUNCTION_X
        |
        +--> TABLE_C
```

---

# 16. SQL history

Store locally in SQLite:

- SQL text
- timestamp
- analysis result
- rule IDs
- environment
- user-entered description
- tags
- execution/simulation result

Searchable by:

```text
SQL
rule
table
date
environment
```

---

# 17. Reports

Generate:

- HTML report
- JSON report
- CSV result
- Markdown report
- PDF report
- text report

Example:

```text
Oracle SQL Preflight Report
----------------------------

Statement:
UPDATE employees ...

Status:
WARNING

Findings:
DML-SET-WHERE-001

Database:
DEV

Rows matched:
1,284

Rows changed:
947

Generated:
2026-09-22
```

---

# 18. Rule management

Create a local rule catalog.

Each rule should contain:

```json
{
  "id": "DML-SET-WHERE-001",
  "severity": "WARNING",
  "enabled": true,
  "description": "...",
  "category": "DML",
  "requires_database": false
}
```

Allow:

- enable/disable
- severity configuration
- category filtering
- custom organization rules
- DBA-specific rules
- exceptions

---

# 19. Custom rules

Allow DBAs to define rules such as:

```text
Never UPDATE production without primary-key predicate.
```

or:

```text
DELETE statements must contain customer_id.
```

or:

```text
Do not update more than 10,000 rows in TEST without confirmation.
```

These should be organization-specific and configurable.

---

# 20. SQL rewrite suggestions

Future functionality:

```text
Original
UPDATE employees
SET status = 'ACTIVE'
WHERE status = 'ACTIVE';
```

Suggested investigation:

```text
UPDATE employees
SET status = 'ACTIVE'
WHERE status <> 'ACTIVE';
```

However, the tool should never silently rewrite or execute SQL.

Suggested SQL must be clearly labeled as a proposed rewrite.

---

# 21. Before/after comparison

Allow:

```text
SQL A
vs
SQL B
```

Compare:

- tables
- columns
- predicates
- joins
- SET assignments
- risk findings
- execution plans
- estimated impact

---

# 22. DBA workspace

Potential UI:

```text
+-------------------------------------------------------------+
| Oracle SQL Preflight                         OFFLINE / DEV   |
+----------------+--------------------------------------------+
| SQL Lab        | SQL Editor                                 |
|                |                                            |
| Analyze        | UPDATE ...                                 |
| Simulator      |                                            |
| Connections    |                                            |
| History        +--------------------------------------------+
| Rules          | Findings                                   |
| Reports        |                                            |
| Settings       | WARNING  DML-SET-WHERE-001                |
|                | CRITICAL DML-001                          |
+----------------+--------------------------------------------+
```

---

# 23. Analysis pipeline

Target architecture:

```text
                     SQL INPUT
                         |
                         v
                  SQL PREPROCESSOR
                         |
                         v
                    SQL PARSER
                         |
                         v
                   AST / MODEL
                         |
          +--------------+--------------+
          |              |              |
          v              v              v
     Syntax Rules   Logic Rules    Oracle Rules
          |              |              |
          +--------------+--------------+
                         |
                         v
                  IMPACT ANALYZER
                         |
               +---------+---------+
               |                   |
               v                   v
          OFFLINE MODE        DB MODE
               |                   |
               |             Metadata
               |             Explain Plan
               |             COUNT/SIM
               |             Constraints
               |                   |
               +---------+---------+
                         |
                         v
                  FINDING ENGINE
                         |
                         v
                  REPORT / UI
```

---

# 24. Safety levels

Findings should use categories such as:

```text
CRITICAL
ERROR
WARNING
INFO
```

But severity must be attached to the rule, not used as a general judgment about the SQL.

Examples:

```text
CRITICAL
UPDATE without WHERE

WARNING
Potential no-op assignment

INFO
SELECT *
```

---

# 25. Future execution controls

If execution is eventually added:

```text
Analyze
   |
   v
Simulate
   |
   v
Review
   |
   v
Explicit Execute
```

Potential protections:

- Production execution disabled by default
- Environment allowlist
- Read-only mode
- Maximum affected-row threshold
- Explicit confirmation
- SQL hash confirmation
- Transaction handling
- Audit trail
- Execution timeout
- Session tagging

---

# 26. Testing strategy

Build automated tests for every rule.

Each rule should include:

```text
Positive example
Negative example
Boundary example
Oracle-specific example
False-positive test
```

Example:

```text
DML-SET-WHERE-001

Should detect:
SET status='A'
WHERE status='A'

Should not detect:
SET status='B'
WHERE status='A'
```

The analyzer should prioritize avoiding false positives.

---

# 27. Performance and scalability

The offline parser should remain responsive for ordinary DBA SQL.

Future versions should support:

- large SQL scripts
- multiple statements
- saved workspaces
- asynchronous database analysis
- cancellation
- query timeout
- cached metadata

---

# 28. Security requirements

Future database mode must address:

- encrypted credential storage
- Oracle wallet support
- least-privilege database accounts
- read-only connection mode
- TLS where configured
- no credential logging
- SQL audit history without passwords
- secure local storage
- production environment restrictions

---

# 29. Packaging

Future distribution options:

### Developer installation

```text
Python
virtualenv
pip
```

### DBA workstation package

```text
Windows executable
Local web server
Embedded dependencies
```

### Enterprise deployment

Potentially:

```text
Internal server
Browser clients
Central rule repository
Central audit/history
```

The enterprise version should remain optional; the original project goal remains useful as a standalone offline workstation tool.

---

# 30. Version roadmap

## V0.1 — MVP

Current:

- Offline web UI
- Parser
- Basic Oracle rules
- DML safety
- SET/WHERE logic
- Formatting

## V0.2 — Logical Analyzer

- Expanded expression reasoning
- NULL logic
- joins
- subqueries
- CTEs
- more contradiction detection
- Oracle error rule catalog
- better SELECT analysis

## V0.3 — Oracle Connectivity

- DEV/TEST connections
- metadata
- object validation
- datatype validation
- constraints
- indexes
- schema browser

## V0.4 — Impact Simulator

- COUNT simulation
- changed-row analysis
- before/after values
- transaction-safe testing
- impact reports

## V0.5 — Performance

- EXPLAIN PLAN
- execution-plan viewer
- index analysis
- cardinality
- join analysis
- performance rules

## V0.6 — DBA Workspace

- SQL history
- saved queries
- reports
- rule management
- custom rules
- SQL comparison
- schema definition
- data source syntax validation

## V0.7 — Advanced Oracle

- PL/SQL
- procedures/packages
- dependencies
- database links
- partition analysis
- optimizer information

## V0.9 - AI integration

- AI model integration
- AI chat assist
- AI agent integration

## V1.0 — Production Release

- hardened security
- installer
- comprehensive rule library
- automated test suite
- documentation
- production-safe execution controls
- enterprise configuration options

---

# 31. Features explicitly out of scope for the core

The following are not required for the core engine:

- Cloud AI
- Online SQL submission
- Sending SQL to external AI services
- Mandatory Internet connection
- Automatic execution against production
- Automatic rewriting/execution without approval

AI may be considered as an optional future feature, but the deterministic analyzer must remain fully functional without it.

---

# 32. Final product concept

The final application should feel like a DBA's:

```text
SQL PRE-FLIGHT CHECK
```

rather than merely another SQL editor.

The workflow should be:

```text
WRITE SQL
    |
    v
ANALYZE
    |
    +--> Syntax
    +--> Logic
    +--> Oracle rules
    +--> Safety
    |
    v
SIMULATE
    |
    +--> Matching rows
    +--> Rows changed
    +--> Constraints
    +--> Plan
    |
    v
REVIEW
    |
    v
OPTIONAL EXECUTION
```

The central value proposition is:

> **Find problems before the SQL changes data.**

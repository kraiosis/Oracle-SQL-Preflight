"""
Tests for the rule catalog (config/rules.json) and its effect on the
analysis engine. Covers scope.md section 18 ("Rule management"):
enable/disable and severity configuration.
"""

import json
import sys
import pathlib
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.analyzer import rules as rules_mod
from app.analyzer.engine import analyze_sql


def rule_ids(result_list):
    ids = set()
    for r in result_list:
        for f in r.findings:
            ids.add(f.rule_id)
    return ids


def finding_by_id(result_list, rule_id):
    for r in result_list:
        for f in r.findings:
            if f.rule_id == rule_id:
                return f
    return None


# --------------------------------------------------------------------------
# Loading behavior
# --------------------------------------------------------------------------

def test_missing_config_file_falls_back_to_defaults_and_writes_file():
    with tempfile.TemporaryDirectory() as d:
        path = pathlib.Path(d) / "rules.json"
        assert not path.exists()
        loaded = rules_mod.load_rules(path)
        assert "DML-001" in loaded
        assert loaded["DML-001"].severity == "CRITICAL"
        # File should now exist, written from defaults.
        assert path.exists()
        with open(path) as f:
            data = json.load(f)
        assert any(r["id"] == "DML-001" for r in data["rules"])


def test_corrupt_config_file_falls_back_to_defaults_without_crashing():
    with tempfile.TemporaryDirectory() as d:
        path = pathlib.Path(d) / "rules.json"
        path.write_text("{ this is not valid json ")
        loaded = rules_mod.load_rules(path)
        assert "DML-002" in loaded
        # The broken file is left alone, not overwritten.
        assert "not valid json" in path.read_text()


def test_config_missing_required_field_falls_back_to_defaults():
    with tempfile.TemporaryDirectory() as d:
        path = pathlib.Path(d) / "rules.json"
        path.write_text(json.dumps({"rules": [{"id": "X-1"}]}))  # missing severity etc.
        loaded = rules_mod.load_rules(path)
        assert "DML-001" in loaded  # fell back to defaults
        assert "X-1" not in loaded


def test_custom_config_overrides_severity_and_enabled():
    with tempfile.TemporaryDirectory() as d:
        path = pathlib.Path(d) / "rules.json"
        custom = {
            "rules": [
                {
                    "id": "PERF-001",
                    "category": "PERF",
                    "severity": "WARNING",   # bumped up from INFO
                    "enabled": False,        # disabled
                    "title": "SELECT * detected",
                    "description": "Custom description.",
                }
            ]
        }
        path.write_text(json.dumps(custom))
        loaded = rules_mod.load_rules(path)
        assert loaded["PERF-001"].severity == "WARNING"
        assert loaded["PERF-001"].enabled is False


# --------------------------------------------------------------------------
# Effect on the analysis engine (module-level catalog mutated in-memory,
# never persisted to disk by these tests -- persist=False)
# --------------------------------------------------------------------------

def test_disabling_a_rule_suppresses_its_finding():
    rules_mod.update_rule("DML-001", enabled=False, persist=False)
    try:
        results = analyze_sql("UPDATE employees SET x = 1")
        assert "DML-001" not in rule_ids(results)
    finally:
        rules_mod.update_rule("DML-001", enabled=True, persist=False)


def test_reenabling_a_rule_restores_its_finding():
    rules_mod.update_rule("DML-002", enabled=False, persist=False)
    rules_mod.update_rule("DML-002", enabled=True, persist=False)
    results = analyze_sql("DELETE FROM employees")
    assert "DML-002" in rule_ids(results)


def test_changing_severity_is_reflected_in_findings():
    rules_mod.update_rule("DML-SET-WHERE-001", severity="CRITICAL", persist=False)
    try:
        results = analyze_sql(
            "UPDATE employees SET status = 'ACTIVE' WHERE status = 'ACTIVE'"
        )
        f = finding_by_id(results, "DML-SET-WHERE-001")
        assert f is not None
        assert f.severity == "CRITICAL"
    finally:
        rules_mod.update_rule("DML-SET-WHERE-001", severity="WARNING", persist=False)


def test_update_unknown_rule_raises_keyerror():
    try:
        rules_mod.update_rule("NOT-A-REAL-RULE", enabled=False, persist=False)
        assert False, "expected KeyError"
    except KeyError:
        pass


def test_update_invalid_severity_raises_config_error():
    try:
        rules_mod.update_rule("PERF-001", severity="SUPER_BAD", persist=False)
        assert False, "expected RuleConfigError"
    except rules_mod.RuleConfigError:
        pass


def test_all_rules_and_get_rule_consistent():
    all_ids = {r.id for r in rules_mod.all_rules()}
    for rid in all_ids:
        assert rules_mod.get_rule(rid) is not None
    assert rules_mod.get_rule("NOT-A-REAL-RULE") is None


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

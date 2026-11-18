#!/usr/bin/env python3
"""Validate benchmark data files and consistency (design §10, 6 checks)."""
import json, sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SCORES = BASE / "registry/model-scores.json"
BENCHES = BASE / "registry/benchmarks.json"
COVERAGE = BASE / "registry/model-coverage.json"
REGISTRY = BASE / "registry/model-registry.json"


def load_json(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def check_schema(data, expected, label):
    if data.get("schema") != expected:
        print(f"FAIL {label}: schema = {data.get('schema')}, expected {expected}")
        return False
    print(f"OK {label}: schema = {expected}")
    return True


def check_scores_keys_in_bench_dict(scores_data, benches_data):
    """Check 3: every model's scores bench must be in benches dict."""
    benches = benches_data.get("benches", {})
    issues = []
    for mid, m in scores_data.get("models", {}).items():
        for s in m.get("scores", []):
            if s["bench"] not in benches:
                issues.append(f"  model {mid}: bench '{s['bench']}' not in benches dict")
    if issues:
        print("FAIL check_bench_in_dict:")
        for i in issues:
            print(i)
        return False
    else:
        print("OK check_bench_in_dict: all scores bench ids found in benches dict")
        return True


def check_scores_required_fields(scores_data):
    """Check 4: every score entry has source_url + date + setting."""
    issues = []
    for mid, m in scores_data.get("models", {}).items():
        for s in m.get("scores", []):
            for field in ("source_url", "date", "setting"):
                if field not in s or not s[field]:
                    issues.append(f"  model {mid}, bench {s.get('bench','?')}: missing/empty '{field}'")
    if issues:
        print("FAIL check_scores_required_fields:")
        for i in issues:
            print(i)
        return False
    else:
        print("OK check_scores_required_fields: all entries have source_url+date+setting")
        return True


def check_bench_setting_in_whitelist(scores_data, benches_data):
    """Check 5: setting must be in benches dict's settings[] for that bench."""
    benches = benches_data.get("benches", {})
    issues = []
    for mid, m in scores_data.get("models", {}).items():
        for s in m.get("scores", []):
            bench_name = s["bench"]
            setting = s["setting"]
            if bench_name not in benches:
                issues.append(f"  model {mid}, bench {bench_name}: bench not in dict; setting={setting}")
            elif setting not in benches[bench_name].get("settings", []):
                # Note: some settings like ctx_128k in graphwalks are allowed as a setting value
                issues.append(f"  model {mid}, bench {bench_name}: setting '{setting}' not in whitelist {benches[bench_name].get('settings', [])}")
    if issues:
        print("FAIL check_bench_setting_in_whitelist:")
        for i in issues:
            print(i)
        return False
    else:
        print("OK check_bench_setting_in_whitelist: all settings in whitelist")
        return True


def check_comparable_group(scores_data, benches_data):
    """Check 6: comparable group = bench_id + bench_version + setting + harness; group内样本 < 3 不做分位着色。
    报告每组样本数（这里只计算每个 (bench, setting, harness) 的计数，不实际着色)。"""
    from collections import Counter
    groups = Counter()
    for mid, m in scores_data.get("models", {}).items():
        for s in m.get("scores", []):
            key = (s["bench"], s.get("bench_version", "unspecified"), s["setting"], s["harness"])
            groups[key] += 1
    # report groups with count < 3
    small = {k: v for k, v in groups.items() if v < 3}
    if small:
        print(f"WARN comparable group < 3 samples (skipping whisker coloring):")
        for k, v in sorted(small.items()):
            print(f"  count={v} group {k}")
    else:
        print(f"INFO: all {len(groups)} comparable groups have >=3 samples")
    # also verify no contradictory assignments: same (bench,setting,harness) but different model values causing conflict
    # simplified: check same (bench,setting,harness) group has all same unit
    from collections import defaultdict
    units = defaultdict(set)
    for mid, m in scores_data.get("models", {}).items():
        for s in m.get("scores", []):
            units[(s["bench"], s["setting"], s["harness"])].add(s["unit"])
    conflicts = {k: len(v) for k, v in units.items() if len(v) > 1}
    if conflicts:
        print(f"WARN unit conflicts in (bench,setting,harness) groups:")
        for k, n in sorted(conflicts.items()):
            print(f"  {k}: {n} different units")
    else:
        print("OK: no unit conflicts in comparable groups")
    return True


def check_coverage_consistency(scores_data, coverage_data, registry_data):
    """Check 7: coverage ids must exist in scores keys OR registry; and coverage↔scores correspond."""
    scores_keys = set(scores_data.get("models", {}).keys())
    registry_keys = set(registry_data.get("models", {}).keys()) if isinstance(registry_data.get("models"), dict) else set()
    cov_groups = coverage_data.get("groups", [])
    issues = []
    # every coverage model id must be in scores OR registry
    for grp in cov_groups:
        for md in grp.get("models", []):
            mid = md["id"]
            if mid not in scores_keys and mid not in registry_keys:
                issues.append(f"  coverage id '{mid}' not in model-scores.json or registry")
    if issues:
        print("FAIL check_coverage_consistency:")
        for i in issues:
            print(i)
        return False
    else:
        print("OK check_coverage_consistency: all coverage ids found in scores or registry")
        # also verify every scores key appears somewhere in coverage groups
        covered = set()
        for grp in cov_groups:
            for md in grp.get("models", []):
                covered.add(md["id"])
        missing = scores_keys - covered
        if missing:
            print(f"WARN: {len(missing)} scores keys not in coverage groups: {sorted(missing)[:10]}...")
        else:
            print("INFO: all scores keys present in coverage groups")
        return True


def main():
    errors = 0
    scores_data = load_json(SCORES)
    benches_data = load_json(BENCHES)
    coverage_data = load_json(COVERAGE)
    registry_data = load_json(REGISTRY)
    checks = [
        ("model-scores.json schema", lambda: check_schema(scores_data, 1, "model-scores")),
        ("model-coverage.json schema", lambda: check_schema(coverage_data, 1, "model-coverage")),
        ("benchmarks.json schema", lambda: check_schema(benches_data, 1, "benchmarks")),
        ("scores keys in benches dict", lambda: check_scores_keys_in_bench_dict(scores_data, benches_data)),
        ("scores required fields", lambda: check_scores_required_fields(scores_data)),
        ("bench setting in whitelist", lambda: check_bench_setting_in_whitelist(scores_data, benches_data)),
        ("comparable group analysis", lambda: check_comparable_group(scores_data, benches_data)),
        ("coverage consistency", lambda: check_coverage_consistency(scores_data, coverage_data, registry_data)),
    ]
    for name, fn in checks:
        print(f"\n=== {name} ===")
        try:
            ok = fn()
            if not ok:
                errors += 1
        except Exception as e:
            print(f"ERROR running {name}: {e}")
            errors += 1
    print(f"\n{'='*40}")
    print(f"Total check failures: {errors}")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
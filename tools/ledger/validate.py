#!/usr/bin/env python3
"""Validate the ledger: JSON Schema structure + cross-file references.

CI（validate.yml）与本地自查共用同一入口：

    python tools/ledger/validate.py             # 校验真实账本（默认仓库根 ledger/）
    python tools/ledger/validate.py --selftest  # 内置自测：正例 + 负例，不碰真实账本

JSON Schema 表达不了的跨文件引用由这里补：
  * activity.vendor          → vendors/<id>.json 必须存在
  * changelog.vendor         → 同上
  * changelog.ref（非 null） → activities/<id>.json 必须存在
  * sources.*[].vendor       → vendors/<id>.json 必须存在
  * 文件名必须等于内容 id（vendorId / activityId 同时是文件名）
  * 编号重复（厂家 id / 活动 id）
  * 日期字段须是真实日历日期且带时区（2026-02-30、缺时区时间戳拒绝）
    —— schema 的 format 关键字在裸 jsonschema 下不生效（可选库未装时不校验），
       所以真实日期在这里后置检查，不依赖 format checker。

退出码非 0 即有错误（CI 拦住提交）。
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

try:
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource
except ImportError:  # pragma: no cover - CI/本地都会先装 jsonschema
    print("缺依赖：python -m pip install jsonschema", file=sys.stderr)
    sys.exit(2)

DEFAULT_LEDGER = Path(__file__).resolve().parents[2] / "ledger"

# date（YYYY-MM-DD）与 dateTime（ISO 8601 带时区）字段键名，做真实日历后置检查。
DATE_KEYS = {"expires", "start", "end"}
DATETIME_KEYS = {"verified_at", "discovered_at", "published_at", "ts"}


def _flatten(validator: Draft202012Validator, doc, errors: list[str], where: str) -> None:
    for err in validator.iter_errors(doc):
        loc = ".".join(str(p) for p in err.absolute_path)
        errors.append(f"{where}: {loc + ': ' if loc else ''}{err.message}")


def _walk_dates(node, where: str, errors: list[str]) -> None:
    """真实日历检查：2026-02-30 能过 schema 正则但不是合法日期；时间戳缺时区也不放过。"""
    if isinstance(node, dict):
        for key, value in node.items():
            if value is None:
                continue
            if key in DATE_KEYS and isinstance(value, str):
                try:
                    date.fromisoformat(value)
                except ValueError:
                    errors.append(f"{where}.{key}: 非真实日期 {value!r}")
            elif key in DATETIME_KEYS and isinstance(value, str):
                try:
                    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                except ValueError:
                    errors.append(f"{where}.{key}: 非真实时间戳 {value!r}")
                else:
                    if parsed.tzinfo is None:
                        errors.append(f"{where}.{key}: 缺时区（统一 +08:00）{value!r}")
            else:
                _walk_dates(value, f"{where}.{key}", errors)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            _walk_dates(item, f"{where}[{i}]", errors)


def _load_json(path: Path, errors: list[str]):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        errors.append(f"{path.name}: JSON 解析失败（第 {exc.lineno} 行：{exc.msg}）")
        return None


def validate_ledger(ledger_dir: Path) -> list[str]:
    """返回全部错误；空列表 = 通过。目录或文件缺失不视为错误（空账本合法）。"""
    errors: list[str] = []
    schema_path = ledger_dir / "schema.json"
    schema_doc = _load_json(schema_path, errors) if schema_path.is_file() else None
    if schema_doc is None:
        errors.append(f"{schema_path}: schema.json 不存在或不可解析")
        return errors
    if not isinstance(schema_doc.get("version"), int):
        errors.append(f"{schema_path.name}: 缺 version（改结构必须升版本）")

    registry = Registry().with_resource(
        schema_doc["$id"], Resource.from_contents(schema_doc)
    )

    def sub_validator(def_name: str) -> Draft202012Validator:
        return Draft202012Validator(
            {"$ref": f"{schema_doc['$id']}#/$defs/{def_name}"}, registry=registry
        )

    vendors: dict[str, str] = {}    # id -> 相对路径
    activities: dict[str, str] = {}

    for kind, dirname in (("vendor", "vendors"), ("activity", "activities")):
        directory = ledger_dir / dirname
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            doc = _load_json(path, errors)
            if doc is None:
                continue
            where = f"{dirname}/{path.name}"
            _flatten(sub_validator(kind), doc, errors, where)
            _walk_dates(doc, where, errors)
            obj_id = doc.get("id")
            if not isinstance(obj_id, str):
                continue
            if path.stem != obj_id:
                errors.append(f"{where}: 文件名 {path.stem!r} 与 id {obj_id!r} 不一致")
            store = vendors if kind == "vendor" else activities
            if obj_id in store:
                errors.append(f"{where}: {kind} id 重复 {obj_id!r}")
            store[obj_id] = where
            if kind == "activity":
                vendor = doc.get("vendor")
                if isinstance(vendor, str) and vendor not in vendors:
                    errors.append(
                        f"{where}: activity.vendor 引用了不存在的厂家 {vendor!r}"
                    )

    cl_path = ledger_dir / "changelog.jsonl"
    if cl_path.is_file():
        for lineno, line in enumerate(
            cl_path.read_text(encoding="utf-8").splitlines(), 1
        ):
            line = line.strip()
            if not line:
                continue
            where = f"changelog.jsonl:{lineno}"
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append(f"{where}: JSON 解析失败（{exc.msg}）")
                continue
            _flatten(sub_validator("changelog_entry"), entry, errors, where)
            _walk_dates(entry, where, errors)
            vendor = entry.get("vendor")
            if isinstance(vendor, str) and vendor not in vendors:
                errors.append(f"{where}: changelog.vendor 引用了不存在的厂家 {vendor!r}")
            ref = entry.get("ref")
            if isinstance(ref, str) and ref not in activities:
                errors.append(f"{where}: changelog.ref 引用了不存在的活动 {ref!r}")

    src_path = ledger_dir / "sources.json"
    if src_path.is_file():
        doc = _load_json(src_path, errors)
        if doc is not None:
            _flatten(sub_validator("sources_file"), doc, errors, "sources.json")
            for section, items in doc.items():
                for i, item in enumerate(items):
                    vendor = item.get("vendor")
                    if isinstance(vendor, str) and vendor not in vendors:
                        errors.append(
                            f"sources.json.{section}[{i}]: 引用了不存在的厂家 {vendor!r}"
                        )

    return errors


# ---------------------------------------------------------------------------
# 内置自测：用真实 schema 在临时目录里构造正例与负例，证明校验真的拦得住。
# ---------------------------------------------------------------------------

GOOD_VENDOR = {
    "id": "acme",
    "name": "Acme AI",
    "region": "cn",
    "endpoints": {"openai": "https://api.acme.example/v1"},
    "auth": {"type": "bearer"},
    "free_quota": {
        "per_model": {"acme-lite": {"tokens": 1000000, "expires": None}}
    },
    "models": [{"id": "acme-lite", "tier": "free"}],
    "notes": "自测样例",
    "verified_at": "2026-11-27T12:00:00+08:00",
    "sources": ["https://acme.example/pricing"],
}

GOOD_BETA = {
    "id": "beta",
    "name": "Beta Cloud",
    "region": "global",
    "endpoints": {"openai": "https://api.beta.example/v1"},
    "auth": {"type": "api_key"},
    "free_quota": {},
    "models": [{"id": "beta-pro", "tier": "paid"}],
    "verified_at": "2026-11-27T12:00:00+08:00",
    "sources": ["https://docs.beta.example"],
}

GOOD_ACTIVITY = {
    "id": "act-20261127-acme-launch",
    "vendor": "acme",
    "title": "0 元购体验包",
    "grant": {
        "type": "activity_package",
        "model": "acme-lite",
        "tokens": 5000000,
        "method": "控制台 0 元购买",
    },
    "window": {"start": "2026-11-27", "end": None},
    "action": "控制台 → 资源包 → 0 元购买",
    "status": "verified",
    "discovered_at": "2026-11-27T12:00:00+08:00",
    "verified_at": "2026-11-27T13:00:00+08:00",
    "sources": ["https://acme.example/news"],
    "evidence": "evidence/2026/11/acme.png",
}

GOOD_CHANGELOG = [
    {"ts": "2026-11-27T12:00:00+08:00", "vendor": "acme",
     "kind": "vendor_added", "field": "$", "ref": None},
    {"ts": "2026-11-27T13:00:00+08:00", "vendor": "acme",
     "kind": "activity_added", "field": "act-20261127-acme-launch",
     "ref": "act-20261127-acme-launch"},
]

GOOD_SOURCES = {
    "announcement_pages": [
        {"vendor": "acme", "url": "https://acme.example/news", "parser": "dom"}
    ],
    "probe_endpoints": [
        {"vendor": "beta", "url": "https://api.beta.example/quota", "needs_key": True}
    ],
}


def _dump(doc) -> str:
    return json.dumps(doc, ensure_ascii=False, indent=2)


def _build_case(root: Path, schema_doc: dict, overrides: dict) -> Path:
    """按 overrides 构造一个临时账本。overrides 可键：
    vendors / activities：文件名 -> 文档（None 表示跳过该文件）
    changelog：行列表；sources：文档；vendor_defaults / activity_defaults：被覆盖
    """
    (root / "schema.json").write_text(
        json.dumps(schema_doc, ensure_ascii=False), encoding="utf-8"
    )
    vendors = overrides.get("vendors", {"acme": GOOD_VENDOR, "beta": GOOD_BETA})
    (root / "vendors").mkdir(exist_ok=True)
    for name, doc in vendors.items():
        if doc is not None:
            (root / "vendors" / f"{name}.json").write_text(_dump(doc), encoding="utf-8")
    activities = overrides.get(
        "activities", {"act-20261127-acme-launch": GOOD_ACTIVITY}
    )
    (root / "activities").mkdir(exist_ok=True)
    for name, doc in activities.items():
        if doc is not None:
            (root / "activities" / f"{name}.json").write_text(_dump(doc), encoding="utf-8")
    changelog = overrides.get("changelog", GOOD_CHANGELOG)
    if changelog is not None:
        (root / "changelog.jsonl").write_text(
            "\n".join(json.dumps(e, ensure_ascii=False) for e in changelog) + "\n",
            encoding="utf-8",
        )
    sources = overrides.get("sources", GOOD_SOURCES)
    if sources is not None:
        (root / "sources.json").write_text(_dump(sources), encoding="utf-8")
    return root


def _selftest() -> bool:
    schema_doc = json.loads(
        (DEFAULT_LEDGER / "schema.json").read_text(encoding="utf-8")
    )
    ok = True

    def expect(name: str, overrides: dict, *, valid: bool, needle: str = "") -> None:
        nonlocal ok
        with tempfile.TemporaryDirectory() as tmp:
            errors = validate_ledger(_build_case(Path(tmp), schema_doc, overrides))
        if valid:
            if errors:
                print(f"FAIL {name}: 预期通过，实际 {errors}")
                ok = False
            else:
                print(f"ok   {name}")
        else:
            if needle in "".join(errors):
                print(f"ok   {name}")
            else:
                print(f"FAIL {name}: 预期报错含「{needle}」，实际 {errors or '(无错误)'}")
                ok = False

    # 正例：完整合法小账本
    expect("positive-full-ledger", {}, valid=True)

    # 负例 1：厂家缺证据来源（schema required）
    vendor_no_sources = copy.deepcopy(GOOD_VENDOR)
    del vendor_no_sources["sources"]
    expect("vendor-missing-sources", {"vendors": {"acme": vendor_no_sources}},
           valid=False, needle="sources")

    # 负例 2：活动编号重复（两个文件同 id）
    dup_a = dict(GOOD_ACTIVITY)
    dup_b = dict(GOOD_ACTIVITY)
    dup_b["vendor"] = "beta"  # 避免厂家引用错误抢镜；id 相同才算编号重复
    expect("duplicate-activity-id",
           {"activities": {"act-20261127-acme-launch": dup_a,
                           "act-20261127-acme-launch-2": dup_b}},
           valid=False, needle="重复")

    # 负例 3：日期格式错（过不了 schema 正则）
    bad_date = copy.deepcopy(GOOD_BETA)
    bad_date["free_quota"] = {"per_model": {
        "beta-lite": {"tokens": 1, "expires": "2026/12/27"}}}
    expect("bad-date-format", {"vendors": {"acme": GOOD_VENDOR, "beta": bad_date}},
           valid=False, needle="2026/12/27")

    # 负例 4：日历上不存在的日期（正则能过，后置检查拦）
    fake_date = copy.deepcopy(GOOD_BETA)
    fake_date["free_quota"] = {"per_model": {
        "beta-lite": {"tokens": 1, "expires": "2026-02-30"}}}
    expect("impossible-date", {"vendors": {"acme": GOOD_VENDOR, "beta": fake_date}},
           valid=False, needle="非真实日期")

    # 负例 5：时间戳缺时区（后置检查拦）
    naive = copy.deepcopy(GOOD_BETA)
    naive["verified_at"] = "2026-11-27T12:00:00"
    expect("naive-datetime", {"vendors": {"acme": GOOD_VENDOR, "beta": naive}},
           valid=False, needle="缺时区")

    # 负例 6：未知字段（additionalProperties: false）
    extra = copy.deepcopy(GOOD_VENDOR)
    extra["bonus"] = 1
    expect("unknown-field", {"vendors": {"acme": extra}},
           valid=False, needle="bonus")

    # 负例 7：活动引用不存在的厂家
    ghost = copy.deepcopy(GOOD_ACTIVITY)
    ghost["vendor"] = "ghost"
    expect("activity-ghost-vendor",
           {"activities": {"act-20261127-acme-launch": ghost}},
           valid=False, needle="不存在的厂家")

    # 负例 8：changelog.ref 引用不存在的活动
    cl = list(GOOD_CHANGELOG)
    cl.append({"ts": "2026-11-27T14:00:00+08:00", "vendor": "acme",
               "kind": "quota_changed", "field": "free_quota.per_model.acme-lite",
               "from": 1000000, "to": 500000, "ref": "act-20260101-ghost"})
    expect("changelog-ghost-ref", {"changelog": cl},
           valid=False, needle="不存在的活动")

    # 负例 9：sources.json 引用不存在的厂家
    src = copy.deepcopy(GOOD_SOURCES)
    src["announcement_pages"].append(
        {"vendor": "ghost", "url": "https://ghost.example/news", "parser": "vlm"})
    expect("sources-ghost-vendor", {"sources": src},
           valid=False, needle="不存在的厂家")

    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER,
                        help="账本目录（默认仓库根 ledger/）")
    parser.add_argument("--selftest", action="store_true",
                        help="跑内置自测（正例 + 负例），不碰真实账本")
    args = parser.parse_args()

    if args.selftest:
        print("自测开始（使用真实 schema.json）…")
        passed = _selftest()
        print("自测通过" if passed else "自测失败")
        return 0 if passed else 1

    errors = validate_ledger(args.ledger.resolve())
    if errors:
        print(f"账本校验失败，{len(errors)} 个错误：")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"账本校验通过：{args.ledger}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

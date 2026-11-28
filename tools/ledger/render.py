#!/usr/bin/env python3
"""从账本渲染三份静态产物（设计文档 3.6 / 4 节接口表）：

    registry.json  模板同步：厂家/模型/额度快照（含活动 effective_tokens），带 schema_version + seq
    intel.json     情报列表：活动 + 变更摘要（changelog 最近条目）
    intel.xml      同一份 intel.json 的 RSS 版本

约定：
  * 产物写入 --out 目录（默认仓库根 render/），全部带 GENERATED 头，不手改；
  * 同一条活动在三份产物里由同一份内存数据渲染，内容不会漂移；
  * 产物数据源严格限定：活动只来自 activities/*.json，变更摘要只来自 changelog.jsonl
    —— 账本里没有的条目不会出现在任何产物里；
  * effective_tokens = per_model 长期额度 + 该模型名下活动额度（schema 不落盘，由这里算）；
  * 签名不在本工具（T1.3 CI 流水线做，签名覆盖 canonical JSON：键排序、无空白）。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate import DEFAULT_LEDGER, validate_ledger  # noqa: E402

CST = timezone(timedelta(hours=8))
GENERATED_NOTE = "GENERATED — do not edit by hand; rendered from ledger/ by tools/ledger/render.py"
CHANGE_SUMMARY_LIMIT = 20  # intel.json 变更摘要条数


def now_iso() -> str:
    return datetime.now(CST).replace(microsecond=0).isoformat()


def rfc822(iso: str) -> str:
    """dateTime → RSS pubDate（UTC，形如 Sat, 27 Nov 2026 06:00:00 GMT）。"""
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return dt.astimezone(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def canonical(payload) -> str:
    """签名口径的规范化 JSON（键排序、无空白）——T1.3 签名直接用它。"""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def load_ledger(ledger: Path) -> dict:
    """读全量账本。返回 vendors（按 id 排序）与 activities、changes。"""
    vendors: list[dict] = []
    vendor_dir = ledger / "vendors"
    if vendor_dir.is_dir():
        for path in sorted(vendor_dir.glob("*.json")):
            vendors.append(read_json(path))
    activities: list[dict] = []
    act_dir = ledger / "activities"
    if act_dir.is_dir():
        for path in sorted(act_dir.glob("*.json")):
            activities.append(read_json(path))
    changes: list[dict] = []
    cl = ledger / "changelog.jsonl"
    if cl.is_file():
        for line in cl.read_text(encoding="utf-8").splitlines():
            if line.strip():
                changes.append(json.loads(line))
    return {"vendors": vendors, "activities": activities, "changes": changes}


def effective_tokens(vendor: dict, activity: dict) -> int | None:
    """活动对单模型的有效额度：per_model 长期 + 活动包。per_model 缺该模型时只算活动。"""
    per_model = vendor.get("free_quota", {}).get("per_model", {})
    base = per_model.get(activity["grant"]["model"], {}).get("tokens", 0)
    return base + activity["grant"]["tokens"]


def render_registry(book: dict, seq: int) -> dict:
    payload = {
        "_generated": GENERATED_NOTE,
        "schema_version": 1,
        "seq": seq,
        "generated_at": now_iso(),
        "vendors": [],
    }
    acts_by_vendor: dict[str, list[dict]] = {}
    for act in book["activities"]:
        acts_by_vendor.setdefault(act["vendor"], []).append(act)

    for vendor in book["vendors"]:
        v = {k: vendor[k] for k in (
            "id", "name", "region", "endpoints", "auth", "free_quota",
            "models", "notes", "status", "verified_at", "sources") if k in vendor}
        v["activities"] = []
        for act in acts_by_vendor.get(vendor["id"], []):
            a = {k: act[k] for k in (
                "id", "title", "grant", "window", "action", "status",
                "discovered_at", "verified_at", "sources", "evidence") if k in act}
            a["effective_tokens"] = effective_tokens(vendor, act)
            v["activities"].append(a)
        payload["vendors"].append(v)
    return payload


def render_intel(book: dict) -> dict:
    acts = []
    for act in book["activities"]:
        item = {
            "id": act["id"], "vendor": act["vendor"], "title": act["title"],
            "grant": act["grant"], "window": act["window"], "action": act["action"],
            "status": act["status"], "discovered_at": act["discovered_at"],
            "verified_at": act.get("verified_at"), "sources": act["sources"],
            "evidence": act.get("evidence"),
        }
        if act["status"] == "retracted":
            # 撤回（T4.4）：原因随情报下发，客户端据此标「已撤回」并下发更正
            item["retracted_reason"] = act.get("retracted_reason") or "内容有误"
        acts.append(item)
    return {
        "_generated": GENERATED_NOTE,
        "generated_at": now_iso(),
        "activities": acts,
        "changes": book["changes"][-CHANGE_SUMMARY_LIMIT:],
    }


def render_intel_xml(intel: dict, base_url: str = "") -> str:
    """intel.json 的 RSS 版本：活动一条 item，变更摘要一条 item。"""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f"<!-- {GENERATED_NOTE} -->",
        '<rss version="2.0">',
        "  <channel>",
        f"    <title>Facet 情报</title>",
        f"    <link>{escape(base_url or 'https://facet.skillgo.cn/')}</link>",
        "    <description>账本渲染的活动与变更（同 intel.json）</description>",
        f"    <lastBuildDate>{rfc822(intel['generated_at'])}</lastBuildDate>",
    ]
    for act in intel["activities"]:
        grant = act["grant"]
        desc = (f"{act['title']}｜{grant['tokens']:,} tokens"
                f"（{grant.get('method') or '按活动规则领取'}）｜{act['action']}")
        lines += [
            "    <item>",
            f"      <title>{escape(act['title'])}</title>",
            f"      <link>{escape((act['sources'] or [''])[0])}</link>",
            f"      <description>{escape(desc)}</description>",
            f"      <pubDate>{rfc822(act['discovered_at'])}</pubDate>",
            f"      <guid isPermaLink=\"false\">{escape(act['id'])}</guid>",
            "    </item>",
        ]
    for change in intel["changes"]:
        change_title = f"{change['vendor']}：{change['kind']}"
        if "from" in change:
            change_desc = f"{change['field']}: {change.get('from')!r} → {change.get('to')!r}"
        else:
            change_desc = change["field"]
        guid = f"{change['ts']}/{change['field']}"
        lines += [
            "    <item>",
            f"      <title>{escape(change_title)}</title>",
            f"      <link>{escape(base_url or 'https://facet.skillgo.cn/intel')}</link>",
            f"      <description>{escape(change_desc)}</description>",
            f"      <pubDate>{rfc822(change['ts'])}</pubDate>",
            f"      <guid isPermaLink=\"false\">{escape(guid)}</guid>",
            "    </item>",
        ]
    lines += ["  </channel>", "</rss>"]
    return "\n".join(lines) + "\n"


def write_out(out: Path, name: str, text: str) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    path = out / name
    path.write_text(text, encoding="utf-8")
    return path


def render_all(ledger: Path, out: Path) -> dict:
    book = load_ledger(ledger)
    seq = len(book["changes"])
    registry = render_registry(book, seq)
    intel = render_intel(book)
    xml = render_intel_xml(intel)
    write_out(out, "registry.json", json.dumps(registry, ensure_ascii=False, indent=2) + "\n")
    write_out(out, "intel.json", json.dumps(intel, ensure_ascii=False, indent=2) + "\n")
    write_out(out, "intel.xml", xml)
    return {"registry": registry, "intel": intel, "xml": xml,
            "vendors": len(registry["vendors"]),
            "activities": len(intel["activities"]),
            "changes": len(intel["changes"])}


# ---------------------------------------------------------------------------
# 自测：临时账本上录入 → 渲染 → 断言验收标准
# ---------------------------------------------------------------------------

GOOD_VENDOR = {
    "id": "acme", "name": "Acme AI", "region": "cn",
    "endpoints": {"openai": "https://api.acme.example/v1"},
    "auth": {"type": "bearer"},
    "free_quota": {"per_model": {"acme-lite": {"tokens": 1000000, "expires": None}}},
    "models": [{"id": "acme-lite", "tier": "free"}],
    "notes": "自测样例",
    "verified_at": "2026-11-27T12:00:00+08:00",
    "sources": ["https://acme.example/pricing"],
}

GOOD_ACTIVITY = {
    "id": "act-20261127-acme-launch", "vendor": "acme",
    "title": "0 元购体验包",
    "grant": {"type": "activity_package", "model": "acme-lite",
              "tokens": 5000000, "method": "控制台 0 元购买"},
    "window": {"start": "2026-11-27", "end": None},
    "action": "控制台 → 资源包 → 0 元购买",
    "status": "verified",
    "discovered_at": "2026-11-27T12:00:00+08:00",
    "verified_at": "2026-11-27T13:00:00+08:00",
    "sources": ["https://acme.example/news"],
    "evidence": "evidence/2026/11/acme.png",
}


def _selftest() -> bool:
    import record  # 复用 record 的写入与 changelog 约定

    ok = True

    def expect(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        print(("ok   " if cond else "FAIL ") + name + (f"（{detail}）" if detail and not cond else ""))
        ok &= cond

    with tempfile.TemporaryDirectory() as tmp:
        ledger = Path(tmp)
        shutil.copy(DEFAULT_LEDGER / "schema.json", ledger / "schema.json")
        (ledger / "vendors").mkdir()
        (ledger / "vendors" / "acme.json").write_text(
            json.dumps(GOOD_VENDOR, ensure_ascii=False), encoding="utf-8")
        (ledger / "activities").mkdir()
        (ledger / "activities" / f"{GOOD_ACTIVITY['id']}.json").write_text(
            json.dumps(GOOD_ACTIVITY, ensure_ascii=False), encoding="utf-8")
        (ledger / "changelog.jsonl").write_text(
            "\n".join([
                json.dumps({"ts": "2026-11-27T12:00:00+08:00", "vendor": "acme",
                            "kind": "vendor_added", "field": "$", "ref": None},
                           ensure_ascii=False),
                json.dumps({"ts": "2026-11-27T13:00:00+08:00", "vendor": "acme",
                            "kind": "activity_added",
                            "field": GOOD_ACTIVITY["id"],
                            "ref": GOOD_ACTIVITY["id"]},
                           ensure_ascii=False),
            ]) + "\n", encoding="utf-8")

        out = Path(tmp) / "render"
        stats = render_all(ledger, out)

        registry = json.loads((out / "registry.json").read_text(encoding="utf-8"))
        intel = json.loads((out / "intel.json").read_text(encoding="utf-8"))
        xml = (out / "intel.xml").read_text(encoding="utf-8")

        # 1. 三份产物带 GENERATED 头
        expect("产物带 GENERATED 头",
               all(GENERATED_NOTE in p for p in
                   (registry["_generated"], intel["_generated"]))
               and "GENERATED" in xml)

        # 2. 同一条活动在三份产物里内容一致
        reg_act = registry["vendors"][0]["activities"][0]
        intel_act = intel["activities"][0]
        same = (reg_act["id"] == intel_act["id"] == GOOD_ACTIVITY["id"]
                and reg_act["title"] == intel_act["title"] == GOOD_ACTIVITY["title"]
                and reg_act["grant"]["tokens"] == intel_act["grant"]["tokens"]
                == GOOD_ACTIVITY["grant"]["tokens"]
                and reg_act["action"] == intel_act["action"])
        in_xml = (f"<title>{GOOD_ACTIVITY['title']}</title>" in xml
                  and f"<guid isPermaLink=\"false\">{GOOD_ACTIVITY['id']}</guid>" in xml)
        expect("同一条活动三份产物内容一致", same and in_xml)

        # 3. effective_tokens 由 render 计算（1,000,000 + 5,000,000）
        expect("effective_tokens = 长期 + 活动",
               reg_act["effective_tokens"] == 6000000)

        # 4. changelog 里没有的条目不出现：产物条目严格等于账本内容
        ledger_changes = [json.loads(line) for line in
                          (ledger / "changelog.jsonl").read_text(encoding="utf-8")
                          .splitlines() if line.strip()]
        expect("产物条目数与账本一致（不多不少）",
               stats["activities"] == 1 and len(intel["activities"]) == 1
               and len(registry["vendors"][0]["activities"]) == 1
               and intel["changes"] == ledger_changes)

        # 5. seq = changelog 行数；registry 结构字段齐
        expect("seq = changelog 行数", registry["seq"] == 2)
        expect("registry 带 schema_version", registry["schema_version"] == 1)

        # 6. XML 可解析且条目数对
        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml)
        expect("intel.xml 可解析",
               root.tag == "rss"
               and len(root.findall("./channel/item"))
               == 1 + len(intel["changes"]))

    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER,
                        help="账本目录（默认仓库根 ledger/）")
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[2] / "render",
                        help="产物输出目录（默认仓库根 render/）")
    parser.add_argument("--selftest", action="store_true",
                        help="临时账本上自测渲染，不碰真实账本")
    args = parser.parse_args()

    if args.selftest:
        print("自测开始（临时账本 + 真实 schema.json）…")
        passed = _selftest()
        print("自测通过" if passed else "自测失败")
        return 0 if passed else 1

    ledger = args.ledger.resolve()
    errors = validate_ledger(ledger)
    if errors:
        print(f"账本校验失败，{len(errors)} 个错误（拒绝渲染）：")
        for e in errors:
            print(f"  - {e}")
        return 1
    stats = render_all(ledger, args.out.resolve())
    print(f"渲染完成：{args.out.resolve()}（vendors={stats['vendors']}，"
          f"activities={stats['activities']}，changes={stats['changes']}）")
    print("  registry.json / intel.json / intel.xml（签名由 T1.3 CI 流水线附加）")
    return 0


if __name__ == "__main__":
    sys.exit(main())

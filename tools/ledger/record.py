#!/usr/bin/env python3
"""录入账本：三个动作，每次自动追加一行变更日志，录完自动校验。

    python tools/ledger/record.py vendor-add   --id ... --name ... --region cn ...
    python tools/ledger/record.py activity-add --vendor ... --slug ... --title ...
    python tools/ledger/record.py quota-set    --vendor ... --model ... --tokens ...
    python tools/ledger/record.py --selftest  # 临时账本上跑三个动作 + 验收断言

约定（与 validate.py / schema.json 一致）：
  * 文件名 = id；活动 id 自动生成 act-<今日>-<vendor>-<slug>；
  * 时间戳统一 +08:00；sources 至少一条（schema required）；
  * quota-set 只改已登记模型的 per_model.tokens；值没变不写日志；
  * 每个动作写盘后立即 validate_ledger，账本坏了 exit 1 不留半截状态。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from validate import DEFAULT_LEDGER, validate_ledger  # noqa: E402

CST = timezone(timedelta(hours=8))
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def now_iso() -> str:
    return datetime.now(CST).replace(microsecond=0).isoformat()


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, doc) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def append_changelog(ledger: Path, entry: dict) -> None:
    line = json.dumps(entry, ensure_ascii=False)
    with (ledger / "changelog.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def check(ledger: Path) -> None:
    errors = validate_ledger(ledger)
    if errors:
        print(f"账本校验失败，{len(errors)} 个错误（本次录入未生效完整，请检查）：")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)


def parse_kv(pairs: list[str], metavar: str):
    """protocol=url / model:tier / model=tokens[:date] → dict。"""
    out = {}
    for item in pairs:
        if "=" in item:
            k, v = item.split("=", 1)
        elif ":" in item:
            k, v = item.split(":", 1)
        else:
            raise argparse.ArgumentTypeError(f"{metavar} 格式应为 key=value：{item!r}")
        if k in out:
            raise argparse.ArgumentTypeError(f"{metavar} 键重复：{k}")
        out[k] = v
    return out


# ---------------------------------------------------------------------------
# 动作一：加厂家
# ---------------------------------------------------------------------------

def cmd_vendor_add(args: argparse.Namespace, ledger: Path) -> None:
    vid = args.id
    if not SLUG_RE.match(vid):
        sys.exit(f"厂家 id 不合法（应为小写字母/数字/连字符）：{vid!r}")
    path = ledger / "vendors" / f"{vid}.json"
    if path.exists():
        sys.exit(f"厂家已存在：{path}（改额度用 quota-set）")

    endpoints = parse_kv(args.endpoint, "--endpoint")
    if not endpoints:
        sys.exit("--endpoint 至少给一个（protocol=url）")
    models = []
    for spec in args.model or []:
        mid, _, tier = spec.partition(":")
        models.append({"id": mid, "tier": tier or "paid"})
    per_model = {}
    for spec in args.quota or []:
        mid, _, rest = spec.partition("=")
        tokens_s, _, expires = rest.partition(":")
        per_model[mid] = {
            "tokens": int(tokens_s),
            "expires": expires or None,
        }

    doc = {
        "id": vid,
        "name": args.name,
        "region": args.region,
        "endpoints": endpoints,
        "auth": {"type": args.auth},
        "free_quota": {"per_model": per_model},
        "models": models,
        "notes": args.note or "",
        "verified_at": now_iso(),
        "sources": args.sources,
    }
    write_json(path, doc)
    append_changelog(ledger, {
        "ts": doc["verified_at"], "vendor": vid,
        "kind": "vendor_added", "field": "$", "ref": None,
    })
    print(f"已录入厂家 {vid}（{len(models)} 个模型）")
    check(ledger)


# ---------------------------------------------------------------------------
# 动作二：加活动
# ---------------------------------------------------------------------------

def cmd_activity_add(args: argparse.Namespace, ledger: Path) -> None:
    vendor_path = ledger / "vendors" / f"{args.vendor}.json"
    if not vendor_path.is_file():
        sys.exit(f"厂家不存在：{args.vendor}（先 vendor-add）")
    if not SLUG_RE.match(args.slug):
        sys.exit(f"活动 slug 不合法：{args.slug!r}")

    act_id = f"act-{datetime.now(CST):%Y%m%d}-{args.vendor}-{args.slug}"
    path = ledger / "activities" / f"{act_id}.json"
    if path.exists():
        sys.exit(f"活动已存在：{path}")

    grant: dict = {
        "type": args.grant_type,
        "model": args.model or "",
        "tokens": args.tokens,
    }
    if args.method:
        grant["method"] = args.method

    doc = {
        "id": act_id,
        "vendor": args.vendor,
        "title": args.title,
        "grant": grant,
        "window": {"start": args.start, "end": args.end or None},
        "action": args.action,
        "status": args.status,
        "discovered_at": now_iso(),
        "sources": args.sources,
    }
    if args.verified_at:
        doc["verified_at"] = args.verified_at
    if args.evidence:
        doc["evidence"] = args.evidence
    write_json(path, doc)
    append_changelog(ledger, {
        "ts": doc["discovered_at"], "vendor": args.vendor,
        "kind": "activity_added", "field": act_id, "ref": act_id,
    })
    print(f"已录入活动 {act_id}")
    check(ledger)


# ---------------------------------------------------------------------------
# 动作三：改额度
# ---------------------------------------------------------------------------

def cmd_quota_set(args: argparse.Namespace, ledger: Path) -> None:
    path = ledger / "vendors" / f"{args.vendor}.json"
    if not path.is_file():
        sys.exit(f"厂家不存在：{args.vendor}")
    doc = read_json(path)
    known = {m["id"] for m in doc.get("models", [])}
    if args.model not in known:
        sys.exit(f"模型未登记：{args.model}（先 vendor-add 并 --model 登记）")

    per_model = doc.setdefault("free_quota", {}).setdefault("per_model", {})
    old = per_model.get(args.model, {}).get("tokens")
    new = args.tokens
    if old == new:
        print(f"额度未变化（{args.model} 已是 {new}），不写变更日志")
        return

    per_model.setdefault(args.model, {})["tokens"] = new
    write_json(path, doc)
    append_changelog(ledger, {
        "ts": now_iso(), "vendor": args.vendor,
        "kind": "quota_changed",
        "field": f"free_quota.per_model.{args.model}.tokens",
        "from": old, "to": new, "ref": None,
    })
    print(f"已更新 {args.vendor} / {args.model} 额度：{old} → {new}")
    check(ledger)


# ---------------------------------------------------------------------------
# 自测：临时账本上跑三个动作，断言 changelog 行数与内容（验收标准）
# ---------------------------------------------------------------------------

def _selftest() -> bool:
    import shutil
    import tempfile

    ok = True

    with tempfile.TemporaryDirectory() as tmp:
        ledger = Path(tmp)
        shutil.copy(DEFAULT_LEDGER / "schema.json", ledger / "schema.json")
        (ledger / "vendors").mkdir()

        # 预先放一家厂家，供 activity-add / quota-set 使用
        acme = {
            "id": "acme", "name": "Acme AI", "region": "cn",
            "endpoints": {"openai": "https://api.acme.example/v1"},
            "auth": {"type": "bearer"},
            "free_quota": {"per_model": {"acme-lite": {"tokens": 1000000,
                                                        "expires": None}}},
            "models": [{"id": "acme-lite", "tier": "free"}],
            "verified_at": "2026-11-27T12:00:00+08:00",
            "sources": ["https://acme.example/pricing"],
        }
        write_json(ledger / "vendors" / "acme.json", acme)

        # 1. 加厂家
        ns = argparse.Namespace(
            id="beta", name="Beta Cloud", region="global",
            endpoint=["openai=https://api.beta.example/v1"],
            auth="bearer", model=["beta-pro:paid"], quota=[],
            note="自测", sources=["https://docs.beta.example"],
        )
        before = changelog_lines(ledger)
        cmd_vendor_add(ns, ledger)
        after = changelog_lines(ledger)
        entry = after[-1]
        expect = (len(after) == len(before) + 1
                  and entry["vendor"] == "beta"
                  and entry["kind"] == "vendor_added"
                  and entry["field"] == "$" and entry["ref"] is None)
        print(("ok   " if expect else "FAIL ") + "vendor-add changelog")
        ok &= expect
        ok &= not validate_ledger(ledger)

        # 2. 加活动
        ns = argparse.Namespace(
            vendor="acme", slug="launch", title="0 元购体验包",
            grant_type="activity_package", model="acme-lite", tokens=5000000,
            start="2026-11-27", end=None, action="控制台 → 0 元购买",
            status="discovered", verified_at=None,
            method="控制台领取", sources=["https://acme.example/news"],
            evidence=None,
        )
        before = changelog_lines(ledger)
        cmd_activity_add(ns, ledger)
        after = changelog_lines(ledger)
        entry = after[-1]
        act_id = entry["field"]
        expect = (len(after) == len(before) + 1
                  and entry["kind"] == "activity_added"
                  and entry["ref"] == act_id
                  and act_id.startswith("act-")
                  and (ledger / "activities" / f"{act_id}.json").is_file())
        print(("ok   " if expect else "FAIL ") + "activity-add changelog")
        ok &= expect
        ok &= not validate_ledger(ledger)

        # 3. 改额度
        ns = argparse.Namespace(vendor="acme", model="acme-lite", tokens=500000)
        before = changelog_lines(ledger)
        cmd_quota_set(ns, ledger)
        after = changelog_lines(ledger)
        entry = after[-1]
        expect = (len(after) == len(before) + 1
                  and entry["kind"] == "quota_changed"
                  and entry["field"] == "free_quota.per_model.acme-lite.tokens"
                  and entry["from"] == 1000000 and entry["to"] == 500000
                  and entry["ref"] is None
                  and read_json(ledger / "vendors" / "acme.json")
                     ["free_quota"]["per_model"]["acme-lite"]["tokens"] == 500000)
        print(("ok   " if expect else "FAIL ") + "quota-set changelog")
        ok &= expect
        ok &= not validate_ledger(ledger)

        # 4. 额度没变不写日志
        before = changelog_lines(ledger)
        cmd_quota_set(ns, ledger)
        expect = changelog_lines(ledger) == before
        print(("ok   " if expect else "FAIL ") + "quota-set 无变化不写日志")
        ok &= expect

        # 5. 重复加厂家被拒
        dup = argparse.Namespace(
            id="beta", name="dup", region="cn",
            endpoint=["openai=https://x.example/v1"], auth="bearer",
            model=[], quota=[], note="", sources=["https://x.example"])
        try:
            cmd_vendor_add(dup, ledger)
            expect = False
        except SystemExit as exc:
            expect = "已存在" in str(exc)
        print(("ok   " if expect else "FAIL ") + "vendor-add 重复被拒")
        ok &= expect

    return ok


def changelog_lines(ledger: Path) -> list[dict]:
    path = ledger / "changelog.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8")
            .splitlines() if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER,
                        help="账本目录（默认仓库根 ledger/）")
    parser.add_argument("--selftest", action="store_true",
                        help="临时账本上自测三个动作，不碰真实账本")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("vendor-add", help="录入新厂家")
    p.add_argument("--id", required=True, help="厂家 id（小写/数字/连字符，= 文件名）")
    p.add_argument("--name", required=True)
    p.add_argument("--region", required=True, choices=["cn", "global"])
    p.add_argument("--endpoint", action="append",
                   metavar="protocol=url", help="可重复，如 openai=https://.../v1")
    p.add_argument("--auth", default="bearer",
                   choices=["bearer", "api_key", "oauth2", "none"])
    p.add_argument("--model", action="append", metavar="id[:tier]",
                   help="登记模型，可重复；tier 默认 paid")
    p.add_argument("--quota", action="append", metavar="model=tokens[:expires]",
                   help="免费额度，可重复；expires 省略 = 长期")
    p.add_argument("--note", default="")
    p.add_argument("--sources", action="append", required=True,
                   metavar="URL", help="证据来源，至少一条（可重复）")

    p = sub.add_parser("activity-add", help="录入活动")
    p.add_argument("--vendor", required=True, help="已存在的厂家 id")
    p.add_argument("--slug", required=True, help="活动短标识（拼进活动 id）")
    p.add_argument("--title", required=True)
    p.add_argument("--grant-type", default="activity_package",
                   choices=["per_model", "activity_package"])
    p.add_argument("--model", help="适用模型")
    p.add_argument("--tokens", type=int, required=True)
    p.add_argument("--start", default=datetime.now(CST).strftime("%Y-%m-%d"))
    p.add_argument("--end", help="结束日 YYYY-MM-DD；省略 = 长期有效")
    p.add_argument("--action", required=True, help="领取动作")
    p.add_argument("--status", default="discovered",
                   choices=["discovered", "verifying", "verified",
                            "published", "expired", "retracted"])
    p.add_argument("--verified-at", default=None,
                   help="核实时间；discovered 状态留空")
    p.add_argument("--method", help="领取方式一句话")
    p.add_argument("--evidence", help="证据文件相对路径")
    p.add_argument("--sources", action="append", required=True,
                   metavar="URL", help="证据来源，至少一条（可重复）")

    p = sub.add_parser("quota-set", help="改已登记模型的免费额度")
    p.add_argument("--vendor", required=True)
    p.add_argument("--model", required=True, help="须已登记于该厂家 models")
    p.add_argument("--tokens", type=int, required=True)

    args = parser.parse_args()
    if not args.selftest and args.cmd is None:
        parser.print_usage(sys.stderr)
        print("record.py: error: 需要子命令 vendor-add / activity-add / quota-set，"
              "或 --selftest", file=sys.stderr)
        return 2
    ledger = args.ledger.resolve()

    if args.selftest:
        print("自测开始（临时账本 + 真实 schema.json）…")
        passed = _selftest()
        print("自测通过" if passed else "自测失败")
        return 0 if passed else 1

    {"vendor-add": cmd_vendor_add,
     "activity-add": cmd_activity_add,
     "quota-set": cmd_quota_set}[args.cmd](args, ledger)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""账本 → 文档统计块：由 ledger/ 重算三份文档里的统计数字，不再手改。

    python tools/ledger/gen_docs.py            # 重新生成（先校验账本）
    python tools/ledger/gen_docs.py --selftest # 临时账本上验证生成规则

文档里的统计数字包在标记块内（HTML 注释，Markdown 渲染不可见）：

    <!-- ledger-stats:start -->
    ...由本脚本生成的内容...
    <!-- ledger-stats:end -->

脚本只重写标记区间，区间外的手工文案（宣传口径、修订记录）一律不动。
口径（与免费额度总览第六节一致）：
  * 保守合计 = 每家只算登记额最高的一个模型（per_model + 已核实活动包）；
  * 领满合计 = 全部已登记模型额度之和（登记口径，不含「每模型各领一份」
    中账本未逐模型细化的部分——宣传口径见总览第六节）；
  * 免费厂家 = 有 per_model 额度、有免费模型或有已核实活动包；
  * 预置渠道数读 seed_channels.json（与账本强一致）。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gen_seed import DEFAULT_LEDGER, load_ledger  # noqa: E402
from validate import validate_ledger  # noqa: E402

DOCS = Path(__file__).resolve().parents[2] / "docs"
README = Path(__file__).resolve().parents[2] / "README.md"
SEED_CHANNELS = (Path(__file__).resolve().parents[2]
                 / "src" / "facet" / "gateway" / "seed_channels.json")

START = "<!-- ledger-stats:start -->"
END = "<!-- ledger-stats:end -->"

CST = timezone(timedelta(hours=8))


def active_activities(activities: list[dict]) -> list[dict]:
    """用于统计的活动：status in verified/published（与 gen_seed 同口径）。"""
    return [a for a in activities if a.get("status") in ("verified", "published")]


def model_tokens(vendor: dict, acts_by_vendor: dict[str, list[dict]]) -> dict[str, int]:
    """单模型登记额度：per_model 长期 + 该模型名下已核实活动包。"""
    merged: dict[str, int] = {}
    for model, quota in ((vendor.get("free_quota") or {}).get("per_model")
                         or {}).items():
        if quota.get("tokens"):
            merged[model] = merged.get(model, 0) + quota["tokens"]
    for act in acts_by_vendor.get(vendor["id"], []):
        grant = act.get("grant") or {}
        model, tokens = grant.get("model"), grant.get("tokens")
        if model and tokens:
            merged[model] = merged.get(model, 0) + tokens
    return merged


def compute(book_vendors: list[dict], activities: list[dict]) -> dict:
    vendors = book_vendors  # load_ledger 已过滤 status != active
    acts_by_vendor: dict[str, list[dict]] = {}
    for act in active_activities(activities):
        acts_by_vendor.setdefault(act["vendor"], []).append(act)

    free_vendors, free_models, conservative, full = [], 0, 0, 0
    for vendor in vendors:
        tokens = model_tokens(vendor, acts_by_vendor)
        has_free_model = any(m.get("tier") == "free"
                             for m in vendor.get("models") or [])
        if tokens or has_free_model or vendor["id"] in acts_by_vendor:
            free_vendors.append(vendor["id"])
        free_models += sum(1 for m in vendor.get("models") or []
                           if m.get("tier") == "free")
        if tokens:
            conservative += max(tokens.values())
            full += sum(tokens.values())
    return {
        "vendors": len(vendors),
        "free_vendors": len(free_vendors),
        "free_models": free_models,
        "conservative": conservative,
        "full": full,
    }


def fmt_tokens(tokens: int) -> str:
    """1.12e9 → 约 11.2 亿；其余按 万/千。"""
    if tokens >= 1e8:
        return f"约 {tokens / 1e8:.1f} 亿"
    if tokens >= 1e4:
        return f"约 {tokens / 1e4:.0f} 万"
    return f"{tokens:,}"


def read_seed_stats() -> dict:
    """预置渠道统计（读 gen_seed 的产物，与账本强一致）。"""
    try:
        text = SEED_CHANNELS.read_text(encoding="utf-8")
        payload = json.loads("\n".join(
            l for l in text.splitlines() if not l.startswith("//")))
    except (OSError, json.JSONDecodeError):
        return {"channels": 0, "free": 0}
    channels = payload.get("channels", [])
    return {"channels": len(channels),
            "free": sum(1 for c in channels if c.get("tier") == "free")}


# ---------------------------------------------------------------------------
# 各文档的统计块内容
# ---------------------------------------------------------------------------

def render_overview(stats: dict, generated_at: str) -> str:
    return (
        f"Facet 内置 **{stats['vendors']} 家主流大模型厂商**（账本 active），"
        f"其中 **{stats['free_vendors']} 家**登记了免费额度或长期免费模型："
        f"一次性赠送按**账本登记口径**合计 **{fmt_tokens(stats['conservative'])} tokens**"
        f"（保守口径，每家只算登记额最高的一个模型），把「每模型各领一份」"
        f"全部领满 **{fmt_tokens(stats['full'])} tokens**（登记口径）；"
        f"另有每日循环额度与 **{stats['free_models']} 个长期免费模型**，"
        f"不占 token 总量。\n\n"
        f"> 本段由 [tools/ledger/gen_docs.py](../tools/ledger/gen_docs.py) 于 "
        f"{generated_at} 从 ledger/ 生成；宣传口径（合计约 11.5 亿、领满约 "
        f"16.6 亿）含登记表未逐模型细化的「每模型各领一份」额度，见第六节。"
    )


def render_key_guide(stats: dict, generated_at: str) -> str:
    return (
        f"> 账本登记 **{stats['vendors']} 家**（active）："
        f"**{stats['free_vendors']} 家**有免费额度/免费模型，"
        f"**{stats['free_models']} 个**长期免费模型；一次性赠送登记合计："
        f"保守 **{fmt_tokens(stats['conservative'])} tokens**（每家只算最高"
        f"单模型）、领满（登记口径）**{fmt_tokens(stats['full'])} tokens**。"
        f"统计由 ledger/ 自动生成（tools/ledger/gen_docs.py，{generated_at}）。"
    )


def render_readme(seed: dict, generated_at: str) -> str:
    return (
        f"  预置渠道 **{seed['channels']}** 个（其中 **{seed['free']} 个**带"
        f"免费额度），由 ledger/ 自动生成"
        f"（tools/ledger/gen_seed.py，{generated_at}）。"
    )


# ---------------------------------------------------------------------------
# 标记区间重写
# ---------------------------------------------------------------------------

def update_doc(path: Path, block: str) -> bool:
    """把 path 中 START/END 标记区间的内容替换为 block；找不到标记或
    文件缺失则不动（返回 False）。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    start, end = text.find(START), text.find(END)
    if start < 0 or end < start:
        return False
    path.write_text(text[:start] + START + "\n" + block + "\n" + END
                    + text[end + len(END):], encoding="utf-8")
    return True


def generate_all(ledger: Path) -> dict:
    vendors, activities = load_ledger(ledger)
    stats = compute(vendors, activities)
    generated_at = datetime.now(CST).replace(microsecond=0).isoformat()

    overview = render_overview(stats, generated_at)
    key_guide = render_key_guide(stats, generated_at)
    readme = render_readme(read_seed_stats(), generated_at)

    # 幂等：块内容不含 START/END 字面量，重写后区间稳定
    for block in (overview, key_guide, readme):
        assert START not in block and END not in block

    results = {}
    for name, path, block in (
        ("免费额度总览.md", DOCS / "免费额度总览.md", overview),
        ("api-key-guide.md", DOCS / "api-key-guide.md", key_guide),
        ("README.md", README, readme),
    ):
        results[name] = update_doc(path, block)
    return {**stats, "docs": results, "generated_at": generated_at}


# ---------------------------------------------------------------------------
# 自测：临时账本 + 临时文档上验证生成规则
# ---------------------------------------------------------------------------

def _selftest() -> bool:
    import tempfile

    ok = True

    def expect(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        print(("ok   " if cond else "FAIL ") + name
              + (f"（{detail}）" if detail and not cond else ""))
        ok &= cond

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        ledger = tmp / "ledger"
        (ledger / "vendors").mkdir(parents=True)
        (ledger / "activities").mkdir()
        vendor = {
            "id": "acme", "name": "Acme AI", "region": "cn",
            "endpoints": {"openai": "https://api.acme.example/v1"},
            "auth": {"type": "bearer"},
            "free_quota": {"per_model": {
                "acme-lite": {"tokens": 1000000, "expires": None},
                "acme-pro": {"tokens": 5000000, "expires": None}}},
            "models": [{"id": "acme-lite", "tier": "free"},
                       {"id": "acme-pro", "tier": "paid"}],
            "notes": "", "verified_at": "2026-11-27T12:00:00+08:00",
            "sources": ["https://acme.example/pricing"],
        }
        (ledger / "vendors" / "acme.json").write_text(
            json.dumps(vendor, ensure_ascii=False), encoding="utf-8")
        activity = {
            "id": "act-1", "vendor": "acme", "title": "体验包",
            "grant": {"type": "activity_package", "model": "acme-lite",
                      "tokens": 9000000, "method": "控制台领取"},
            "window": {"start": "2026-11-27", "end": None},
            "action": "控制台领取", "status": "verified",
            "discovered_at": "2026-11-27T12:00:00+08:00",
            "verified_at": "2026-11-27T13:00:00+08:00",
            "sources": ["https://acme.example/news"], "evidence": "",
        }
        (ledger / "activities" / "act-1.json").write_text(
            json.dumps(activity, ensure_ascii=False), encoding="utf-8")

        docs = tmp / "docs"
        docs.mkdir()
        overview = docs / "免费额度总览.md"
        overview.write_text(
            "## 一句话\n\n"
            "旧的手工统计句子，应被替换。\n\n"
            + START + "\n旧块内容\n" + END + "\n", encoding="utf-8")
        key_guide = docs / "api-key-guide.md"
        key_guide.write_text(
            "## 二、厂家总览\n\n"
            + START + "\n旧块内容\n" + END + "\n", encoding="utf-8")

        # 1. 统计口径：保守 = 每家 max 单模型（100 万 + 900 万活动 = 1000 万）
        vendors, activities = load_ledger(ledger)
        stats = compute(vendors, activities)
        expect("保守 = 1,000 万（max(100万, 100万+900万)）",
               stats["conservative"] == 10000000, str(stats))
        expect("领满 = 1,500 万（100+500+900）",
               stats["full"] == 15000000, str(stats))
        expect("免费厂家 = 1、免费模型 = 1",
               stats["free_vendors"] == 1 and stats["free_models"] == 1,
               str(stats))

        # 2. 数字格式化
        expect("1.12e9 → 约 11.2 亿", fmt_tokens(1123500000) == "约 11.2 亿")
        expect("500 万 → 约 500 万", fmt_tokens(5000000) == "约 500 万")

        # 3. 标记区间重写：区间内替换、区间外不动、幂等
        global DOCS, README
        saved_docs, saved_readme = DOCS, README
        DOCS, README = docs, docs / "README.md"
        try:
            README.write_text(
                "- 首次启动自动种子化厂家预置渠道（均为禁用态）。\n\n"
                + START + "\n旧块内容\n" + END + "\n", encoding="utf-8")
            result = generate_all(ledger)
            expect("三份文档都重写成功",
                   all(result["docs"].values()), str(result["docs"]))
            text = overview.read_text(encoding="utf-8")
            expect("区间外手工文案保留、区间内已替换",
                   text.startswith("## 一句话")
                   and "旧的手工统计句子" in text
                   and "旧块内容" not in text)
            expect("块内数字正确（22→1 家、11.2 亿→1000 万）",
                   "**1 家**登记了免费额度" in text
                   and "约 1000 万 tokens" in text)
            expect("密钥手册块写入统计句",
                   "账本登记 **1 家**" in key_guide.read_text(encoding="utf-8"))
            before = text
            generate_all(ledger)  # 幂等重跑
            expect("重跑幂等（内容不变）", text == before)
        finally:
            DOCS, README = saved_docs, saved_readme

    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--ledger", type=Path, default=DEFAULT_LEDGER,
                        help="账本目录（默认仓库根 ledger/）")
    parser.add_argument("--selftest", action="store_true",
                        help="临时账本上自测，不碰真实文档")
    args = parser.parse_args()

    if args.selftest:
        print("自测开始（临时账本 + 临时文档）…")
        passed = _selftest()
        print("自测通过" if passed else "自测失败")
        return 0 if passed else 1

    ledger = args.ledger.resolve()
    errors = validate_ledger(ledger)
    if errors:
        print(f"账本校验失败，{len(errors)} 个错误（拒绝生成）：")
        for e in errors:
            print(f"  - {e}")
        return 1
    result = generate_all(ledger)
    print(f"文档统计块已生成（{result['generated_at']}）：")
    print(f"  厂家 {result['vendors']}（{result['free_vendors']} 家有免费额度）、"
          f"免费模型 {result['free_models']}、"
          f"保守 {result['conservative']:,}、领满 {result['full']:,}")
    for name, done in result["docs"].items():
        print(f"  {'ok  ' if done else 'MISS'} {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

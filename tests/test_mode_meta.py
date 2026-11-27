"""mode_meta 单一分类实现点的回归测试。

设计文档：docs/免费与接入模式统一建模设计.md。第二步（本阶段）注册表
32 个 mode 已全部声明显式 ``free_kind``（口径 = PPT 四类 + 主类规则
forever > daily > one_time > voucher），下面的 32-mode 锁定表锁的就是
显式分类结果；推断规则只为用户自定义厂家兜底。

与第一步推断值不同的修正（均以 PPT 为准）：
- zhipu/openrouter/opencode/zai payg → forever（长期免费模型）；
- nvidia payg → daily（PPT「每日循环」表收录 NVIDIA NIM）；
- moonshot/xiaomi/anthropic payg → voucher（注册代金券，不推断）；
- minimax：PPT 无任何免费政策 → none（第一步文档「¥14 代金券」系误读，
  ¥14 是硅基流动的一次性体验金，已文档化在 siliconflow 的 note 里）。
"""
from __future__ import annotations

from facet.gateway import (bailian_quota, deepseek_quota, hunyuan_quota,
                           minimax_quota, modelscope_quota, moonshot_quota,
                           openrouter_quota, zhipu_quota)
from facet.gateway import mode_meta, providers as providers_mod
from facet.gateway.mode_meta import (
    CHECKIN_HANDLERS, KEY_API_FETCHERS, MGMT_WALLET_FETCHERS,
    catalog_free_pool, catalog_is_mixed, cred_kind_of, free_kind_of,
    free_kinds_of, mode_meta as mode_meta_of, quota_capability_of,
    requires_checkin_of,
)

# ── 32-mode 显式分类锁定表（第二步：注册表全部声明显式 free_kind）──────
# (free_kind, free_kinds, cred_kind, requires_checkin, quota_capability)
EXPECTED = {
    "volcengine/coding": ("none", ["none"], "aksk", False, "none"),
    "volcengine/agent": ("none", ["none"], "aksk", False, "none"),
    "volcengine/payg": ("one_time", ["one_time"], "aksk", False, "mgmt_api"),
    "bailian/payg": ("one_time", ["one_time"], "aksk", False, "mgmt_api"),
    # bailian 在 MGMT_WALLET_FETCHERS（provider 级）：agent 模式也能查钱包。
    "bailian/agent": ("none", ["none"], "key", False, "mgmt_api"),
    "zhipu/coding": ("none", ["none"], "key", False, "key_api"),
    # GLM Flash 系列长期免费 → forever（PPT「长期免费」表）。
    "zhipu/payg": ("forever", ["forever"], "key", False, "key_api"),
    "modelscope/payg": ("daily", ["daily"], "key", True, "key_api"),
    "siliconflow/payg": ("forever", ["forever"], "key", False, "mgmt_api"),
    # hunyuan 在 MGMT_WALLET_FETCHERS（provider 级）：agent 模式也能查钱包。
    "hunyuan/agent": ("none", ["none"], "key", False, "mgmt_api"),
    # 一次性赠额 + 44 个长期免费模型：主类 one_time、全集叠加 forever。
    "hunyuan/payg": ("one_time", ["one_time", "forever"], "aksk", False,
                     "mgmt_api"),
    "qianfan/agent": ("none", ["none"], "key", False, "none"),
    # 有 free_quota 但无查询通道（无 cmd、不在 fetcher 表）→ none。
    "qianfan/payg": ("one_time", ["one_time"], "key", False, "none"),
    # 注册代金券三家：显式声明（推断永不产出 voucher）。
    "moonshot/payg": ("voucher", ["voucher"], "key", False, "key_api"),
    "xiaomi/payg": ("voucher", ["voucher"], "key", False, "none"),
    "xiaomi/agent": ("none", ["none"], "key", False, "none"),
    "deepseek/payg": ("none", ["none"], "key", False, "key_api"),
    # PPT 无任何免费政策（¥14 体验金是硅基流动的，第一步文档误读已修正）。
    "minimax/payg": ("none", ["none"], "key", False, "key_api"),
    "minimax/agent": ("none", ["none"], "key", False, "key_api"),
    "teleai/payg": ("one_time", ["one_time"], "key", False, "none"),
    # 免费层速率随模型浮动 → daily（PPT「每日循环」表）。
    "google/payg": ("daily", ["daily"], "key", False, "none"),
    # 主类 forever（25+ 个 :free 模型）；副类 daily 来自 rpd:50 文档限额。
    "openrouter/payg": ("forever", ["forever", "daily"], "key", False,
                        "key_api"),
    # PPT「每日循环」表收录 NVIDIA NIM（约 40 RPM 无期限）→ daily。
    "nvidia/payg": ("daily", ["daily"], "key", False, "none"),
    "llm7/payg": ("daily", ["daily"], "key", False, "none"),
    "opencode/payg": ("forever", ["forever"], "key", False, "none"),
    "opencode/agent": ("none", ["none"], "key", False, "none"),
    "commandcode/agent": ("daily", ["daily"], "key", False, "none"),
    "openai/payg": ("none", ["none"], "key", False, "none"),
    "anthropic/payg": ("voucher", ["voucher"], "key", False, "none"),
    "xai/payg": ("none", ["none"], "key", False, "none"),
    "zai/payg": ("forever", ["forever"], "key", False, "none"),
}


def _builtin_modes():
    for entry in providers_mod.load_providers():
        pid = str(entry.get("id") or "")
        for key, mode in (entry.get("modes") or {}).items():
            yield pid, key, mode


def test_builtin_modes_inference_locked():
    """31 个 mode 的推断结果逐项锁定；注册表增删 mode 时必须同步此表。"""
    seen = set()
    for pid, key, mode in _builtin_modes():
        name = f"{pid}/{key}"
        seen.add(name)
        expected = EXPECTED[name]
        got = mode_meta_of(pid, mode)
        assert (got["free_kind"], got["free_kinds"], got["cred_kind"],
                got["requires_checkin"], got["quota_capability"]) == expected, name
    assert seen == set(EXPECTED), (
        "providers.json 的 mode 集合变了，请同步更新 EXPECTED 锁定表")


# ── 注册表搬家回归：与 server.py 原硬编码表逐项一致 ────────────────────

def test_key_api_fetchers_match_original_server_table():
    assert KEY_API_FETCHERS == {
        "openrouter": openrouter_quota.fetch_key_usage,
        "deepseek": deepseek_quota.fetch_balance,
        "zhipu": zhipu_quota.fetch_account_report,
        "minimax": minimax_quota.fetch_usage,
        "moonshot": moonshot_quota.fetch_balance,
        "modelscope": modelscope_quota.fetch_balance,
    }


def test_mgmt_wallet_fetchers_match_original_server_table():
    assert MGMT_WALLET_FETCHERS == {
        "bailian": (bailian_quota.fetch_balance,
                    bailian_quota.AK_ENV, bailian_quota.SK_ENV),
        "hunyuan": (hunyuan_quota.fetch_balance,
                    hunyuan_quota.ID_ENV, hunyuan_quota.KEY_ENV),
    }


def test_checkin_handlers_registry():
    assert CHECKIN_HANDLERS == {"modelscope": modelscope_quota.checkin}


def test_server_no_longer_defines_fetcher_tables():
    """server.py 的旧表名必须删净，防止出现第二份事实源。"""
    import facet.gateway.server as server_mod
    assert not hasattr(server_mod, "CHANNEL_USAGE_FETCHERS")
    assert not hasattr(server_mod, "MGMT_WALLET_FETCHERS")


# ── 显式字段优先于推断 ──────────────────────────────────────────────────

def test_explicit_free_kind_wins():
    mode = {"tier": "paid", "free_limits": {"note": "赠金"},
            "free_kind": "voucher"}
    assert free_kind_of("moonshot", mode) == "voucher"
    assert free_kinds_of("moonshot", mode) == ["voucher"]


def test_voucher_is_never_inferred():
    """代金券只认显式声明：仅有 free_limits 时推断为 one_time，不是 voucher。"""
    assert free_kind_of("moonshot", {"free_limits": {"note": "代金券"}}) == "one_time"
    assert "voucher" not in free_kinds_of("moonshot",
                                          {"free_limits": {"note": "代金券"}})


def test_explicit_cred_kind_and_fallback():
    assert cred_kind_of({"cred_kind": "aksk", "credential_envs": []}) == "aksk"
    assert cred_kind_of({"cred_kind": "key",
                         "credential_envs": [{"env": "X"}]}) == "key"
    # 推断：credential_envs 非空 → aksk。
    assert cred_kind_of({"credential_envs": [{"env": "AK"}, {"env": "SK"}]}) == "aksk"
    assert cred_kind_of({}) == "key"


def test_explicit_requires_checkin_wins():
    assert requires_checkin_of("openai", {"requires_checkin": True}) is True
    assert requires_checkin_of("openai", {"requires_checkin": False}) is False
    # 第二步删除了「provider==modelscope 即签到」的兜底：无声明 → 不签到，
    # 魔搭需要签到完全由注册表显式字段表达。
    assert requires_checkin_of("modelscope", {}) is False
    assert requires_checkin_of("modelscope",
                               {"requires_checkin": True}) is True


def test_explicit_quota_capability_wins():
    assert quota_capability_of("openai", {"quota_capability": "key_api"}) == "key_api"
    assert quota_capability_of("bailian", {"quota_capability": "none"}) == "none"


# ── 推断规则细节 ────────────────────────────────────────────────────────

def test_wizard_has_free_equivalence_on_builtin_registry():
    """第一步铁律的证明：meta.free_kind 与向导旧版四信号口径在全部内置
    mode 上完全等价（wizardHasFree 换用 meta 后行为不变）。"""
    for pid, key, mode in _builtin_modes():
        legacy = bool(mode.get("tier") == "free" or mode.get("free_quota")
                      or mode.get("free_limits") or mode.get("free_models"))
        assert (free_kind_of(pid, mode) != "none") == legacy, f"{pid}/{key}"


def test_daily_quota_period_maps_to_daily():
    mode = {"free_quota": {"requests": 50, "period": "day"}}
    assert free_kind_of("anyone", mode) == "daily"
    assert free_kinds_of("anyone", mode) == ["daily"]


def test_free_limits_daily_caps_add_daily_kind():
    """文档化的账户级日量上限（rpd/tpd）是「每日循环」副类信号。

    OpenRouter：主类 forever（:free 模型清单），副类 daily（rpd:50）；
    只有 rpm/tpm 这类无日量的限速不算每日循环。"""
    mode = {"free_kind": "forever", "free_limits": {"rpm": 20, "rpd": 50}}
    assert free_kind_of("openrouter", mode) == "forever"
    assert free_kinds_of("openrouter", mode) == ["forever", "daily"]
    assert free_kinds_of("nvidia", {"free_kind": "daily",
                                    "free_limits": {"rpm": 40}}) == ["daily"]
    assert free_kinds_of("zai", {"free_kind": "forever"}) == ["forever"]


def test_builtin_modes_all_declare_free_kind():
    """第二步完成标志：内置注册表每个 mode 都显式声明 free_kind，且五种
    分类各有代表（新增厂家忘记声明时此测试首先失败）。"""
    declared = set()
    for pid, key, mode in _builtin_modes():
        kind = mode.get("free_kind")
        assert kind in mode_meta.FREE_KINDS, f"{pid}/{key} 缺显式 free_kind"
        declared.add(kind)
    assert declared == set(mode_meta.FREE_KINDS)


def test_wholly_free_tier_falls_back_to_forever():
    """tier=free 且无任何结构化信号：注册表声明整层免费 → forever。

    保证不变量 tier==free ⇒ free_kind != none（向导 wizardHasFree 前后端
    口径一致的前提）。"""
    assert free_kind_of("somevendor", {"tier": "free"}) == "forever"


# ── 目录级 mixed / wholly-free 单点规则 ────────────────────────────────

def test_catalog_is_mixed():
    assert catalog_is_mixed(["a:free", "b"]) is True
    assert catalog_is_mixed(["a-free", "b"]) is True
    assert catalog_is_mixed(["a", "b"]) is False
    assert catalog_is_mixed([]) is False


def test_catalog_free_pool():
    # 混合目录（OpenRouter 式）：只取 :free 子集。
    assert catalog_free_pool(["auto", "a:free", "b:free"]) == ["a:free", "b:free"]
    # 整层免费目录（NIM/豆包式）：全部模型都是候选。
    assert catalog_free_pool(["nvidia/a", "nvidia/b"]) == ["nvidia/a", "nvidia/b"]
    assert catalog_free_pool([]) == []


# ── 签到 Runner 泛化：按注册表调度，而不是写死魔搭 ────────────────────

def test_checkin_runner_dispatches_via_registry():
    """Runner 只调度 requires_checkin 的渠道，执行函数取自 CHECKIN_HANDLERS。

    provider_mode 用真实内置注册表解析：第二步删除 modelscope 兜底后，
    「魔搭需要签到」完全由注册表显式 requires_checkin 表达。"""
    from types import SimpleNamespace

    from facet.gateway import server as server_mod

    registry = {}
    for entry in providers_mod.load_providers():
        for mk, m in (entry.get("modes") or {}).items():
            registry[(str(entry["id"]), mk)] = m

    def fake_channel(name, provider):
        return SimpleNamespace(name=name, provider=provider, mode="payg",
                               enabled=True, api_key=f"key-{name}")

    channels = [fake_channel("魔搭", "modelscope"),
                fake_channel("深度求索", "deepseek"),
                fake_channel("火山", "volcengine")]
    app = SimpleNamespace(
        config=SimpleNamespace(channels=channels),
        provider_mode=lambda provider, mode: registry.get((provider, mode), {}),
    )
    runner = server_mod.ModelScopeCheckinRunner(app)
    with_checkin = runner._channels()
    assert [c.name for c, _h in with_checkin] == ["魔搭"]
    assert all(h is modelscope_quota.checkin for _c, h in with_checkin)


def test_checkin_runner_skips_disabled_channels():
    from types import SimpleNamespace

    from facet.gateway import server as server_mod

    registry = {}
    for entry in providers_mod.load_providers():
        for mk, m in (entry.get("modes") or {}).items():
            registry[(str(entry["id"]), mk)] = m

    channels = [
        SimpleNamespace(name="魔搭启用", provider="modelscope", mode="payg",
                        enabled=True, api_key="k1"),
        SimpleNamespace(name="魔搭停用", provider="modelscope", mode="payg",
                        enabled=False, api_key="k2"),
    ]
    app = SimpleNamespace(
        config=SimpleNamespace(channels=channels),
        provider_mode=lambda provider, mode: registry.get((provider, mode), {}),
    )
    runner = server_mod.ModelScopeCheckinRunner(app)
    assert [c.name for c, _h in runner._channels()] == ["魔搭启用"]

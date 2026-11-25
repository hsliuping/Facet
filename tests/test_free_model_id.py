"""免费模型识别口径的单一实现点回归测试。

历史教训：免费判断曾散落在 server.py / maintenance.py 共 7 处，各自硬编码
`.endswith(":free")`，只认 :free 漏了 -free，与 probe.FREE_ID_RE（认
:free/-free）口径分裂。收敛到 probe.is_free_model 单点后，本测试锁住：
任意入口对同一模型 id 的免费判断必须一致，且 -free 后缀必须被认作免费。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from facet.gateway.probe import FREE_ID_RE, is_free_model
from facet.gateway.server import model_tier


@pytest.mark.parametrize("model, expected", [
    ("nvidia/nemotron-3.5-lightning:free", True),
    ("google/gemma-4-26b-a4b-it:free", True),
    # -free 后缀是这次修复的关键：旧实现 .endswith(":free") 会漏判为付费。
    ("acme/rocket-free", True),
    ("vendor/model-FREE", True),   # 大小写不敏感
    (":free", True),               # 前缀冒号、无模型名也认（后缀规则）
    ("openrouter/auto", False),
    ("openrouter/fusion", False),
    ("aion-labs/aion-2.0", False),
    ("deepseek-chat", False),
    ("free", False),               # 无前缀符号（: 或 -），不算免费后缀
    ("notfree-suffix", False),     # -free 必须是后缀，中间出现不算
    ("model:free:extra", False),   # :free 不在末尾不算
    ("", False),
    (None, False),
    (123, False),
])
def test_is_free_model_single_source_of_truth(model, expected):
    assert is_free_model(model) is expected
    # 单点保证：函数结果必须与底层正则完全一致，没有第二套口径。
    assert is_free_model(model) == bool(FREE_ID_RE.search(str(model or "")))


def _openrouter_channel():
    return SimpleNamespace(provider="openrouter", tiers={}, tier="free")


def test_model_tier_openrouter_recognizes_both_free_suffixes():
    ch = _openrouter_channel()
    # :free 一直认；-free 是修复点——旧代码只 endswith(":free") 会返回 paid。
    assert model_tier(ch, [], "google/gemma-4-26b-a4b-it:free") == "free"
    assert model_tier(ch, [], "acme/rocket-free") == "free"
    assert model_tier(ch, [], "openrouter/auto") == "paid"
    assert model_tier(ch, [], "aion-labs/aion-2.0") == "paid"


def test_model_tier_openrouter_per_model_override_wins():
    ch = SimpleNamespace(provider="openrouter",
                         tiers={"special-free:free": "paid"}, tier="free")
    # 手动档位覆盖优先于后缀推断。
    assert model_tier(ch, [], "special-free:free") == "paid"
    assert model_tier(ch, [], "other:free") == "free"

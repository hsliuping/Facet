"""Playwright E2E 测试：仪表板 (E1)。

E1：打开仪表板 → 摘要卡数值与 /api/state 一致（渠道/可用/请求/Tokens 交叉比对）。
"""
from __future__ import annotations

import pytest

pytest.importorskip("playwright", reason="需要安装 playwright")
from playwright.sync_api import expect


@pytest.mark.smoke
def test_e1_dashboard_summary_matches_api(console, api):
    """[E1] 打开仪表板 → 摘要卡（可用渠道/冷却中/累计请求/累计Tokens）数值与 /api/state 一致。"""
    # 1. 切到仪表板
    console.nav("dashboard")

    # 2. 验证摘要卡片可见
    dash_summary = console.el("#dash-summary")
    expect(dash_summary).to_be_visible()

    # 3. 后端获取状态
    state = api.get("/api/state")
    channels = state.get("channels", [])
    total_channels = len(channels)
    cooling = sum(1 for c in channels if c.get("cooling"))
    blocked = sum(1 for c in channels if c.get("blocked"))
    # 可用 = 启用且配了真实 Key、未冷却未停用（未配 Key 的模板渠道不算）。
    available = sum(1 for c in channels if c.get("enabled") and c.get("configured")
                    and not c.get("cooling") and not c.get("blocked"))
    total_requests = sum(c.get("total_requests", 0) for c in channels)
    total_tokens = sum(c.get("total_tokens", 0) for c in channels)

    # 4. 获取摘要卡片文本并解析
    summary_text = dash_summary.inner_text()

    # 5. 交叉比对各项数值（按实际 DOM 结构中的标签）
    # 可用渠道：格式为 "N / M"
    expected_available = f"{available} / {total_channels}"
    assert expected_available in summary_text, \
        f"可用渠道数 {expected_available} 未在摘要中找到：{summary_text}"

    # 运行健康·渠道状态行：分桶口径与渠道页一致（停用=enabled=false，
    # 封锁=监视自动熔断，未配置=启用但缺 Key）。
    disabled = sum(1 for c in channels if c.get("enabled") is False)
    unconfigured = sum(1 for c in channels if c.get("enabled")
                       and not c.get("configured")
                       and not c.get("cooling") and not c.get("blocked"))
    health_text = console.el("#dash-health").inner_text()
    for fragment in (f"{available} 可用", f"{cooling} 冷却", f"{disabled} 停用"):
        assert fragment in health_text, \
            f"渠道状态行缺少「{fragment}」：{health_text}"
    if unconfigured:
        assert f"{unconfigured} 未配置" in health_text, \
            f"渠道状态行缺少「{unconfigured} 未配置」：{health_text}"

    # 冷却中数量
    assert str(cooling) in summary_text, \
        f"冷却中数量 {cooling} 未在摘要中找到：{summary_text}"

    # 累计请求
    assert str(total_requests) in summary_text or f"{total_requests:,}" in summary_text, \
        f"累计请求 {total_requests} 未在摘要中找到：{summary_text}"

    # 累计 Tokens（可能有千分位分隔）
    tokens_str = str(total_tokens)
    tokens_formatted = f"{total_tokens:,}"
    assert tokens_str in summary_text or tokens_formatted in summary_text, \
        f"累计 Tokens {total_tokens} 未在摘要中找到：{summary_text}"


@pytest.mark.smoke
def test_e1_dashboard_cards_render(console):
    """[E1] 仪表板各卡片区域渲染完整。"""
    console.nav("dashboard")

    # 验证各卡片区域存在
    for card in ("#dash-summary", "#dash-free", "#dash-health", "#dash-subs"):
        expect(console.el(card)).to_be_visible()

    # 验证摘要卡中有具体指标项
    metrics = console.el("#dash-summary .metric")
    expect(metrics).to_have_count(7, timeout=5000)


@pytest.mark.smoke
def test_e1_dashboard_free_card_link(console):
    """[E1] 免费模型速览卡的"查看全部"按钮跳转正确。"""
    console.nav("dashboard")

    # 验证免费模型速览卡存在
    dash_free = console.el("#dash-free")
    expect(dash_free).to_be_visible()

    # 点击"查看全部"按钮（btn-dash-free）
    btn = console.el("#btn-dash-free")
    expect(btn).to_be_visible()
    btn.click()

    # 验证跳转到免费模型页
    expect(console.page.locator("#panel-free")).to_be_visible()
    assert console.active_tab() == "free"
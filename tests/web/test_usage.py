"""Playwright E2E 用例：WP4-J 用量报表

[J1] 7/30 天范围切换：分别请求 /api/usage/7 与 /api/usage/30，图表汇总随范围变化
"""
from __future__ import annotations

import re
from datetime import date, timedelta

import pytest

pytest.importorskip("playwright", reason="需要安装 playwright")
from playwright.sync_api import expect


@pytest.mark.smoke
def test_j1_usage_range_switch(app, console, api):
    """[J1] 点 7/30 天切换 → 请求 /api/usage/7 与 /api/usage/30，汇总随范围变化"""
    today = date.today()
    recent_day = today                     # 两个范围都会命中
    old_day = today - timedelta(days=20)   # 只有 30 天范围会命中

    # 用后端 recorder 直接写入用量（与线上按天累计同一份数据）
    for _ in range(5):
        app.usage.record("alpha", True, tokens=100, on=recent_day)
    for _ in range(7):
        app.usage.record("alpha", True, tokens=100, on=old_day)

    # 接口层先确认基线：7 天 = 5，30 天 = 5 + 7
    for days, expected in ((7, 5), (30, 12)):
        series = api.get(f"/api/usage/{days}")
        assert series["range"] == days
        assert sum(item["requests"] for item in series["days"]) == expected

    console.nav("usage")

    # 默认 30 天范围：近 30 天 + 合计 12
    expect(console.el("#chart-body")).to_contain_text("近 30 天")
    expect(console.el("#chart-body")).to_contain_text("每日请求数（合计 12）")

    # 切到 7 天：请求 /api/usage/7，汇总随之变小
    with console.expect_api("/api/usage/7") as info:
        console.el('#panel-usage .range-switch button[data-range="7"]').click()
    assert info.value.status == 200
    expect(console.el("#chart-body")).to_contain_text("近 7 天")
    expect(console.el("#chart-body")).to_contain_text("每日请求数（合计 5）")
    # 20 天前那一天的数据被排除，30 天特有的汇总文案不应残留
    expect(console.el("#chart-body")).not_to_contain_text("近 30 天")

    # 切回 30 天：请求 /api/usage/30，汇总恢复
    with console.expect_api("/api/usage/30") as info:
        console.el('#panel-usage .range-switch button[data-range="30"]').click()
    assert info.value.status == 200
    expect(console.el("#chart-body")).to_contain_text("近 30 天")
    expect(console.el("#chart-body")).to_contain_text("每日请求数（合计 12）")

    # 范围按钮高亮状态同步
    expect(console.el('#panel-usage .range-switch button[data-range="30"]')) \
        .to_have_class(re.compile(r"\bactive\b"))


"""Playwright E2E 用例：WP4-I 自维护

[I1] 任务表逐列断言：周期/上次运行/下次运行/状态/上次结果，与 GET /api/tasks 交叉比对
     注：页面表格并无「开关」「连续失败次数」列（见 app.js renderTasks 与 index.html
     thead），开关状态只通过「周期」列显示为「未启用」间接表达，
     consecutive_failures 仅存在于 /api/tasks 数据层，这里一并做数据交叉比对。
"""
from __future__ import annotations

import pytest

pytest.importorskip("playwright", reason="需要安装 playwright")
from playwright.sync_api import expect

# 与 app.js TASK_STATES 保持一致
TASK_STATES = {
    "running": "运行中",
    "idle": "等待中",
    "ok": "正常",
    "warn": "需注意",
    "disabled": "已关闭",
}


@pytest.mark.smoke
def test_i1_task_table_columns_match_api(console, api):
    """[I1] 自维护任务表逐列断言 + 与 /api/tasks 交叉比对"""
    tasks = api.get("/api/tasks")["tasks"]
    assert tasks, "测试环境应至少注册一个后台任务"

    console.nav("maintenance")
    # 表格数据来自 /api/tasks（loadMaintenance 之后异步加载），显式等待一次刷新
    console.expect_api("/api/tasks",
                       action=lambda: console.el("#btn-refresh-tasks").click())
    console.wait_rows("#tasks-body", len(tasks))

    for t in tasks:
        row = console.el("#tasks-body tr").filter(has_text=t["label"])
        expect(row).to_have_count(1)
        cells = row.locator("td")

        # 列 1：任务名 + 说明
        expect(cells.nth(0)).to_contain_text(t["label"])

        # 列 2：状态（开关之外的运行态），标签类名同步断言
        state = t["state"]
        state_text = TASK_STATES.get(state, "—")
        tag = cells.nth(1).locator("span.tag")
        expect(tag).to_have_text(state_text)
        cls = {"running": "unknown", "idle": "active", "ok": "active",
               "warn": "expiring", "disabled": "expired"}.get(state, "unknown")
        expect(tag).to_have_class(f"tag {cls}")

        # 列 3：周期 —— 「开关」在 UI 上表现为未启用时周期显示「未启用」
        expected_cycle = (t.get("cycle") or f"每 {t['hours']} 小时") if t["enabled"] else "未启用"
        expect(cells.nth(2)).to_have_text(expected_cycle)

        # 列 4/5：上次运行、下次运行（时间串由浏览器 toLocaleString 生成，用同一段 JS 复算）
        expect(cells.nth(3)).to_have_text(_localise(console, t.get("last_run_at")))
        expect(cells.nth(4)).to_have_text(_localise(console, t.get("next_run_at")))

        # 列 6：上次结果（失败优先展示错误，否则展示 summary）
        if t.get("last_error"):
            expect(cells.nth(5)).to_contain_text("失败")
            expect(cells.nth(5)).to_contain_text(t["last_error"])
        else:
            expect(cells.nth(5)).to_have_text(t.get("last_summary") or "—")

        # 数据层交叉比对：UI 未渲染的两个字段（开关 / 连续失败次数）
        assert isinstance(t["enabled"], bool)
        assert isinstance(t.get("consecutive_failures", 0), int)


def _localise(console, ts) -> str:
    """用页面自身的 ``new Date(ts*1000).toLocaleString()`` 生成期望文本，避免时区/locale 差异。"""
    if not ts:
        return "—"
    return console.page.evaluate("(ts) => new Date(ts * 1000).toLocaleString()", ts)

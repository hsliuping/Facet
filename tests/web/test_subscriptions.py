"""Playwright E2E 用例：WP4-F 订阅与厂家

[F1] 新增订阅（厂家/档位/额度/到期日）→ 列表出现 + 后端落盘断言 + 仪表板同步
"""
from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

pytest.importorskip("playwright", reason="需要安装 playwright")
from playwright.sync_api import expect


@pytest.mark.mutating
def test_f1_create_subscription_syncs_dashboard(console, api, data_dir):
    """[F1] 新增订阅 → 列表出现 + 落盘断言 + 仪表板"订阅与成本"卡同步更新"""
    console.nav("subscriptions")

    # 点击新增订阅按钮，打开编辑器
    console.btn("新增订阅").click()
    modal = console.wait_modal()

    # 填写表单
    expires_at = (date.today() + timedelta(days=20)).isoformat()  # 超过 remind_days 7，状态应为 active
    modal.locator("#f-plan").fill("E2E 套餐")
    modal.locator("#f-tier").fill("pro")
    modal.locator("#f-price").fill("99")
    modal.locator("#f-expires_at").fill(expires_at)
    modal.locator("#f-quota_limit").fill("1000")
    modal.locator("#f-quota_unit").select_option("tokens")

    # 提交：表单在 .modal-backdrop，按钮为 button[type="submit"].primary
    with console.expect_api("/api/subscriptions", status=200) as info:
        modal.locator('button[type="submit"].primary').click()
    resp = info.value
    assert resp.status == 200

    # Toast 确认
    console.wait_toast("订阅已保存")

    # UI 列表断言：#sub-body 应出现该套餐行
    # quota/channels 列也带 class="sub-note"，console.rows() 会排除含 .sub-note td 的行，
    # 需用原生 locator 数真实行数
    expect(console.el("#sub-body tr").filter(has_text="E2E 套餐")).to_have_count(1)
    row = console.el("#sub-body tr").first
    expect(row).to_contain_text("E2E 套餐")
    expect(row).to_contain_text("pro")
    # 到期日 20 天后，remind_days=7，状态应为生效中
    expect(row.locator(".tag.active")).to_contain_text("生效中")

    # 后端落盘断言：GET /api/subscriptions 返回列表含该记录
    data = api.get("/api/subscriptions")
    subs = data.get("subscriptions", [])
    created = next((s for s in subs if s.get("plan") == "E2E 套餐"), None)
    assert created is not None, "后端列表未出现新增订阅"
    assert created["tier"] == "pro"
    assert created["price"] == 99
    assert created["expires_at"] == expires_at
    assert created["quota_limit"] == 1000
    assert created["quota_unit"] == "tokens"

    # 文件落盘：data_dir/subscriptions.json 同步
    sub_file = data_dir / "subscriptions.json"
    assert sub_file.exists(), "subscriptions.json 未生成"
    with sub_file.open(encoding="utf-8") as f:
        file_payload = json.load(f)
    file_subs = file_payload.get("subscriptions", [])
    assert any(s.get("plan") == "E2E 套餐" for s in file_subs), "文件未包含新增记录"

    # 仪表板同步：导航到 dashboard，#dash-subs 显示 "1 生效"
    console.nav("dashboard")
    expect(console.el("#dash-subs")).to_contain_text("1 生效")
    # dash-subusage 表格也应出现该行
    expect(console.el("#dash-subusage")).to_contain_text("E2E 套餐")
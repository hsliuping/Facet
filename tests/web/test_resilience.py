"""[K1] 后端接口 500 时页面有错误提示且不白屏

覆盖懒加载面板：自维护
断言：出现错误 toast、面板可见、侧边栏/主体/徽章完整、无永久 loading（独立异步区域已加载完毕）
"""
import re

import pytest

pytest.importorskip("playwright", reason="需要安装 playwright")
from playwright.sync_api import expect


@pytest.mark.smoke
def test_api_500_shows_error_and_keeps_shell(console):
    """只读接口返回 500：页面显示错误提示，不白屏、不卡死，独立异步区域仍正常完成加载"""
    page = console.page
    base = console.base_url
    error_msg = "K1 模拟后端故障"

    # 1. 拦截 /api/maintenance 仅精确路径，返回 500
    pattern = re.compile(r"/api/maintenance$")
    def handle(route):
        route.fulfill(status=500, json={"error": {"message": error_msg}})

    page.route(pattern, handle)
    try:
        # 2. 进入自维护面板（懒加载），等待拦截到的 500 响应
        with console.expect_api("/api/maintenance", status=500):
            page.click('button.menu-item[data-tab="maintenance"]')

        # 3. 错误 toast 出现
        toast_text = console.wait_toast(contains=error_msg, kind="bad")
        assert error_msg in toast_text

        # 4. 外壳完整：面板可见、侧边栏可见、主体可见、连接徽章仍为「已连接」
        expect(console.el("#panel-maintenance")).to_be_visible()
        expect(console.el("aside.sidebar")).to_be_visible()
        expect(console.el("div.main")).to_be_visible()
        expect(console.el("#conn-badge")).to_have_text("已连接")

        # 5. 无永久 loading：该面板内其他独立异步请求（/api/maintenance/logs、/api/backups、/api/freealign/logs）
        #    已完成并渲染了空态文案，说明页面未卡在「加载中」状态
        expect(console.el("#maintlog-body")).to_contain_text("还没有维护日志")
        expect(console.el("#backup-body")).to_contain_text("还没有备份文件")
        expect(console.el("#freealign-body")).to_contain_text("还没有对齐记录")

        # 6. 面板主标题仍正常渲染，确认非白屏
        expect(console.el("#panel-maintenance h2")).to_have_text("自维护")
    finally:
        page.unroute(pattern)
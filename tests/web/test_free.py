"""Playwright E2E 测试：免费模型页 (D1)。

D1：打开免费模型页 → 每行含模型名/免费来源/状态/额度余量/厂家限速/近 30 天用量。
另加一条"无免费渠道"空态断言。
"""
from __future__ import annotations

import json
import pytest

pytest.importorskip("playwright", reason="需要安装 playwright")
from playwright.sync_api import expect

from conftest import base_config


def free_config() -> dict:
    """预置 2 个免费渠道的配置（tier="free"）。"""
    cfg = base_config()
    # 修改现有渠道为免费，并添加免费额度相关字段
    for ch in cfg["channels"]:
        ch["tier"] = "free"
        # 添加免费额度配置（本地限流）
        ch["free_limits"] = {"rpm": 60, "rpd": 1000, "tpm": 20000, "tpd": 500000}
        # 模拟有厂家额度同步信息
        ch["provider"] = "test-provider"
    return cfg


@pytest.fixture(scope="function")
def free_config_path(data_dir):
    """带 2 个免费渠道的配置文件。"""
    path = data_dir / "config.json"
    path.write_text(json.dumps(free_config(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


@pytest.fixture(scope="function")
def free_app(free_config_path, data_dir):
    """使用免费渠道配置的 App 实例。"""
    from facet.gateway.core import load_config
    from facet.gateway.server import App

    return App(
        load_config(free_config_path),
        config_path=free_config_path,
        state_file=data_dir / "state.json",
        subscriptions_file=data_dir / "subscriptions.json",
        usage_file=data_dir / "usage.json",
        notifications_file=data_dir / "notifications.json",
        audit_file=data_dir / "audit.jsonl",
        keys_file=data_dir / "keys.json",
    )


@pytest.fixture(scope="function")
def free_gateway_server(free_app):
    """启动带免费渠道配置的 gateway。"""
    import threading
    from http.server import ThreadingHTTPServer
    from facet.gateway.server import Handler

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    httpd.app = free_app
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


@pytest.fixture(scope="function")
def free_page(browser, free_gateway_server, request):
    """已注入管理密钥的页面，指向免费渠道配置的 gateway。"""
    from conftest import _open_console, ADMIN_KEY
    yield from _open_console(browser, free_gateway_server, request, admin=True)


@pytest.fixture(scope="function")
def free_console(free_page, free_gateway_server):
    """绑定免费渠道页面的 Page Object。"""
    from ui import Console
    return Console(free_page, free_gateway_server)


@pytest.fixture(scope="function")
def free_api(free_gateway_server):
    """指向免费渠道 gateway 的 API 客户端。"""
    from conftest import ApiClient, ADMIN_KEY
    return ApiClient(free_gateway_server, ADMIN_KEY)


@pytest.mark.smoke
def test_d1_free_models_table_has_expected_columns(free_console, free_api):
    """[D1] 打开免费模型页 → 每行含模型名/免费来源/状态/额度余量/厂家限速/近 30 天用量。"""
    # 1. 切到免费模型页（等待 /api/maintenance）
    free_console.nav("free")

    # 2. 验证摘要卡片可见
    expect(free_console.el("#free-summary")).to_be_visible()

    # 3. 验证表格 body 可见
    free_body = free_console.el("#free-body")
    expect(free_body).to_be_visible()

    # 4. 获取表头验证列结构
    # 注意：表头在 <thead> 中，通过查找父元素的 thead th
    headers = free_console.page.locator("#free-body").locator("xpath=../..").locator("thead th")
    header_texts = [headers.nth(i).inner_text() for i in range(headers.count())]
    # 根据实际 DOM 结构，表头关键词（实际列为：模型、能力分、免费来源、状态、免费额度、限速、近 30 天、操作）
    expected_keywords = ["模型", "能力分", "免费来源", "状态", "免费额度", "限速", "近 30 天"]
    for expected in expected_keywords:
        assert any(expected in h for h in header_texts), \
            f"缺少表头列: {expected} (实际: {header_texts})"

    # 5. 等待表格行渲染（至少有 1 行数据，因为配置了 2 个免费渠道各有模型）
    # 注意：base_config 中每个渠道只有 1 个 auto 模型
    free_console.wait_rows("#free-body", count=2, timeout=15000)

    # 6. 验证每行包含预期列内容：模型名、来源、状态、额度、限速、近30天用量
    rows = free_console.rows("#free-body")
    count = rows.count()
    assert count >= 2, f"预期至少 2 行（2 个免费渠道各 1 模型），实际 {count} 行"

    for i in range(count):
        row = rows.nth(i)

        # 第 1 列：模型名 - 包含 <code> 元素（例如 gpt-3.5-turbo / claude-sonnet-4-5）
        code_el = row.locator("td").nth(0).locator("code")
        expect(code_el).to_be_visible()
        # 验证 <code> 内容非空
        code_text = code_el.inner_text().strip()
        assert code_text, f"第 {i} 行模型名应非空，实际: '{code_text}'"

        # 第 2 列：能力分 - 公开评测 Agent 分，未收录显示「未评测」
        ability_el = row.locator("td").nth(1)
        expect(ability_el).to_be_visible()
        ability_text = ability_el.inner_text().strip()
        assert ability_text == "未评测" or ability_text.replace(".", "", 1).isdigit(), \
            f"第 {i} 行能力分应为「未评测」或数字，实际: '{ability_text}'"

        # 第 3 列：来源 - 包含提供商名称和渠道名
        source_el = row.locator("td").nth(2)
        expect(source_el).to_be_visible()
        # 检查来源文本包含已知标识
        source_text = source_el.inner_text()
        assert "test" in source_text.lower(), f"来源文本应包含 test 标识: {source_text}"

        # 第 4 列：状态标签
        status_el = row.locator("td").nth(3)
        expect(status_el).to_be_visible()

        # 第 5 列：额度 - 有免费额度条或文本
        quota_el = row.locator("td").nth(4)
        expect(quota_el).to_be_visible()

        # 第 6 列：限速 - 有厂家 + 网关限速信息
        rate_el = row.locator("td").nth(5)
        expect(rate_el).to_be_visible()

        # 第 7 列：近 30 天用量 - 有用量数据
        usage_el = row.locator("td").nth(6)
        expect(usage_el).to_be_visible()

        # 第 8 列：测试按钮存在
        test_btn = row.locator("td").nth(7).locator("button.free-test")
        expect(test_btn).to_be_visible()


@pytest.mark.smoke
def test_d1_free_models_empty_state(console):
    """[D1] 无免费渠道配置时 → 显示空态提示。"""
    # 使用默认 base_config（无 tier="free" 渠道）
    console.nav("free")

    free_body = console.el("#free-body")
    expect(free_body).to_be_visible()

    # 空态行：colspan=8，包含提示文案
    empty_row = free_body.locator("tr:has(td.sub-note)")
    expect(empty_row).to_be_visible()
    expect(empty_row).to_contain_text("还没有免费渠道")
    expect(empty_row).to_contain_text("到「渠道配置」把渠道的套餐设为")
    expect(empty_row).to_contain_text("它的模型就会出现在这里")


@pytest.mark.smoke
def test_d1_free_models_summary_cards(free_console, free_api):
    """[D1] 免费模型页摘要卡片数值与 /api/maintenance 交叉比对。"""
    free_console.nav("free")

    # 后端获取维护数据
    maint = free_api.get("/api/maintenance")
    free_data = maint.get("free_models", {})
    summary = free_data.get("summary", {})
    channels = free_data.get("channels", [])

    # 总模型数 = 各渠道模型数之和（与前端 rows.length 一致）
    total_models = sum(len(ch.get("models", {})) for ch in channels)

    # UI 摘要卡片
    summary_el = free_console.el("#free-summary")
    summary_text = summary_el.inner_text()

    # 免费模型总数
    assert f"免费模型 {total_models}" in summary_text or str(total_models) in summary_text, \
        f"摘要卡片中应包含免费模型总数 {total_models}: {summary_text}"

    # 现在能用数（routable）来自 summary.routable；仅做存在性检查
    routable = summary.get("routable", 0)
    assert f"现在能用 {routable}" in summary_text or str(routable) in summary_text, \
        f"摘要卡片中应包含现在能用数 {routable}: {summary_text}"
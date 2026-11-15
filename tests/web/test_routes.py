"""Playwright E2E 测试：路由链编辑器 (C1)。

C1：新增链 + 添加渠道节点 → 保存 → /api/config.routes 出现该链，节点顺序与 UI 一致。
"""
from __future__ import annotations

import json

import pytest

pytest.importorskip("playwright", reason="需要安装 playwright")
from playwright.sync_api import expect

from conftest import base_config


@pytest.fixture(scope="function")
def config_path(data_dir):
    """覆盖 conftest.config_path：追加第二个 openai 渠道，供添加节点使用。"""
    cfg = base_config()
    cfg["channels"].append({
        "name": "test-channel-2",
        "protocol": "openai",
        "base_url": "http://127.0.0.1:9997",
        "api_key": "sk-test-key-2",
        "models": {"auto": "gpt-3.5-turbo"},
        "priority": 30,
    })
    path = data_dir / "config.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


@pytest.mark.mutating
def test_c1_add_route_chain_and_node(console, api):
    """[C1] 新增链 + 添加渠道节点 → 保存 → routes 出现该链，节点顺序与 UI 一致。"""
    # ── 第一步：切到路由链面板（纯本地渲染，无需等待 API）
    console.nav("routes", api=None)

    # 2. 点击"新增路由链"按钮
    console.el("#btn-add-route").click()

    # 3. 等待弹窗打开并填写表单
    modal = console.wait_modal()
    modal.locator("input[name='rname']").fill("test-chain")
    modal.locator("select[name='rprotocol']").select_option("openai")
    modal.locator("select[name='rchannel']").select_option("test-channel")
    # 提交，同时等待保存配置的网络请求完成
    console.expect_api("/api/config",
                       action=lambda: modal.locator('[type="submit"]').click())
    expect(console.page.locator(".modal-backdrop:visible")).to_have_count(0, timeout=5000)

    # 4. 在新链（索引 2，前面是两条 default）上添加第二个渠道节点
    add_node_btn = console.el('button[data-action="add-node"][data-index="2"]')
    expect(add_node_btn).to_be_visible()
    console.el('select[id="add-select-2"]').select_option("test-channel-2")
    # 点击"添加渠道"，等待其触发的保存请求（debounced 600ms 后 POST /api/config）
    console.expect_api("/api/config", action=lambda: add_node_btn.click())

    # 5. 后端交叉校验：/api/config.routes 应包含新链，节点顺序与 UI 一致
    cfg = api.get("/api/config")
    routes = cfg["routes"]
    test_chain = next(
        (r for r in routes if r["name"] == "test-chain" and r["protocol"] == "openai"),
        None)
    assert test_chain is not None, "新增链 test-chain 未出现在 /api/config.routes 中"
    assert test_chain["channels"] == ["test-channel", "test-channel-2"], \
        f"节点顺序不匹配 UI：{test_chain['channels']}"

    # 6. UI 端验证链渲染正确
    chain_cards = console.el("#route-list .card")
    expect(chain_cards).to_have_count(3)  # 2 条 default + 1 条 test-chain
    # 通过 inner_text 定位 test-chain 卡片（Console.el 无法接受 has_text kwarg）
    cards_locator = console.page.locator("#route-list .card")
    cards = cards_locator.all()
    test_chain_card = next(
        (c for c in cards if "test-chain" in c.inner_text()), None)
    assert test_chain_card is not None, "UI 中未找到 test-chain 卡片"
    nodes = test_chain_card.locator(".chain li:not(.empty)")
    expect(nodes).to_have_count(2)
    # 获取节点文本并验证
    node_texts = [nodes.nth(i).inner_text() for i in range(2)]
    assert any("1" in t and "test-channel" in t for t in node_texts), \
        f"第一个节点应为编号+test-channel: {node_texts}"
    assert any("2" in t and "test-channel-2" in t for t in node_texts), \
        f"第二个节点应为编号+test-channel-2: {node_texts}"
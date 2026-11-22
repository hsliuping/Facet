"""WP2 P0 渠道生命周期（B1、B2、B3），均打 @pytest.mark.mutating"""
from __future__ import annotations

import json
import pytest

pytest.importorskip("playwright", reason="需要安装 playwright")
from playwright.sync_api import expect  # noqa: E402


@pytest.mark.mutating
def test_b1_new_channel_syncs_list_and_config(config_path, console, api):
    """[B1] 新增渠道后列表与配置同步"""
    console.nav("channels")
    # 初始两条渠道
    console.wait_cards("#channel-list", 2)

    # 点击新增按钮
    console.el("#btn-add-channel").click()
    console.wait_modal()

    # 填写表单
    console.el("#f-name").fill("new-channel")
    console.page.select_option("#f-provider", "openai")
    # provider 的 onchange 会动态生成计费模式选项；选第一个具体模式
    console.page.wait_for_function(
        "() => document.querySelector('#f-mode').options.length > 1")
    console.page.select_option("#f-mode", index=1)
    console.page.select_option("#f-protocol", "openai")
    console.el("#f-base_url").fill("http://127.0.0.1:8080")
    console.el("#f-api_key").fill("sk-new-key")
    console.el("#f-models").fill("auto=gpt-3.5-turbo")
    # 保存
    console.el('#editor-form button[type="submit"]').click()
    # 等待热加载 toast
    console.wait_toast("已保存并热加载", kind="ok")
    # 等待列表行数 +1
    console.wait_cards("#channel-list", 3)

    # UI 断言：列表中出现 new-channel
    cards = console.cards("#channel-list")
    names = [c.locator(".ch-name").inner_text() for c in cards.all()]
    expect(cards).to_have_count(3)
    assert "new-channel" in names

    # 后端断言：配置文件中存在该渠道
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    ch_names = [c["name"] for c in cfg["channels"]]
    assert "new-channel" in ch_names


@pytest.mark.mutating
def test_b2_edit_preserves_masked_key(config_path, console, api):
    """[B2] 编辑已有渠道：密钥框显示 *** 原样保存 -> 后端密钥仍为原值 sk-test-key"""
    console.nav("channels")
    console.wait_cards("#channel-list", 2)

    # 点击第一条 test-channel 的编辑按钮（data-index=0）
    console.el('button[data-action="edit"][data-index="0"]').click()
    console.wait_modal()

    # 修改 base_url（非密钥字段），确保保存后配置会更新但密钥不变
    console.el("#f-base_url").fill("http://127.0.0.1:9999")
    # 保存
    console.el('#editor-form button[type="submit"]').click()
    # 编辑后保存即刻热加载，UI 直接渲染新 base_url（已在随后的 first_card 断言中验证）

    # UI 断言：编辑器关闭，base_url 已更新（第一条卡片包含新端点）
    first_card = console.cards("#channel-list").first
    expect(first_card).to_contain_text("http://127.0.0.1:9999")

    # 后端断言：原始 api_key 仍为 sk-test-key
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    first = cfg["channels"][0]
    assert first["api_key"] == "sk-test-key"


@pytest.mark.mutating
def test_b3_delete_channel_removes_from_list_and_config(config_path, console, api):
    """[B3] 删除渠道：处理二次确认（ask 浮层）-> 列表消失 + /api/config 中不存在"""
    console.nav("channels")
    console.wait_cards("#channel-list", 2)

    # 先新增一个临时渠道，随后删除它（避免破坏基础两条渠道）
    console.el("#btn-add-channel").click()
    console.wait_modal()
    console.el("#f-name").fill("temp-delete")
    console.page.select_option("#f-provider", "openai")
    console.page.wait_for_function(
        "() => document.querySelector('#f-mode').options.length > 1")
    console.page.select_option("#f-mode", index=1)
    console.page.select_option("#f-protocol", "openai")
    console.el("#f-base_url").fill("http://127.0.0.1:8080")
    console.el("#f-api_key").fill("sk-temp-del")
    console.el("#f-models").fill("auto=gpt-3.5-turbo")
    console.el('#editor-form button[type="submit"]').click()
    console.wait_toast("已保存并热加载", kind="ok")
    console.wait_cards("#channel-list", 3)

    # 点击临时渠道的删除按钮（索引 2，即第三条）
    console.el('button[data-action="delete"][data-index="2"]').click()
    # 确认 ask 浮层出现（删除渠道要求输入渠道名才能确认）
    ask = console.wait_ask()
    expect(ask.locator('[data-ask="ok"]')).to_be_disabled()
    # 输入渠道名解锁确认按钮，并等待自动保存 /api/config 响应
    with console.expect_api("/api/config"):
        console.ask_answer("temp-delete")
    # 等待列表行数回到 2
    console.wait_cards("#channel-list", 2)

    # UI 断言：列表中不再有 temp-delete
    cards = console.cards("#channel-list")
    names = [c.locator(".ch-name").inner_text() for c in cards.all()]
    expect(cards).to_have_count(2)
    assert "temp-delete" not in names

    # 后端断言：配置文件中已不存在 temp-delete
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    ch_names = [c["name"] for c in cfg["channels"]]
    assert "temp-delete" not in ch_names
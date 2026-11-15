"""WP2 P0 认证与访问控制（A1、A2）。"""
from __future__ import annotations

import pytest

pytest.importorskip("playwright", reason="需要安装 playwright")
from playwright.sync_api import expect  # noqa: E402


@pytest.fixture
def anon_page(browser, gateway_server):
    """覆盖 conftest 默认 anon_page：未注入密钥时 boot() 会停在 ask 浮层。

    父 conftest 的 ``anon_page`` 会等待连接徽章离开「连接中」，但匿名会话的
    ``boot()`` 正等待 ask 浮层，永远走不到改徽章的那步，因此本地 fixture 直接
    打开页面，让用例自己处理 ask 浮层。
    """
    context = browser.new_context(viewport={"width": 1280, "height": 800},
                                  locale="zh-CN")
    page = context.new_page()
    page.goto(gateway_server + "/", wait_until="domcontentloaded")
    yield page
    context.close()


@pytest.mark.smoke
def test_a1_missing_admin_key_shows_ask_then_login(anon_console):
    """[A1] 未带管理密钥打开控制台：弹出密钥框，输入正确 key 后面板加载"""
    ask = anon_console.wait_ask()
    expect(ask.locator(".ask-input")).to_be_visible()
    expect(anon_console.el(".ask-backdrop")).to_be_visible()
    expect(anon_console.page.locator("aside.sidebar")).to_be_visible()

    anon_console.ask_answer("test-admin")
    expect(anon_console.el(".ask-backdrop")).to_have_count(0)
    anon_console.wait_badge("已连接")
    expect(anon_console.el("#panel-dashboard")).to_be_visible()
    expect(anon_console.page.locator("#dash-summary .metric").first).to_be_visible()


@pytest.mark.smoke
def test_a2_wrong_admin_key_shows_auth_error(anon_console):
    """[A2] 输入错误管理密钥：提示鉴权失败，不进入已登录态"""
    ask = anon_console.wait_ask()
    expect(ask.locator(".ask-input")).to_be_visible()
    anon_console.ask_answer("wrong-admin-key")

    # 后端返回 401 invalid admin key；前端 toast 提示，徽章显示连接失败。
    anon_console.wait_toast("invalid admin key", kind="bad")
    anon_console.wait_badge("连接失败")
    # 其他初始数据请求仍以错误 key 返回 401，会继续弹出密钥框；不能视为已登录。
    expect(anon_console.el(".ask-backdrop")).to_be_visible()
    expect(anon_console.page.locator("#dash-summary .metric")).to_have_count(0)

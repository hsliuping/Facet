"""Facet 控制台的 Page Object。

用例只描述业务动作，选择器与等待策略集中在这里：
- ``nav()`` 取代 ``click`` + ``wait_for_timeout``
- ``expect_api()`` 取代固定睡眠，直接等待真实接口响应
- ``ask_*`` / ``editor_*`` 统一处理页内弹窗与编辑器
"""
from __future__ import annotations

from playwright.sync_api import Page, expect

MENU = 'button.menu-item[data-tab="{tab}"]'
PANEL = "#panel-{tab}"

# 每个面板进入时会发出的首个接口（用于 nav(api=...) 的显式等待）。
ENTRY_API = {
    "free": "/api/maintenance",
    "channels": "/api/channels/usage",
    "models": "/api/state",
    "status": "/api/state",
    "maintenance": "/api/maintenance",
    "usage": "/api/usage/",
    "requests": "/api/requests/",
    "members": "/api/usage/users/",
    "apikeys": "/api/keys",
    "subscriptions": "/api/subscriptions",
    "vendors": "/api/providers",
    "integration": "/api/guide",
}


class Console:
    """控制台交互入口，绑定一个已加载完成的 ``page``。"""

    def __init__(self, page: Page, base_url: str = ""):
        self.page = page
        self.base_url = base_url.rstrip("/")

    # ------------------------------------------------------------------
    # 导航
    # ------------------------------------------------------------------

    def nav(self, tab: str, *, api: str | None = "", timeout: int = 15000):
        """点击菜单切到 ``tab``，等待面板可见。

        ``api`` 为 ``""`` 时按 ``ENTRY_API`` 等待该页首个数据接口；
        传 ``None`` 表示该页进入不发请求（routes/vendors 等纯本地渲染页）。
        返回匹配到的 ``Response``（未等待时为 ``None``）。
        """
        path = ENTRY_API.get(tab) if api == "" else api
        selector = MENU.format(tab=tab)
        if path is None:
            self.page.click(selector)
        else:
            with self.page.expect_response(
                    lambda r: path in r.url and r.status == 200,
                    timeout=timeout) as info:
                self.page.click(selector)
        panel = self.page.locator(PANEL.format(tab=tab))
        expect(panel).to_be_visible(timeout=timeout)
        return info.value if path is not None else None

    def goto(self, tab: str, **kw):
        """通过 URL hash 切页，等价于手动改地址栏/浏览器前进后退。"""
        self.page.evaluate(f"() => {{ location.hash = '#/{tab}'; }}")
        panel = self.page.locator(PANEL.format(tab=tab))
        expect(panel).to_be_visible(timeout=kw.get("timeout", 15000))

    def active_tab(self) -> str:
        """当前高亮菜单项的 ``data-tab``。"""
        return self.page.locator(".menu-item.active").get_attribute("data-tab")

    def page_title(self) -> str:
        return self.page.locator("#page-title").inner_text()

    def badge(self) -> str:
        return self.page.locator("#conn-badge").inner_text()

    def wait_badge(self, text: str, timeout: int = 15000) -> None:
        """等待连接徽章变成指定文案。"""
        expect(self.page.locator("#conn-badge")).to_have_text(text,
                                                              timeout=timeout)

    # ------------------------------------------------------------------
    # 快捷检查（多数只读用例只需这些）
    # ------------------------------------------------------------------

    def sidebar_visible(self) -> bool:
        return self.page.locator("aside.sidebar").is_visible()

    def panel_visible(self, tab: str) -> bool:
        return self.page.locator(PANEL.format(tab=tab)).is_visible()

    def el(self, selector: str):
        """任意 CSS 选择器的定位器。"""
        return self.page.locator(selector)

    def btn(self, label: str):
        return self.page.get_by_role("button", name=label)

    def input(self, selector: str):
        return self.page.locator(selector)

    def expect_text(self, selector: str, text: str):
        expect(self.page.locator(selector)).to_have_text(text)

    # ------------------------------------------------------------------
    # 网络
    # ------------------------------------------------------------------

    def expect_api(self, path: str, *, action=None, status: int = 200,
                   timeout: int = 15000):
        """等待命中 ``path`` 的响应。

        传 ``action``（无参可调用）时在其执行期间等待；否则返回上下文
        管理器，由调用方在 ``with`` 块里触发动作。
        """
        pred = lambda r: path in r.url and r.status == status  # noqa: E731
        if action is None:
            return self.page.expect_response(pred, timeout=timeout)
        with self.page.expect_response(pred, timeout=timeout) as info:
            action()
        return info.value

    def json(self, path: str, *, action=None, status: int = 200) -> dict:
        """等待接口响应并返回其 JSON 体。"""
        resp = self.expect_api(path, action=action, status=status)
        return resp.json()

    # ------------------------------------------------------------------
    # 提示与弹窗
    # ------------------------------------------------------------------

    def toasts(self):
        return self.page.locator("#toasts .toast")

    def wait_toast(self, contains: str = "", kind: str | None = None,
                   timeout: int = 10000) -> str:
        """等待出现一条提示，返回其文案。

        ``contains`` 非空时按子串过滤，``kind`` 对应 toast 的样式类
        （``ok`` / ``warn`` / ``bad``）。
        """
        sel = "#toasts .toast"
        if kind:
            sel += f".{kind}"
        locator = self.page.locator(sel)
        if contains:
            locator = self.page.locator(sel, has_text=contains)
        expect(locator.first).to_be_visible(timeout=timeout)
        return locator.first.inner_text()

    def wait_modal(self, timeout: int = 10000):
        """等待页内通用编辑器 ``#modal`` 打开，返回其定位器。"""
        modal = self.page.locator(".modal-backdrop:visible")
        expect(modal.first).to_be_visible(timeout=timeout)
        return modal.first

    def close_modal(self) -> None:
        """点掉当前打开的遮罩（不保存）。"""
        self.page.locator(".modal-backdrop:visible").first.wait_for(
            state="visible", timeout=5000)
        self.page.keyboard.press("Escape")
        expect(self.page.locator(".modal-backdrop:visible")).to_have_count(
            0, timeout=5000)

    # ------------------------------------------------------------------
    # ask 浮层（管理密钥 / API Key / 二次确认）
    # ------------------------------------------------------------------

    def ask(self):
        return self.page.locator(".ask-backdrop .ask-modal")

    def wait_ask(self, contains: str = "", timeout: int = 10000):
        ask = self.ask() if not contains else self.page.locator(
            ".ask-backdrop .ask-modal", has_text=contains)
        expect(ask.first).to_be_visible(timeout=timeout)
        return ask.first

    def ask_answer(self, value: str) -> None:
        """在 ask 输入框填值并确认。"""
        ask = self.wait_ask()
        ask.locator(".ask-input").fill(value)
        ask.locator('[data-ask="ok"]').click()

    def ask_confirm(self) -> None:
        """确认一个无输入框的 ask（二次确认）浮层。"""
        ask = self.wait_ask()
        ask.locator('[data-ask="ok"]').click()

    def ask_cancel(self) -> None:
        ask = self.wait_ask()
        ask.locator('[data-ask="cancel"]').click()

    # ------------------------------------------------------------------
    # 表格
    # ------------------------------------------------------------------

    def rows(self, body_selector: str):
        """表格主体行数（空态占位行不算）。"""
        return self.page.locator(f"{body_selector} tr:not(:has(td.sub-note))")

    def cards(self, container_selector: str):
        """面板卡片数（.card 子元素）。"""
        return self.page.locator(f"{container_selector} .card")

    def wait_rows(self, body_selector: str, count: int,
                  timeout: int = 15000) -> None:
        expect(self.rows(body_selector)).to_have_count(count, timeout=timeout)

    def wait_cards(self, container_selector: str, count: int,
                   timeout: int = 15000) -> None:
        expect(self.cards(container_selector)).to_have_count(count, timeout=timeout)
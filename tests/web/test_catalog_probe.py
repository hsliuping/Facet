"""渠道编辑器目录试通（B6）：拉取模型后逐个 ping，不可用自动取消勾选。

聚合渠道（OpenRouter 式）目录里常混着已下线/限速的模型，映射过去调用才炸。
「试通勾选」按向导自动选型的轮询语义逐个 ping（单模型 10 秒超时，8 个一批），
失败项取消勾选并标 ✗，「把勾选写入模型映射」因此只写可用的模型。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from playwright.sync_api import expect

from ui import Console


BAD_MODELS = {"stale-model", "dead-model"}


class MockUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/").endswith("/models"):
            self._json(200, {"data": [
                {"id": "stale-model"}, {"id": "dead-model"},
                {"id": "fresh-model"},
            ]})
        else:
            self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if body.get("model") in BAD_MODELS:
            self._json(404, {"error": {"message": "model not found"}})
            return
        self._json(200, {"choices": [{"message": {
            "role": "assistant", "content": "pong"}}],
            "usage": {"total_tokens": 1}})

    def log_message(self, fmt, *args):
        pass


@pytest.fixture()
def mock_upstream():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), MockUpstream)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


@pytest.mark.mutating
def test_catalog_probe_unchecks_broken_models(admin_page, mock_upstream):
    """试通勾选：可用项保留勾选并标 ✓，不可用项取消勾选并标 ✗（不落盘）。"""
    page = admin_page
    console = Console(page)
    console.nav("channels")
    console.wait_cards("#channel-list", 2)
    console.el('button[data-action="edit"][data-index="0"]').click()
    console.wait_modal()

    # 只改草稿里的 base_url 指向 mock 上游，不保存（避免污染会话配置）
    console.el("#f-base_url").fill(mock_upstream)
    console.el("#editor-fetch-models").click()
    page.wait_for_selector("#catalog-items input[type='checkbox']",
                           state="attached")

    boxes = page.locator("#catalog-items input[type='checkbox']")
    assert boxes.count() == 3  # 目录三项全部预勾选
    page.locator("#catalog-probe").click()
    page.wait_for_selector(".tag.probe-ok", timeout=15000)

    # fresh 保留勾选 + ✓；stale/dead 取消勾选 + ✗（悬停可见原因）
    assert page.locator(".tag.probe-ok").count() == 1
    assert page.locator(".tag.probe-bad").count() == 2
    state = page.evaluate(
        """() => Array.from(
            document.querySelectorAll('#catalog-items input[type=checkbox]'))
            .map((b) => ({ value: b.value, checked: b.checked }))""")
    by_value = {b["value"]: b["checked"] for b in state}
    assert by_value["fresh-model"] is True
    assert by_value["stale-model"] is False
    assert by_value["dead-model"] is False
    title = page.locator(".tag.probe-bad").first.get_attribute("title") or ""
    assert title  # 失败原因写进悬停提示

    # 遗留「auto=」空值行的渠道：写入映射时 auto 自动落到试通可用的模型，
    # 不再把空行带进映射卡住保存
    page.evaluate(
        """() => { document.getElementById('f-models').value = 'auto=\\nstale-model'; }""")
    console.el("#catalog-apply").click()
    console.wait_toast("已写入", kind="ok")
    lines = [s.strip() for s in console.el("#f-models").input_value().splitlines()
             if s.strip()]
    assert "auto=fresh-model" in lines
    assert "fresh-model=fresh-model" in lines
    assert not any(s.endswith("=") for s in lines)  # 不留空值行
    assert "stale-model" not in lines  # 未勾选的身份映射不复活

    # 会话首次访问会自动弹出使用指南（guide-backdrop 同属 modal-backdrop，
    # 其捕获层还会拦截 Escape）：先关指南，再关编辑器。
    guide = page.locator("#guide-close")
    if guide.count() and guide.first.is_visible():
        guide.first.click()
        expect(page.locator("#guide-backdrop")).to_be_hidden()
    # 改过 base_url 会触发「未保存修改」守卫，确认放弃后关闭
    console.el("#modal-close").click()
    console.ask_confirm()
    expect(page.locator("#modal")).to_be_hidden()

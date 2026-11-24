"""向导渠道测试的自动选型轮询：过时模型 404 换下一个，打通为止。

场景（Facet6 实测）：注册表写死的 auto 模型被上游下线后，渠道测试一碰
就 404，用户既不知道哪个模型可用，也不该需要知道。现在向导拉取真实目录
后按「厂家自有模型优先」逐个试到打通，前端逐行显示每个模型的测试结果；
401/403 这类 Key/账号问题则立即停下不再换模型。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

# 这两个模型上游已「下线」：对话补全一律 404 page not found
BAD_MODELS = {"nvidia/alpha-stale", "acme/beta-stale"}


class MockUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _models(self) -> None:
        data = json.dumps({"data": [
            {"id": "nvidia/alpha-stale"},
            {"id": "acme/beta-stale"},
            {"id": "nvidia/gamma-fresh"},
        ]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/").endswith("/models"):
            return self._models()
        self.send_error(404)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if body.get("model") in BAD_MODELS:
            payload = b"404 page not found"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        data = json.dumps({
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"total_tokens": 5},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

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


def test_wizard_autoselect_skips_stale_models(admin_page, mock_upstream, api):
    page = admin_page
    # 直接把向导状态置到 test-save 步所需的最小上下文：
    # 注册表清单（mode.models）故意只写过时的 alpha——轮询必须以现拉的
    # 目录为准，而不是相信注册表清单。
    page.evaluate(
        """(base) => {
          wizardState.tier = 'free';
          wizardState.provider = {
            id: 'nvidia', name: 'NVIDIA NIM', modeKey: 'payg',
            mode: { protocol: 'openai', base_url: base + '/v1',
                    models: ['nvidia/alpha-stale'], tier: 'free' },
          };
          wizardState.providerKey = 'sk-wizard-test';
          return renderWizard('test-save');
        }""",
        mock_upstream,
    )
    # renderWizard await 了 onEnter，走到这里轮询必已结束：直接断言 DOM。
    rows = page.locator('.wizard-test-row')
    assert rows.count() == 2  # beta 未被尝试：轮询在第一个打通的模型处停
    first = rows.nth(0).inner_text()
    second = rows.nth(1).inner_text()
    assert 'nvidia/alpha-stale' in first and '404' in first
    assert 'nvidia/gamma-fresh' in second and '✓' in second
    # 状态区报告成功与尝试次数；厂家自有模型优先（beta 依旧排最后但没轮到）
    status = page.locator('#wizard-test-status').inner_text()
    assert '测试通过' in status and '第 2 次尝试打通' in status
    # 渠道已落盘：auto 指向实测打通的模型，而不是目录字母序头部
    cfg = api.get('/api/config')
    assert 'nvidia' in [c['name'] for c in cfg['channels']]
    saved = next(c for c in cfg['channels'] if c['name'] == 'nvidia')
    assert saved['models']['auto'] == 'nvidia/gamma-fresh'

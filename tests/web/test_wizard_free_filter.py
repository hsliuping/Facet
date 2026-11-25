"""向导免费档的候选过滤：只测 :free 模型，不碰付费路由。

OpenRouter 的目录里 openrouter/auto、openrouter/fusion 等不带 :free 后缀
的是付费路由，未充值账号一碰就被上游拒绝。向导选「免费模型」时必须和
渠道编辑器「只选免费」、后端 model_tier 同口径，只把 :free/-free 模型放进
测试池；本测试断言前端过滤后实际 ping 过的模型不含任何非免费项。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from facet.gateway.probe import is_free_model

# 记录每个测试里 mock 上游实际收到的 ping 模型，供断言过滤结果。
_pinged: list[str] = []


class MockUpstreamFree(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _models(self) -> None:
        data = json.dumps({"data": [
            {"id": "acme/rocket-free"},  # -free 后缀：旧实现会误判为非免费
            {"id": "baidu/ernie-5.5-flash-preview:free"},
            {"id": "google/gemma-4-26b-a4b-it:free"},
            {"id": "liquid/lfm-3-1.7b:free"},
            # 非免费：不带 :free 后缀的付费路由，免费档绝不能 ping 它们。
            {"id": "openrouter/auto"},
            {"id": "openrouter/fusion"},
            {"id": "openrouter/pareto-code"},
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
        model = body.get("model") or ""
        _pinged.append(model)
        # 只有 gemma 打通：让轮询至少试一个候选，又在非免费项之前停下。
        if model == "google/gemma-4-26b-a4b-it:free":
            data = json.dumps({
                "choices": [{"message": {"role": "assistant", "content": "ok"}}],
                "usage": {"total_tokens": 5},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        payload = b"404 page not found"
        self.send_response(404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        pass


@pytest.fixture()
def mock_free_upstream():
    _pinged.clear()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), MockUpstreamFree)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_wizard_free_tier_only_pings_free_models(admin_page, mock_free_upstream, api):
    page = admin_page
    page.evaluate(
        """(base) => {
          wizardState.tier = 'free';
          wizardState.provider = {
            id: 'openrouter', name: 'OpenRouter', modeKey: 'payg',
            mode: { protocol: 'openai', base_url: base + '/v1',
                    models: ['openrouter/auto'], tier: 'free' },
          };
          wizardState.providerKey = 'sk-wizard-free-test';
          return renderWizard('test-save');
        }""",
        mock_free_upstream,
    )
    # 过滤生效：所有被 ping 的模型都被 probe.is_free_model 认定为免费，
    # 付费路由一个都没碰；-free 后缀也必须被认作免费（旧实现会漏掉）。
    assert _pinged, "向导应至少 ping 一个候选模型"
    assert all(is_free_model(m) for m in _pinged), _pinged
    assert "acme/rocket-free" in _pinged  # -free 后缀未被误过滤
    assert "openrouter/auto" not in _pinged
    assert "openrouter/fusion" not in _pinged
    assert "openrouter/pareto-code" not in _pinged
    # 渠道落盘：auto 指向打通的免费模型，映射里不含任何非免费模型。
    cfg = api.get('/api/config')
    saved = next(c for c in cfg['channels'] if c['name'] == 'openrouter')
    assert saved['models']['auto'] == 'google/gemma-4-26b-a4b-it:free'
    mapped = [k for k in saved['models'] if k not in ('auto', '*')]
    assert mapped and all(is_free_model(m) for m in mapped), mapped

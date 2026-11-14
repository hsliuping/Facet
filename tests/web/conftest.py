"""Playwright E2E 测试夹具：隔离的临时目录、API 客户端、认证上下文、失败留证。

设计要点（对应 WP0）：
- 每个用例独享 ``tempfile.TemporaryDirectory()``，config 与全部持久化文件
  （state / usage / audit / keys / subscriptions / notifications / vendorquota）
  都落在该目录内，跑完即删，不污染仓库与系统临时区。
- ``api`` 客户端用标准库 urllib，直接读后端落盘结果做交叉断言，不新增依赖。
- ``admin_page`` 在页面脚本执行前注入管理密钥；``anon_page`` 不注入，
  用于认证拦截类用例。两者共用会话级 Chromium，只新建 context。
- 用例失败时自动保存整页截图与 Playwright trace 到
  ``build/playwright-artifacts/<用例名>/``。
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

import pytest

pytest.importorskip("playwright", reason="需要安装 playwright")
from playwright.sync_api import sync_playwright  # noqa: E402

SRC = Path(__file__).resolve().parent.parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from facet.gateway.core import load_config  # noqa: E402
from facet.gateway.server import App, Handler  # noqa: E402

ADMIN_KEY = "test-admin"
ARTIFACT_ROOT = Path("build") / "playwright-artifacts"


# ---------------------------------------------------------------------------
# 测试配置
# ---------------------------------------------------------------------------

def base_config() -> dict:
    """基础配置：两条渠道、两条渠道各属一条同协议 ``default`` 路由链。

    路由按 ``protocol:hint`` 解析，两条链同名不同协议是合法的（openai 的
    default 与 anthropic 的 default 互不冲突）。
    """
    return {
        "_readme": "playwright e2e fixture config",
        "listen": {"host": "127.0.0.1", "port": 0, "admin_key": ADMIN_KEY},
        "channels": [
            {
                "name": "test-channel",
                "protocol": "openai",
                "base_url": "http://127.0.0.1:9999",
                "api_key": "sk-test-key",
                "models": {"auto": "gpt-3.5-turbo"},
                "priority": 10,
            },
            {
                "name": "test-claude",
                "protocol": "anthropic",
                "base_url": "http://127.0.0.1:9998",
                "api_key": "sk-ant-test",
                "models": {"auto": "claude-sonnet-4-5"},
                "priority": 20,
            },
        ],
        "routes": [
            {"name": "default", "protocol": "openai", "channels": ["test-channel"]},
            {"name": "default", "protocol": "anthropic", "channels": ["test-claude"]},
        ],
    }


# ---------------------------------------------------------------------------
# gateway 服务（每用例独享临时目录）
# ---------------------------------------------------------------------------

@pytest.fixture(scope="function")
def data_dir():
    """每用例独享的临时数据目录。"""
    with tempfile.TemporaryDirectory(prefix="facet-web-") as tmp:
        yield Path(tmp)


@pytest.fixture(scope="function")
def config_path(data_dir):
    path = data_dir / "config.json"
    path.write_text(json.dumps(base_config(), ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path


@pytest.fixture(scope="function")
def app(config_path, data_dir) -> App:
    """不监听端口的 App 实例，供直接操作后端状态或替换 handler 的用例使用。"""
    return App(
        load_config(config_path),
        config_path=config_path,
        state_file=data_dir / "state.json",
        subscriptions_file=data_dir / "subscriptions.json",
        usage_file=data_dir / "usage.json",
        notifications_file=data_dir / "notifications.json",
        audit_file=data_dir / "audit.jsonl",
        keys_file=data_dir / "keys.json",
    )


@pytest.fixture(scope="function")
def gateway_server(app) -> str:
    """在随机端口启动 gateway，返回 base_url。"""
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    httpd.app = app
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=5)


# ---------------------------------------------------------------------------
# API 客户端（标准库，用于落盘交叉断言）
# ---------------------------------------------------------------------------

class ApiClient:
    """最小 HTTP 客户端：默认带 X-Admin-Key，返回解析后的 JSON。"""

    def __init__(self, base_url: str, admin_key: str | None = ADMIN_KEY):
        self.base_url = base_url.rstrip("/")
        self.admin_key = admin_key

    def request(self, method: str, path: str, body: dict | None = None,
                admin_key: str | None = ...):
        key = self.admin_key if admin_key is Ellipsis else admin_key
        headers = {}
        if key:
            headers["X-Admin-Key"] = key
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        req = Request(urljoin(self.base_url + "/", path.lstrip("/")),
                      data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=15) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except HTTPError as exc:
            raw = exc.read().decode("utf-8")
            try:
                payload = json.loads(raw) if raw else {}
            except ValueError:
                payload = {"raw": raw}
            raise ApiError(exc.code, payload) from None

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.request("POST", path, body, **kw)

    def put(self, path, body=None, **kw):
        return self.request("PUT", path, body, **kw)

    def delete(self, path, **kw):
        return self.request("DELETE", path, **kw)


class ApiError(Exception):
    """后端返回非 2xx，携带状态码与解析后的错误体。"""

    def __init__(self, status: int, payload: dict):
        super().__init__(f"HTTP {status}: {payload}")
        self.status = status
        self.payload = payload


@pytest.fixture(scope="function")
def api(gateway_server) -> ApiClient:
    return ApiClient(gateway_server)


@pytest.fixture(scope="function")
def anon_api(gateway_server) -> ApiClient:
    """不带管理密钥的客户端，用于断言 401。"""
    return ApiClient(gateway_server, admin_key=None)


# ---------------------------------------------------------------------------
# 浏览器与页面
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def browser():
    """会话级 Chromium，避免每用例重启浏览器。"""
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        yield b
        b.close()


def _record_outcome(item) -> bool:
    """当前用例是否已失败（call 阶段的 report 在 fixture teardown 之前生成）。"""
    rep = getattr(item, "rep_call", None)
    if rep is not None:
        return bool(rep.failed)
    rep_setup = getattr(item, "rep_setup", None)
    return bool(rep_setup and rep_setup.failed)


def _open_console(browser, gateway_server, request, *, admin: bool):
    """打开控制台并返回 (context, page)。teardown 时按失败情况留证。"""
    context = browser.new_context(viewport={"width": 1280, "height": 800},
                                 locale="zh-CN")
    page = context.new_page()
    if admin:
        page.add_init_script(
            "sessionStorage.setItem('facet_admin_key', %r)" % ADMIN_KEY)
    context.tracing.start(screenshots=True, snapshots=True)
    page.goto(gateway_server + "/", wait_until="domcontentloaded")
    # 等到 boot() 走完 loadState：徽章文案离开初始的「连接中」即视为外壳就绪
    page.wait_for_function(
        "() => { const b = document.getElementById('conn-badge');"
        " return b && b.textContent !== '连接中'; }", timeout=20000)
    try:
        yield page
    finally:
        failed = _record_outcome(request.node)
        slug = request.node.name.replace("/", "_").replace("::", "__")
        target = ARTIFACT_ROOT / slug
        try:
            if failed:
                target.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(target / "failure.png"),
                                full_page=True)
            context.tracing.stop(path=str(target / "trace.zip")
                                 if failed else None)
        except Exception:
            pass
        context.close()


@pytest.fixture(scope="function")
def admin_page(browser, gateway_server, request):
    """已注入管理密钥：懒加载面板可正常取数。"""
    yield from _open_console(browser, gateway_server, request, admin=True)


@pytest.fixture(scope="function")
def page(browser, gateway_server, request):
    """``admin_page`` 的别名，供只读冒烟用例沿用。"""
    yield from _open_console(browser, gateway_server, request, admin=True)


@pytest.fixture(scope="function")
def anon_page(browser, gateway_server, request):
    """未注入密钥：后端返回 401，页面弹出「需要管理密钥」浮层。"""
    yield from _open_console(browser, gateway_server, request, admin=False)


# ---------------------------------------------------------------------------
# Page Object
# ---------------------------------------------------------------------------

@pytest.fixture(scope="function")
def console(page, gateway_server):
    """绑定 ``page`` 的 Page Object。"""
    from ui import Console
    return Console(page, gateway_server)


@pytest.fixture(scope="function")
def anon_console(anon_page, gateway_server):
    """绑定 ``anon_page`` 的 Page Object。"""
    from ui import Console
    return Console(anon_page, gateway_server)


# ---------------------------------------------------------------------------
# 报告钩子：记录各阶段结果，供失败留证判断
# ---------------------------------------------------------------------------

@pytest.hookimpl(tryfirst=True, hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    setattr(item, f"rep_{rep.when}", rep)


# marker 注册见 pyproject.toml [tool.pytest.ini_options]
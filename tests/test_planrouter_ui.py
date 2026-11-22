"""Tests for the browser console and its configuration API."""
from __future__ import annotations

import base64
import io
import json
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from facet.gateway import subscriptions as subs
from facet.gateway.audit import RequestLog
from facet.gateway.core import load_config
from facet.gateway.keys import KeyStore
from facet.gateway.server import App, Handler


def base_config(gateway_key: str | None = None, admin_key: str | None = None) -> dict:
    listen = {"host": "127.0.0.1", "port": 0}
    if gateway_key:
        listen["api_key"] = gateway_key
    if admin_key:
        listen["admin_key"] = admin_key
    return {
        "_readme": "test config",
        "listen": listen,
        "channels": [
            {
                "name": "plan-a", "protocol": "openai", "base_url": "http://127.0.0.1:9001",
                "api_key": "top-secret", "models": {"auto": "model-a"}, "priority": 10,
            },
            {
                "name": "plan-b", "protocol": "openai", "base_url": "http://127.0.0.1:9002",
                "api_key": "env:PLAN_B_KEY", "models": {"auto": "model-b"}, "priority": 20,
            },
            {
                "name": "claude", "protocol": "anthropic", "base_url": "http://127.0.0.1:9003",
                "models": {"auto": "claude-sonnet-4-5"},
            },
        ],
        "routes": [
            {"name": "default", "protocol": "openai", "channels": ["plan-a", "plan-b"]},
            {"name": "default", "protocol": "anthropic", "channels": ["claude"]},
        ],
    }


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        tmp.close()
        self.path = Path(tmp.name)
        self.httpd = None

    def tearDown(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        self.path.unlink(missing_ok=True)
        Path(str(self.path) + ".tmp").unlink(missing_ok=True)
        (self.path.parent / "freealign.jsonl").unlink(missing_ok=True)
        for bak in self.path.parent.glob(self.path.name + ".*.bak"):
            bak.unlink(missing_ok=True)

    def start(self, config: dict | None = None) -> str:
        raw = config if config is not None else base_config()
        self.path.write_text(json.dumps(raw), encoding="utf-8")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.httpd.app = App(load_config(self.path), config_path=self.path)  # type: ignore[attr-defined]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def call(self, base: str, method: str, path: str, body=None, key: str | None = None,
             admin: str | None = None):
        headers = {}
        if key:
            headers["Authorization"] = "Bearer " + key
        if admin is not None:
            headers["X-Admin-Key"] = admin
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        req = Request(base + path, data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=5) as resp:
                return resp.status, resp.headers, resp.read()
        except HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    def test_access_page_warns_on_non_loopback_listen(self):
        """监听地址为非环回时，接入信息页必须给出 HTTP 明文传输的安全提示。"""
        from facet.gateway.ui import STATIC_DIR
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
        # 接入信息页顶部的常驻警告条容器（默认隐藏，非环回时显示）
        self.assertIn('id="access-security-note"', html)
        # 前端从 /api/state 的 listen.host 判断回环地址
        self.assertIn("loopbackHosts", js)
        self.assertIn("renderAccessSecurity", js)
        # 提示文案要说明明文风险并给出可行建议
        self.assertIn("明文", html)
        self.assertIn("反向代理", html)
        # .banner 的 display:flex 会压过 UA 的 [hidden] 样式，必须显式覆盖，
        # 否则回环地址下警告条仍然渲染（冒烟发现过的回归）
        self.assertIn(".banner[hidden]", css)

    def test_request_log_supports_channel_filter_and_limit(self):
        """请求日志 API 支持按渠道筛选（跨归档回溯）并与 limit 参数组合。"""
        base = self.start()
        app = self.httpd.app  # type: ignore[union-attr]
        audit_path = self.path.parent / "audit-channel-test.jsonl"
        self.addCleanup(lambda: audit_path.unlink(missing_ok=True))
        app.audit = RequestLog(audit_path)
        app.audit.record(channel="plan-a", status="ok", attempt=0)
        app.audit.record(channel="plan-b", status="ok", attempt=0)
        app.audit.record(channel="plan-a", status="network_error", attempt=1)

        _, _, body = self.call(base, "GET", "/api/requests")
        payload = json.loads(body)
        self.assertEqual(len(payload["entries"]), 3)

        _, _, body = self.call(base, "GET", "/api/requests?channel=plan-a")
        payload = json.loads(body)
        self.assertEqual(len(payload["entries"]), 2)
        self.assertTrue(all(e["channel"] == "plan-a" for e in payload["entries"]))

        _, _, body = self.call(base, "GET", "/api/requests/2")
        self.assertEqual(len(json.loads(body)["entries"]), 2)

        # 渠道筛选与路径 limit 参数可组合
        _, _, body = self.call(base, "GET", "/api/requests/1?channel=plan-a")
        payload = json.loads(body)
        self.assertEqual(len(payload["entries"]), 1)
        self.assertEqual(payload["entries"][0]["channel"], "plan-a")

    def test_request_log_page_offers_channel_filter_and_history(self):
        """请求日志页提供渠道筛选、更多历史入口与尝试明细的附加标记。"""
        from facet.gateway.ui import STATIC_DIR
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        # 渠道筛选下拉 + 加载更多按钮
        self.assertIn('id="requests-channel-filter"', html)
        self.assertIn('id="btn-more-requests"', html)
        # 前端把渠道筛选透传给后端（跨归档），并维护可调的加载数量
        self.assertIn("requestsLimit", js)
        self.assertIn("requests-channel-filter", js)
        self.assertIn("btn-more-requests", js)
        # 尝试时间线补渲染审计里已有但未展示的字段标记
        self.assertIn("模型未验证", js)
        self.assertIn("含图像", js)
        self.assertIn("粘性", js)

    def test_tables_show_loading_state_and_load_errors_are_visible(self):
        """表格首载显示"加载中"，页面进入时的加载失败不再静默吞错。"""
        from facet.gateway.ui import STATIC_DIR
        js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
        # 统一的加载中辅助 + 脉冲样式（与"还没有数据"的静态空态区分）
        self.assertIn("function markLoading", js)
        self.assertIn("加载中…", js)
        self.assertIn("loading-note", js)
        self.assertIn("loading-note", css)
        self.assertIn("@keyframes", css)
        # 主表格（请求日志/用量模型/API Key/成员用量）首载都给出加载中反馈
        self.assertIn("markLoading('requests-body'", js)
        self.assertIn("markLoading('usage-models-body'", js)
        self.assertIn("markLoading('api-keys-body'", js)
        self.assertIn("markLoading('member-usage-body'", js)
        # 用户进入页面触发的加载失败必须可见（toast）；后台轮询失败不 toast
        # 刷屏，交给 markConn 如实翻徽章（断连诚实化见其专属测试）
        self.assertIn("loadRequests().catch((e) => toast(e.message, 'bad'))", js)
        self.assertIn("loadChannelUsage().catch((e) => toast(e.message, 'bad'))", js)
        self.assertIn("loadMaintLogs().catch((e) => toast(e.message, 'bad'))", js)
        self.assertIn("loadRequests().then(() => markConn(true), () => markConn(false))", js)

    def test_connection_badge_is_honest_about_disconnects(self):
        """断连后徽章必须说实话：轮询失败翻成「已断开」，恢复后提示并撤销横幅。"""
        from facet.gateway.ui import STATIC_DIR
        js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        css = (STATIC_DIR / "style.css").read_text(encoding="utf-8")
        # 失败计数防抖 + 统一的状态翻转助手（单次抖动不惊扰，连续失败才切态）
        self.assertIn("function markConn(", js)
        self.assertIn("connFails", js)
        self.assertIn("已断开 · 自动重试中", js)
        self.assertIn("已重新连接", js)
        # 5s 轮询不再静默吞错：成败两路都喂给 markConn
        self.assertIn("if (state.tab === 'dashboard') loadDashboard().then(() => markConn(true), () => markConn(false));", js)
        self.assertIn("if (state.tab === 'status') loadState().then(() => markConn(true), () => markConn(false));", js)
        self.assertIn("if (state.tab === 'requests') loadRequests().then(() => markConn(true), () => markConn(false));", js)
        # 断连横幅：默认隐藏，复用 banner 体系（含 .banner[hidden] 覆盖规则）
        self.assertIn('id="conn-banner" hidden', html)
        self.assertIn("conn-banner", js)
        self.assertIn(".banner[hidden]", css)
        # 首连失败指数退避重试，而不是留下一张死页面
        self.assertIn("BOOT_RETRY_DELAYS", js)
        self.assertIn("连接失败 · 自动重试中", js)

    def test_modal_editors_confirm_before_discarding_changes(self):
        """编辑弹窗装关闭守卫：有改动未保存时先确认，保存成功直接关闭。

        快照覆盖 DOM 顺序的全部输入，天然包含渠道编辑器的动态行与目录复选框。
        直接把 closeModal 当事件处理器传会把事件对象当 force 实参（truthy），
        绕过确认——所有关闭按钮必须包一层箭头函数。
        """
        from facet.gateway.ui import STATIC_DIR
        js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        # 守卫三件套：快照 / 装载 / force 旁路
        self.assertIn("let modalDirtyGuard", js)
        self.assertIn("function formSnapshot", js)
        self.assertIn("function armDirtyGuard", js)
        self.assertIn("function closeModal(force = false)", js)
        self.assertIn("modalDirtyGuard = null;", js)
        # 确认文案
        self.assertIn("有未保存的修改", js)
        self.assertIn("确定要放弃", js)
        # 四类编辑器装守卫（渠道/路由/厂家/账号）
        self.assertIn("armDirtyGuard('editor-form')", js)
        self.assertIn("armDirtyGuard('route-form')", js)
        self.assertIn("armDirtyGuard('vendor-form')", js)
        self.assertIn("armDirtyGuard('account-form')", js)
        # 保存成功路径绕过守卫直接关闭（渠道/路由/厂家/账号 ≥4 处）
        self.assertGreaterEqual(js.count("closeModal(true)"), 4)
        # 关闭按钮一律包箭头函数，防止事件对象被当作 force 实参
        self.assertIn("$('modal-close').onclick = () => closeModal();", js)
        self.assertIn("$('editor-cancel').onclick = () => closeModal();", js)
        self.assertIn("$('vendor-cancel').onclick = () => closeModal();", js)
        self.assertIn("$('route-cancel').onclick = () => closeModal();", js)
        self.assertIn("[data-account=\"cancel\"]').onclick = () => closeModal();", js)
        # Esc 关闭路径保持直呼（无实参即走守卫）
        self.assertIn("e.preventDefault(); closeModal(); return;", js)

    def test_page_and_static_assets(self):
        base = self.start()
        status, _, body = self.call(base, "GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Facet", body)
        self.assertIn(b"/static/app.js", body)
        status, headers, body = self.call(base, "GET", "/static/style.css")
        self.assertEqual(status, 200)
        self.assertIn("text/css", headers.get("Content-Type"))
        self.assertIn(b":root", body)
        status, _, _ = self.call(base, "GET", "/static/%2e%2e%2fserver.py")
        self.assertEqual(status, 404)

    def test_config_masks_plaintext_secrets(self):
        base = self.start()
        _, _, body = self.call(base, "GET", "/api/config")
        config = json.loads(body)
        channels = {c["name"]: c for c in config["channels"]}
        self.assertEqual(channels["plan-a"]["api_key"], "***")
        self.assertEqual(channels["plan-b"]["api_key"], "env:PLAN_B_KEY")
        self.assertEqual(config["_readme"], "test config")

    def test_first_run_sets_the_management_password(self):
        # Fresh seed: no management password yet, so the loopback console is
        # open and reports admin_key_set=False (the browser then forces the
        # operator to pick a password before anything can be used).
        base = self.start(base_config())
        status, _, body = self.call(base, "GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["listen"]["admin_key_set"])

        # The browser posts the new password with an empty old_key.
        status, _, body = self.call(base, "POST", "/api/admin/key",
                                    body={"key": "my-pass", "old_key": ""})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["admin_key"], "my-pass")

        # From now on the management API requires it.
        status, _, _ = self.call(base, "GET", "/api/config")
        self.assertEqual(status, 401)
        status, _, _ = self.call(base, "GET", "/api/config", admin="my-pass")
        self.assertEqual(status, 200)
        status, _, body = self.call(base, "GET", "/api/state", admin="my-pass")
        self.assertTrue(json.loads(body)["listen"]["admin_key_set"])

    def test_api_requires_admin_key_but_page_does_not(self):
        # Gateway key without an admin_key locks the management API: there is
        # no fallback password for hand-written configs.
        base = self.start(base_config(gateway_key="gw-secret"))
        status, _, _ = self.call(base, "GET", "/api/config")
        self.assertEqual(status, 401)
        status, _, _ = self.call(base, "GET", "/api/config", key="gw-secret")
        self.assertEqual(status, 401)
        for guess in ("admin", "gw-secret", ""):
            status, _, _ = self.call(base, "GET", "/api/config", admin=guess)
            self.assertEqual(status, 401)
        status, _, _ = self.call(base, "GET", "/")
        self.assertEqual(status, 200)

    def test_api_accepts_only_the_configured_admin_key(self):
        base = self.start(base_config(gateway_key="gw-secret", admin_key="mine"))
        status, _, _ = self.call(base, "GET", "/api/config", admin="mine")
        self.assertEqual(status, 200)
        status, _, _ = self.call(base, "GET", "/api/config", admin="admin")
        self.assertEqual(status, 401)
        status, _, _ = self.call(base, "GET", "/api/config", admin="wrong")
        self.assertEqual(status, 401)
        status, _, _ = self.call(base, "GET", "/api/config")
        self.assertEqual(status, 401)

    def test_save_hot_reloads_and_preserves_secrets_and_state(self):
        base = self.start()
        app = self.httpd.app  # type: ignore[union-attr]
        app.store.states["plan-a"].mark_cooldown(3600, "quota", time.time())

        _, _, body = self.call(base, "GET", "/api/config")
        config = json.loads(body)
        config["channels"].append({
            "name": "plan-c", "protocol": "openai", "base_url": "http://127.0.0.1:9004",
            "api_key": "env:PLAN_C_KEY", "models": {"auto": "model-c"}, "priority": 30,
            # New channels must carry a vendor binding (see
            # test_planrouter_vendor_binding.py); nvidia/payg is a
            # builtin registry entry.
            "provider": "nvidia", "mode": "payg",
        })
        config["routes"][0]["channels"].append("plan-c")
        status, _, body = self.call(base, "POST", "/api/config", config, key="")
        self.assertEqual(status, 200, body.decode())
        self.assertEqual(json.loads(body)["channels"], 4)

        on_disk = json.loads(self.path.read_text(encoding="utf-8"))
        keys = {c["name"]: c.get("api_key") for c in on_disk["channels"]}
        self.assertEqual(keys["plan-a"], "top-secret")
        self.assertEqual(keys["plan-c"], "env:PLAN_C_KEY")
        self.assertNotIn("api_key", on_disk["listen"])

        _, _, state = self.call(base, "GET", "/api/state")
        payload = json.loads(state)
        names = [c["name"] for c in payload["channels"]]
        self.assertIn("plan-c", names)
        self.assertEqual(payload["routes"]["openai:default"], ["plan-a", "plan-b", "plan-c"])
        self.assertTrue(next(c for c in payload["channels"] if c["name"] == "plan-a")["cooling"])

    def test_invalid_config_is_rejected_without_touching_disk(self):
        base = self.start()
        before = self.path.read_bytes()
        _, _, body = self.call(base, "GET", "/api/config")
        config = json.loads(body)
        config["routes"][0]["channels"].append("no-such-channel")  # unknown channel
        status, _, body = self.call(base, "POST", "/api/config", config)
        self.assertEqual(status, 400)
        self.assertIn("未知渠道".encode("utf-8"), body)
        self.assertEqual(self.path.read_bytes(), before)

    def test_reset_cooldown_and_reload_from_disk(self):
        base = self.start()
        app = self.httpd.app  # type: ignore[union-attr]
        app.store.states["plan-b"].mark_cooldown(7200, "quota", time.time())
        status, _, body = self.call(base, "POST", "/api/channels/plan-b/reset")
        self.assertEqual(status, 200)
        _, _, state = self.call(base, "GET", "/api/state")
        self.assertFalse(next(c for c in json.loads(state)["channels"] if c["name"] == "plan-b")["cooling"])

        raw = json.loads(self.path.read_text(encoding="utf-8"))
        raw["channels"][0]["priority"] = 99
        self.path.write_text(json.dumps(raw), encoding="utf-8")
        status, _, _ = self.call(base, "POST", "/api/reload")
        self.assertEqual(status, 200)
        _, _, body = self.call(base, "GET", "/api/config")
        self.assertEqual(json.loads(body)["channels"][0]["priority"], 99)

    def test_guide_endpoint_serves_manual(self):
        base = self.start()
        status, _, body = self.call(base, "GET", "/api/guide")
        self.assertEqual(status, 200)
        guide = json.loads(body)
        self.assertTrue(guide["available"])
        self.assertIn("3.1 NVIDIA NIM——免费层", guide["markdown"])
        self.assertIn("5.4 Z.AI（智谱海外）", guide["markdown"])

    def test_guide_serves_user_guide_and_blocks_unknown_docs(self):
        # ?doc=user-guide serves the task-based user manual (docs/user-guide.md);
        # any other name falls back to the default manual, never the filesystem.
        base = self.start()
        status, _, body = self.call(base, "GET", "/api/guide?doc=user-guide")
        self.assertEqual(status, 200)
        guide = json.loads(body)
        self.assertTrue(guide["available"])
        self.assertIn("第一次部署", guide["markdown"])
        self.assertIn("整机迁移", guide["markdown"])
        status, _, body = self.call(base, "GET", "/api/guide?doc=../../secrets")
        guide = json.loads(body)
        self.assertTrue(guide["available"])
        self.assertIn("3.1 NVIDIA NIM——免费层", guide["markdown"])

    def test_guide_image_route_confines_to_docs(self):
        base = self.start()
        # A real file that exists in the repo but outside docs/ must be off-limits.
        status, _, _ = self.call(base, "GET", "/api/guide/img/..%2F..%2Fsrc%2Ffacet%2Fgateway%2Fproviders.json")
        self.assertEqual(status, 404)
        status, _, _ = self.call(base, "GET", "/api/guide/img/images/api-key-guide/missing.png")
        self.assertEqual(status, 404)

    def test_freealign_logs_reads_jsonl_history(self):
        base = self.start()
        # Written by _record_free_align: the patch itself never hits the disk.
        entry = {"ts": "2026-11-09T10:00:00", "trigger": "scheduled",
                 "aligned": [{"channel": "plan-a", "models": ["m1"], "changed": True}],
                 "alerts": []}
        (self.path.parent / "freealign.jsonl").write_text(
            json.dumps(entry) + "\n", encoding="utf-8")
        status, _, body = self.call(base, "GET", "/api/freealign/logs")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["source"], "file")
        self.assertEqual(payload["logs"][0]["trigger"], "scheduled")
        self.assertNotIn("patch", payload["logs"][0])

    def test_backups_lists_bak_files_newest_first(self):
        base = self.start()
        old = self.path.with_name(self.path.name + ".maintenance.bak")
        new = self.path.with_name(self.path.name + ".freealign.bak")
        old.write_text("{}", encoding="utf-8")
        time.sleep(0.02)
        new.write_text("{}", encoding="utf-8")
        status, _, body = self.call(base, "GET", "/api/backups")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual([b["tag"] for b in payload["backups"]], ["freealign", "maintenance"])
        self.assertTrue(all(b["size"] >= 0 and b["mtime"] > 0 for b in payload["backups"]))
        # The live config itself is never listed as a backup.
        self.assertEqual(payload["config"], self.path.name)

    def test_maintenance_logs_limit_is_parsed(self):
        base = self.start()
        status, _, body = self.call(base, "GET", "/api/maintenance/logs/100")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["limit"], 100)

    # ------------------------------------------------------------------
    # /api/recommend — agent / dev model recommendations.
    # ------------------------------------------------------------------

    def test_recommend_requires_admin_key(self):
        base = self.start(base_config(gateway_key="gw-secret"))
        # Without admin header → 401.
        status, _, _ = self.call(base, "GET", "/api/recommend?purpose=agent")
        self.assertEqual(status, 401)
        # Gateway key alone is not an admin credential.
        status, _, _ = self.call(base, "GET", "/api/recommend", key="gw-secret")
        self.assertEqual(status, 401)
        # No admin_key is configured, so the management API stays locked
        # instead of accepting "admin" (there is no default password).
        status, _, _ = self.call(base, "GET", "/api/recommend?purpose=agent", admin="admin")
        self.assertEqual(status, 401)

    def test_recommend_accepts_only_the_configured_admin_key(self):
        base = self.start(base_config(gateway_key="gw-secret", admin_key="adm-secret"))
        status, _, _ = self.call(base, "GET", "/api/recommend?purpose=agent", admin="admin")
        self.assertEqual(status, 401)
        status, _, body = self.call(base, "GET", "/api/recommend?purpose=agent",
                                    admin="adm-secret")
        self.assertEqual(status, 200, body)

    def test_recommend_agent_prefers_tool_call_then_free(self):
        base = self.start()
        app = self.httpd.app  # type: ignore[union-attr]
        # Catalogs: gpt-4o resolves to a registry key with tool_call=True;
        # model-a / model-b are unknown to the registry (tool_call=None).
        app.store.record_catalog("plan-a", ["gpt-4o", "model-a"], "http://a/v1")
        app.store.record_catalog("plan-b", ["model-b"], "http://b/v1")
        status, _, body = self.call(base, "GET", "/api/recommend?purpose=agent", admin="admin")
        self.assertEqual(status, 200, body)
        payload = json.loads(body)
        self.assertEqual(payload["purpose"], "agent")
        items = payload["items"]
        wires = [it["wire_model"] for it in items]
        # tool_call=True must come before unknown models.
        self.assertEqual(wires[0], "gpt-4o")
        # The known tool-call row is marked matched and tool_call=True.
        top = items[0]
        self.assertTrue(top["matched"])
        self.assertTrue(top["tool_call"])
        # Unknown models are counted in unknown_capability.
        self.assertGreaterEqual(payload["unknown_capability"], 1)

    def test_recommend_agent_orders_by_benchmark_score(self):
        base = self.start()
        app = self.httpd.app  # type: ignore[union-attr]
        # Both resolve to registry keys with tool_call=True on the same
        # channel, so tool bucket and tier tie; the bundled benchmark
        # aggregate decides: sonnet-5-5 (75.3) before gpt-5.6 (49.5).
        app.store.record_catalog(
            "plan-a", ["openai/gpt-5.6", "anthropic/claude-sonnet-5-5"], "http://a/v1")
        status, _, body = self.call(base, "GET", "/api/recommend?purpose=agent",
                                    admin="admin")
        self.assertEqual(status, 200, body)
        items = json.loads(body)["items"]
        self.assertEqual([it["wire_model"] for it in items],
                         ["anthropic/claude-sonnet-5-5", "openai/gpt-5.6"])
        self.assertGreater(items[0]["agent_score"], items[1]["agent_score"])
        # Runtime selection (facet-agent attempts) keeps the same order.
        self.assertEqual(
            app._agent_candidates()[:2],
            [("plan-a", "anthropic/claude-sonnet-5-5"),
             ("plan-a", "openai/gpt-5.6")])

    def test_recommend_agent_ranks_scored_before_unscored(self):
        base = self.start()
        app = self.httpd.app  # type: ignore[union-attr]
        # gpt-4o is registry-known tool_call=True but has no bundled agent
        # score; a scored model must rank before it within the same group.
        app.store.record_catalog(
            "plan-a", ["gpt-4o", "anthropic/claude-sonnet-5-5"], "http://a/v1")
        status, _, body = self.call(base, "GET", "/api/recommend?purpose=agent",
                                    admin="admin")
        self.assertEqual(status, 200, body)
        items = json.loads(body)["items"]
        self.assertEqual([it["wire_model"] for it in items],
                         ["anthropic/claude-sonnet-5-5", "gpt-4o"])
        self.assertIsNone(items[1]["agent_score"])

    def test_recommend_agent_excludes_tool_less_models(self):
        base = self.start()
        app = self.httpd.app  # type: ignore[union-attr]
        # aimlapi/gemma-2-27b-it is a registry entry with tool_call=False.
        app.store.record_catalog(
            "plan-a", ["aimlapi/gemma-2-27b-it", "gpt-4o"], "http://a/v1")
        status, _, body = self.call(base, "GET", "/api/recommend?purpose=agent",
                                    admin="admin")
        self.assertEqual(status, 200, body)
        wires = [it["wire_model"] for it in json.loads(body)["items"]]
        self.assertNotIn("aimlapi/gemma-2-27b-it", wires)
        self.assertIn("gpt-4o", wires)

    def test_recommend_skips_dead_and_blocked_channels(self):
        base = self.start()
        app = self.httpd.app  # type: ignore[union-attr]
        app.store.record_catalog("plan-a", ["gpt-4o", "model-a"], "http://a/v1")
        app.store.record_catalog("plan-b", ["model-b"], "http://b/v1")
        # Mark gpt-4o on plan-a as dead (probe-verified).
        from facet.gateway.maintenance import label_dead_model
        label_dead_model(app, "plan-a", "gpt-4o", ok=False, error="HTTP 500")
        status, _, body = self.call(base, "GET", "/api/recommend?purpose=agent", admin="admin")
        self.assertEqual(status, 200, body)
        items = json.loads(body)["items"]
        wires = {it["wire_model"]: [it["channel"] for it in items if it["wire_model"] == it["wire_model"]]
                 for it in items}
        # gpt-4o must be excluded entirely: dead on plan-a and not listed on plan-b.
        gpt_rows = [it for it in items if it["wire_model"] == "gpt-4o"]
        self.assertEqual(gpt_rows, [])
        # model-a still comes from plan-a, model-b from plan-b.
        self.assertIn("model-a", wires)
        self.assertIn("model-b", wires)

    def test_recommend_deduplicates_same_wire_across_channels(self):
        base = self.start()
        app = self.httpd.app  # type: ignore[union-attr]
        # Same wire model on two channels → one row, the best channel picked.
        app.store.record_catalog("plan-a", ["gpt-4o"], "http://a/v1")
        app.store.record_catalog("plan-b", ["gpt-4o"], "http://b/v1")
        status, _, body = self.call(base, "GET", "/api/recommend?purpose=agent", admin="admin")
        self.assertEqual(status, 200, body)
        items = json.loads(body)["items"]
        gpt_rows = [it for it in items if it["wire_model"] == "gpt-4o"]
        self.assertEqual(len(gpt_rows), 1)
        # Both channels offer the same registry match; pick whichever
        # _row_better prefers (free beats paid, then higher score).
        self.assertIn(gpt_rows[0]["channel"], ("plan-a", "plan-b"))


class MigrationPackageTests(unittest.TestCase):
    """One-click whole-machine migration: export/import zip of the config dir.

    The console cannot move a deployment today: config backups cover only
    router.json and the config export masks every secret, so moving to a new
    machine means hand-copying keys.json / subscriptions.json / usage.json.
    These tests pin the migration bundle: everything the gateway persists in
    the config dir travels in one zip, import restores it with hot reload and
    snapshot backups, and the local management password never changes.
    """

    def setUp(self):
        self._tmpdirs = []

    def tearDown(self):
        import shutil
        for path in self._tmpdirs:
            shutil.rmtree(path, ignore_errors=True)

    def _dir(self) -> Path:
        import tempfile
        path = Path(tempfile.mkdtemp(prefix="facet-migration-"))
        self._tmpdirs.append(path)
        return path

    def _seed_files(self, root: Path) -> dict:
        """Write config + keys + subscriptions + usage like a lived-in deploy."""
        raw = base_config(admin_key="source-pass")
        raw["channels"][0]["api_key"] = "top-secret"
        config_path = root / "router.json"
        config_path.write_text(json.dumps(raw), encoding="utf-8")
        keys_path = root / "keys.json"
        store = KeyStore(keys_path)
        member = store.create("alice")
        subs_path = root / "subscriptions.json"
        subs.save_file(subs_path, [{"plan": "pro", "price": 20}], None)
        usage_path = root / "usage.json"
        usage_path.write_text(json.dumps({
            "days": {"2026-11-20": {"plan-a": {
                "requests": 3, "success": 2, "failed": 1, "tokens": 100}}}},
        ), encoding="utf-8")
        return {"config": config_path, "keys": keys_path, "member": member,
                "subs": subs_path, "usage": usage_path, "raw": raw}

    def _start(self, root: Path, admin_key: str | None = None) -> tuple[str, App]:
        if admin_key is not None:
            raw = json.loads((root / "router.json").read_text(encoding="utf-8"))
            raw.setdefault("listen", {})["admin_key"] = admin_key
            (root / "router.json").write_text(json.dumps(raw), encoding="utf-8")
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        httpd.daemon_threads = True
        httpd.app = App(
            load_config(root / "router.json"), config_path=root / "router.json",
            keys_file=root / "keys.json", subscriptions_file=root / "subscriptions.json",
            usage_file=root / "usage.json")  # type: ignore[attr-defined]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        self.addCleanup(httpd.server_close)
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        return base, httpd.app  # type: ignore[return-value]

    def _export(self, base: str, admin: str = "source-pass"):
        status, _, body = self.call(base, "GET", "/api/migration/export", admin=admin)
        return status, json.loads(body) if status == 200 else body

    def call(self, base: str, method: str, path: str, body=None, admin: str | None = None):
        headers = {}
        if admin:
            headers["X-Admin-Key"] = admin
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        req = Request(base + path, data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=5) as resp:
                return resp.status, resp.headers, resp.read()
        except HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    def test_export_zip_carries_every_persistent_file_unmasked(self):
        root = self._dir()
        seed = self._seed_files(root)
        base, _ = self._start(root)
        status, payload = self._export(base)
        self.assertEqual(status, 200)
        self.assertTrue(payload["filename"].endswith(".zip"))
        blob = base64.b64decode(payload["data"])
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            names = set(zf.namelist())
            self.assertEqual(names, {"router.json", "keys.json",
                                     "subscriptions.json", "usage.json"})
            # The whole point: channel secrets travel in plaintext so the
            # target machine can serve requests without re-entering keys.
            shipped = json.loads(zf.read("router.json").decode("utf-8"))
            self.assertEqual(shipped["channels"][0]["api_key"], "top-secret")
            member_keys = json.loads(zf.read("keys.json").decode("utf-8"))
            self.assertEqual(member_keys["keys"][0]["key"], seed["member"]["key"])
        self.assertEqual({c["name"] for c in payload["contents"]}, names)

    def test_export_with_config_only(self):
        root = self._dir()
        (root / "router.json").write_text(
            json.dumps(base_config(admin_key="source-pass")), encoding="utf-8")
        base, _ = self._start(root)
        status, payload = self._export(base)
        self.assertEqual(status, 200)
        self.assertEqual([c["name"] for c in payload["contents"]], ["router.json"])

    def test_import_requires_admin_key(self):
        root = self._dir()
        self._seed_files(root)
        base, _ = self._start(root)
        status, _, _ = self.call(base, "GET", "/api/migration/export")
        self.assertEqual(status, 401)

    def test_import_rejects_bad_payloads(self):
        root = self._dir()
        self._seed_files(root)
        base, _ = self._start(root)
        for data in ("not-base64!!", ""):
            status, _, body = self.call(base, "POST", "/api/migration/import",
                                        body={"data": data}, admin="source-pass")
            self.assertEqual(status, 400, body)
        # A valid zip without router.json is not a migration package.
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("keys.json", "{}")
        status, _, body = self.call(base, "POST", "/api/migration/import",
                                    body={"data": base64.b64encode(buf.getvalue()).decode()},
                                    admin="source-pass")
        self.assertEqual(status, 400)
        self.assertIn("router.json", body.decode("utf-8"))

    def test_import_moves_everything_and_hot_reloads(self):
        source = self._dir()
        seed = self._seed_files(source)
        source_base, source_app = self._start(source)
        _, exported = self._export(source_base)

        target = self._dir()
        (target / "router.json").write_text(
            json.dumps(base_config(admin_key="target-pass")), encoding="utf-8")
        target_base, target_app = self._start(target)
        status, _, body = self.call(
            target_base, "POST", "/api/migration/import",
            body={"data": exported["data"]}, admin="target-pass")
        self.assertEqual(status, 200, body)
        result = json.loads(body)
        self.assertTrue(result["ok"])
        self.assertIn("router.json", result["imported"])
        self.assertIn("keys.json", result["imported"])
        self.assertIn("subscriptions.json", result["imported"])
        self.assertIn("usage.json", result["imported"])

        # Config travels whole (source channels appear) and hot-reloads.
        status, _, body = self.call(target_base, "GET", "/api/config",
                                    admin="target-pass")
        names = {c["name"] for c in json.loads(body)["channels"]}
        self.assertEqual(names, {"plan-a", "plan-b", "claude"})
        # Member keys keep working on the target machine.
        self.assertIsNotNone(target_app.keys.lookup(seed["member"]["key"]))
        # Subscriptions and usage are reloaded into the running process.
        self.assertEqual(target_app.subscriptions[0]["plan"], "pro")
        self.assertIn("2026-11-20", target_app.usage.data["days"])

    def test_import_preserves_local_admin_key(self):
        source = self._dir()
        self._seed_files(source)
        source_base, _ = self._start(source)
        _, exported = self._export(source_base)

        target = self._dir()
        (target / "router.json").write_text(
            json.dumps(base_config(admin_key="target-pass")), encoding="utf-8")
        target_base, _ = self._start(target)
        status, _, body = self.call(
            target_base, "POST", "/api/migration/import",
            body={"data": exported["data"]}, admin="target-pass")
        self.assertEqual(status, 200, body)
        # The importing operator would be locked out if the source machine's
        # management password silently replaced theirs.
        on_disk = json.loads((target / "router.json").read_text(encoding="utf-8"))
        self.assertEqual(on_disk["listen"]["admin_key"], "target-pass")
        status, _, _ = self.call(target_base, "GET", "/api/config", admin="target-pass")
        self.assertEqual(status, 200)
        status, _, _ = self.call(target_base, "GET", "/api/config", admin="source-pass")
        self.assertEqual(status, 401)

    def test_import_snapshots_overwritten_files(self):
        source = self._dir()
        self._seed_files(source)
        source_base, _ = self._start(source)
        _, exported = self._export(source_base)

        target = self._dir()
        self._seed_files(target)
        target_base, _ = self._start(target)
        # App startup annotates channel ids on disk, so capture the files
        # that are actually live at import time.
        old_config = (target / "router.json").read_text(encoding="utf-8")
        old_keys = (target / "keys.json").read_text(encoding="utf-8")
        status, _, body = self.call(
            target_base, "POST", "/api/migration/import",
            body={"data": exported["data"]}, admin="source-pass")
        self.assertEqual(status, 200, body)
        backup = (target / "router.json.migration.bak")
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.read_text(encoding="utf-8"), old_config)
        keys_backup = (target / "keys.json.migration.bak")
        self.assertTrue(keys_backup.is_file())
        self.assertEqual(keys_backup.read_text(encoding="utf-8"), old_keys)

    def test_import_ignores_unrelated_zip_members(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("router.json", json.dumps(base_config(admin_key="source-pass")))
            zf.writestr("keys.json", json.dumps({"keys": [
                {"key": "sk-member-abc", "name": "bob", "created": 0, "enabled": True}]}))
            # Path traversal and junk entries must never reach the config dir.
            zf.writestr("../evil.txt", "nope")
            zf.writestr("sub/../../deep.txt", "nope")
        target = self._dir()
        (target / "router.json").write_text(
            json.dumps(base_config(admin_key="target-pass")), encoding="utf-8")
        base, app = self._start(target)
        status, _, body = self.call(
            base, "POST", "/api/migration/import",
            body={"data": base64.b64encode(buf.getvalue()).decode()},
            admin="target-pass")
        self.assertEqual(status, 200, body)
        self.assertEqual([p.name for p in target.iterdir() if p.name.startswith("keys")],
                         ["keys.json"])


class MemberKeyRotationTests(unittest.TestCase):
    """POST /api/keys/rotate：一键换发成员 Key，旧值立即失效，配额保留。"""

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        tmp.close()
        self.path = Path(tmp.name)
        self.keys_path = Path(tmp.name + ".keys.json")
        self.httpd = None

    def tearDown(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        self.path.unlink(missing_ok=True)
        self.keys_path.unlink(missing_ok=True)

    def start(self) -> str:
        raw = base_config(admin_key="pass")
        self.path.write_text(json.dumps(raw), encoding="utf-8")
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.httpd.app = App(load_config(self.path), config_path=self.path,
                             keys_file=self.keys_path)  # type: ignore[attr-defined]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def call(self, base: str, method: str, path: str, body=None, admin: str | None = None,
             key: str | None = None):
        headers = {}
        if admin is not None:
            headers["X-Admin-Key"] = admin
        if key:
            headers["Authorization"] = "Bearer " + key
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        req = Request(base + path, data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=5) as resp:
                return resp.status, resp.headers, resp.read()
        except HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    def test_rotate_endpoint_swaps_the_secret_in_place(self):
        store = KeyStore(self.keys_path)
        record = store.create("alice", limits={"rpm": 30})
        base = self.start()
        status, _, body = self.call(base, "POST", "/api/keys/rotate",
                                    body={"key": record["key"]}, admin="pass")
        self.assertEqual(status, 200, body)
        payload = json.loads(body)
        self.assertNotEqual(payload["key"], record["key"])
        self.assertTrue(payload["key"].startswith("sk-pr-"))
        self.assertEqual(payload["name"], "alice")
        self.assertEqual(payload["limits"], {"rpm": 30})
        # 旧 key 立即失效，新 key 可用（keys.json 已落盘，需重载验证）
        reloaded = KeyStore(self.keys_path)
        self.assertIsNone(reloaded.lookup(record["key"]))
        self.assertEqual(reloaded.lookup(payload["key"])["name"], "alice")
        # 未知 key 返回 400 并带中文错误
        status, _, body = self.call(base, "POST", "/api/keys/rotate",
                                    body={"key": "sk-pr-" + "0" * 32}, admin="pass")
        self.assertEqual(status, 400)
        self.assertIn("不存在", json.loads(body)["error"]["message"])

    def test_console_has_a_rotate_button_wired_to_the_endpoint(self):
        from facet.gateway.ui import STATIC_DIR
        js = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn('data-rotate=', js)
        self.assertIn('/api/keys/rotate', js)
        self.assertIn('rotateKey', js)


if __name__ == "__main__":
    unittest.main()

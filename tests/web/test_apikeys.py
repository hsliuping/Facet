"""Playwright E2E 用例：WP4-H API Keys

[H1] 生成新 API Key → 列表出现 + 用该 key 调 /v1/models 返回 200 + 后端落盘断言
[H1] 明文仅展示一次：产品目前不满足（renderApiKeys 始终渲染完整明文），标 xfail
"""
from __future__ import annotations

import json
import urllib.request

import pytest

pytest.importorskip("playwright", reason="需要安装 playwright")
from playwright.sync_api import expect


def _create_key(console, name: str) -> str:
    """通过 UI 生成一个新 Key，返回表格里展示的明文。"""
    console.nav("apikeys")
    console.el("#btn-api-key-create").click()
    console.ask_answer(name)
    console.wait_toast("已生成新 Key")
    row = console.el("#api-keys-body tr").filter(has_text=name)
    expect(row).to_have_count(1)
    code = row.locator("code[data-copy]")
    expect(code).to_be_visible()
    key = code.get_attribute("data-copy")
    assert key and key.startswith("sk-pr-"), f"未从表格取到明文 Key：{key!r}"
    return key


@pytest.mark.mutating
def test_h1_create_api_key_and_call_models(console, api, gateway_server, data_dir):
    """[H1] 生成新 API Key → 列表出现 + 明文可取 + Bearer 调 /v1/models 200 + 落盘断言"""
    key = _create_key(console, "e2e-key")

    # UI 断言：行内名称、启用状态与明文一致
    row = console.el("#api-keys-body tr").filter(has_text="e2e-key")
    expect(row.locator("code[data-copy]")).to_have_text(key)
    expect(row).to_contain_text("启用")

    # 后端断言 1：/api/keys 返回同一 key
    payload = api.get("/api/keys")
    record = next((k for k in payload.get("keys", []) if k.get("name") == "e2e-key"), None)
    assert record is not None, "后端 /api/keys 未出现新 Key"
    assert record["key"] == key
    assert record.get("enabled") is True

    # 后端断言 2：keys.json 已落盘（KeyStore 以明文 key 存储）
    keys_file = data_dir / "keys.json"
    assert keys_file.exists(), "keys.json 未生成"
    with keys_file.open(encoding="utf-8") as f:
        on_disk = json.load(f)
    assert any(k.get("key") == key and k.get("name") == "e2e-key"
               for k in on_disk.get("keys", [])), "keys.json 中未找到新 Key"

    # 用新 key 作为 Bearer 调 /v1/models（urllib 直连 gateway_server）
    req = urllib.request.Request(
        f"{gateway_server}/v1/models",
        headers={"Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.status == 200
        body = json.loads(resp.read().decode("utf-8"))
    assert body.get("object") == "list"


@pytest.mark.mutating
@pytest.mark.xfail(
    reason="产品缺陷：app.js renderApiKeys 始终以完整明文渲染 Key"
           "（<code data-copy=\"r.key\">r.key</code>），且 GET /api/keys 返回完整明文，"
           "重新打开面板后明文仍可见，不满足“明文仅展示一次”",
    strict=False,
)
def test_h1_key_masked_after_reopen(console):
    """[H1] 明文仅展示一次：重新打开面板后应为掩码或不可见（当前产品不满足，xfail）"""
    key = _create_key(console, "e2e-mask-check")

    # 离开再重新进入 API Keys 面板（重新拉取 /api/keys 渲染）
    console.nav("dashboard")
    console.nav("apikeys")

    row = console.el("#api-keys-body tr").filter(has_text="e2e-mask-check")
    expect(row).to_have_count(1)
    # 渲染层断言：重新打开后不应再展示完整明文
    expect(row.locator("code").first).not_to_contain_text(key)
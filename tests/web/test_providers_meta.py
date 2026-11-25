"""console /api/providers 注入归一化 meta 子对象（mode_meta.py 单点）。

第一步（行为不变）：meta 只是附加字段，前端向导/渠道页据此消费分类；
同时断言注入发生在响应拷贝上，不污染内存注册表（maintenance 会把
app.providers 原样写回 overlay，污染会扩散到磁盘）。
"""
from __future__ import annotations


def test_providers_api_injects_mode_meta(api, app):
    payload = api.get('/api/providers')
    providers = payload['providers']
    assert providers, "内置注册表不应为空"

    or_entry = next(p for p in providers if p['id'] == 'openrouter')
    meta = or_entry['modes']['payg']['meta']
    assert meta == {
        "free_kind": "forever",
        "free_kinds": ["forever", "daily"],
        "cred_kind": "key",
        "requires_checkin": False,
        "quota_capability": "key_api",
    }

    # aksk 型厂家：火山 payg（credential_envs 两个管理面凭据 + cmd）。
    volc = next(p for p in providers if p['id'] == 'volcengine')
    vmeta = volc['modes']['payg']['meta']
    assert vmeta['cred_kind'] == 'aksk'
    assert vmeta['free_kind'] == 'one_time'
    assert vmeta['quota_capability'] == 'mgmt_api'

    # 签到厂家：魔搭。
    ms = next(p for p in providers if p['id'] == 'modelscope')
    assert ms['modes']['payg']['meta']['requires_checkin'] is True

    # 注入只发生在响应上：内存注册表条目保持干净，防止 overlay 落盘污染。
    for entry in app.providers:
        for mode in (entry.get("modes") or {}).values():
            assert "meta" not in mode, entry.get("id")


def test_every_builtin_mode_carries_meta(api):
    payload = api.get('/api/providers')
    count = 0
    for provider in payload['providers']:
        for mode in (provider.get('modes') or {}).values():
            assert isinstance(mode.get('meta'), dict), provider['id']
            for key in ("free_kind", "free_kinds", "cred_kind",
                        "requires_checkin", "quota_capability"):
                assert key in mode['meta'], (provider['id'], key)
            count += 1
    assert count >= 30, f"内置注册表应有 30+ mode，实际 {count}"


def test_free_models_payload_channels_declare_free_kind(data_dir):
    """免费模型页四类分组的后端契约：每个渠道行携带 free_kind（PPT 口径，
    mode_meta 单点分类）。用魔搭渠道锁定 daily：分组前端无需再推断。"""
    import json
    from conftest import base_config
    from facet.gateway.core import load_config
    from facet.gateway.server import App
    from facet.gateway import maintenance as maint
    from facet.gateway.mode_meta import FREE_KINDS

    cfg = base_config()
    cfg["channels"][0].update({"tier": "free", "provider": "modelscope",
                               "mode": "payg"})
    path = data_dir / "config.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    application = App(
        load_config(path),
        config_path=path,
        state_file=data_dir / "state.json",
        subscriptions_file=data_dir / "subscriptions.json",
        usage_file=data_dir / "usage.json",
        notifications_file=data_dir / "notifications.json",
        audit_file=data_dir / "audit.jsonl",
        keys_file=data_dir / "keys.json",
    )
    payload = maint.free_models_payload(application)
    channels = payload.get("channels") or []
    assert channels, "配置了 tier=free 渠道后 payload 应含该渠道"
    for ch in channels:
        assert ch.get('free_kind') in FREE_KINDS, ch.get('name')
    assert channels[0]['free_kind'] == 'daily'  # modelscope/payg 显式声明

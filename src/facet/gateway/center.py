"""中心服务客户端（设计文档第 5 节 / dev-plan M2 T2.1–T2.8）。

职责与边界：
  * 配置与隐私开关（center.json）：所有回传功能可单独关闭；
    ``enabled=false`` 时一个网络请求都不发（验收 T2.1）。
  * 同步（T2.3）：启动后错峰拉取 registry.json / intel.json /
    releases/latest，带 If-None-Match，Ed25519 验签；断网、超时、验签
    失败一律静默降级用本地缓存，绝不影响网关转发。
  * 合并规则（T2.4）：按「预置指纹」判断渠道是否被用户改过——指纹一致
    自动更新模型与额度并记入消息；指纹不一致或无指纹（旧渠道一律视为
    已改）只提示不覆盖；用户自建（无 provider）不处理；厂家下线只标记。
  * 情报推送（T2.5）：已读游标去重，新情报进消息区并转发 webhook。
  * 更新通知（T2.6）：本地版本 vs releases/latest，仅新版本才提示。
  * 心跳与行动计数（T2.7）：install_id 相位错峰 + ±10% 抖动 + 指数退避，
    行动只传聚合计数，绝不上报渠道地址/密钥/请求内容。
  * 一键预填（T2.8）：只接受验签过的 registry 里的厂家数据，永不预填
    api_key，界面明示目标域名；伪造厂家一律拒绝。

数据文件（与 router.json 同目录）：
  center.json        用户可编辑：中心地址、公钥、隐私开关、隐私同意标记
  center.state.json  install_id、缓存、已读游标、消息、行动计数（可整删重建）

纯标准库实现（约束 A3）：网络用 urllib，后台一条守护线程。
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import random
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from . import ed25519

__all__ = [
    "CenterClient", "default_settings", "normalize_settings",
    "channel_fingerprint", "diff_registry", "vendor_template",
    "phase_fraction", "semver_newer", "selftest",
]

DEFAULT_URL = "https://center.skillgo.cn"
# T0.1 登记的项目公钥（Ed25519 SPKI DER base64）；自建中心换成自己的即可。
DEFAULT_PUBLIC_KEY = "MCowBQYDK2VwAyEA9t6kEpGgIvqNuFYqesO1gpN9miM51hxf6lzOn9LRjik="

SYNC_INTERVAL = 6 * 3600.0   # 设计 5.2：每 6 小时同步一次
SYNC_JITTER = 0.10           # ±10% 随机偏移，避免整点雷群
STARTUP_DELAY = (5.0, 45.0)  # 启动后先等一小段错峰窗口
REPORT_PERIOD = 24 * 3600.0  # 设计 5.6：默认心跳周期 24h（registry 可下发覆盖）
REPORT_JITTER = 0.10
BACKOFF_BASE = 60.0          # 心跳失败指数退避基数
BACKOFF_MAX = 4 * 3600.0
FETCH_TIMEOUT = 10.0
MESSAGES_LIMIT = 200         # 已读归档容量
WEBHOOK_ATTEMPTS = 3         # 设计 3.7：失败退避重试 3 次

SETTING_FIELDS = {
    "url": str, "public_key": str,
    "enabled": bool, "sync_registry": bool, "push_intel": bool,
    "check_updates": bool, "report_anonymous": bool,
    "report_actions": bool, "report_diagnostics": bool, "notice_ack": bool,
}
ACTION_KEYS = ("one_click_add_channel", "open_activity")
OFFLINE_STATUSES = {"deprecated", "offline", "sunset", "removed"}
# 指纹覆盖的「预置字段」：用户改动这些字段就视为改过预置模板。
PRESET_FIELDS = ("protocol", "provider", "mode", "base_url", "tier",
                 "models", "model_quota", "free_quota")


def default_settings() -> dict:
    return {
        "url": DEFAULT_URL,
        "public_key": DEFAULT_PUBLIC_KEY,
        "enabled": True,
        "sync_registry": True,
        "push_intel": True,
        "check_updates": True,
        "report_anonymous": True,
        "report_actions": True,     # 仅 report_anonymous 开启时生效
        "report_diagnostics": False,  # 预留：诊断上报需显式开启
        "notice_ack": False,        # 首次启动隐私声明是否已读
    }


def normalize_settings(raw: dict | None) -> tuple[dict, list[str]]:
    """旧字段/坏值一律回退默认值；返回 (settings, errors)。"""
    base = default_settings()
    errors: list[str] = []
    if not isinstance(raw, dict):
        return base, ["center 配置不是对象，已回退默认值"]
    for key, kind in SETTING_FIELDS.items():
        if key not in raw:
            continue
        value = raw[key]
        if kind is bool:
            if not isinstance(value, bool):
                errors.append(f"center.{key} 应为布尔，已忽略")
                continue
        elif not isinstance(value, str):
            errors.append(f"center.{key} 应为字符串，已忽略")
            continue
        base[key] = value
    url = base["url"].strip().rstrip("/")
    if not (url.startswith("http://") or url.startswith("https://")):
        errors.append("center.url 必须是 http(s) 地址，已回退默认值")
        url = DEFAULT_URL
    base["url"] = url
    try:
        ed25519.load_public_key(base["public_key"])
    except ValueError:
        errors.append("center.public_key 无法解析，已回退默认值")
        base["public_key"] = DEFAULT_PUBLIC_KEY
    return base, errors


def channel_fingerprint(channel: dict) -> str:
    """预置指纹：安装时渠道模板原始配置的哈希（设计 5.2 合并规则）。

    指纹随渠道一起存在 router.json 里；用户改动预置字段（base_url /
    models / 额度等）后指纹不再一致，即视为「已被修改」。
    """
    payload = {k: channel[k] for k in PRESET_FIELDS if k in channel}
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def semver_newer(current: str, candidate: str) -> bool:
    """candidate 是否比 current 新（数字段比较，解析失败视为不更新）。"""
    def parts(text: str):
        try:
            return tuple(int(x) for x in str(text).strip().split(".")[:3])
        except ValueError:
            return None
    a, b = parts(current), parts(candidate)
    if a is None or b is None:
        return False
    return b > a


def phase_fraction(install_id: str) -> float:
    """稳定相位：sha256(install_id) 前 64 位 / 2^64 ∈ [0,1)。

    同一 install_id 每次算出同一个相位（设计 5.6：防上报风暴）。
    """
    digest = hashlib.sha256(str(install_id).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def vendor_template(vendor: dict) -> dict:
    """registry 厂家 → 渠道模板（api_key 永空，由用户填）。"""
    endpoints = vendor.get("endpoints") or {}
    base_url = str(endpoints.get("openai") or next(iter(endpoints.values()), "") or "")
    models: list[str] = []
    for entry in vendor.get("models") or []:
        if isinstance(entry, dict) and entry.get("id"):
            models.append(str(entry["id"]))
    models_map = {}
    if models:
        models_map["auto"] = models[0]
    quota: dict = {}
    per_model = ((vendor.get("free_quota") or {}).get("per_model") or {})
    for mid, info in per_model.items():
        if isinstance(info, dict):
            quota[str(mid)] = {
                "tokens": int(info.get("tokens") or 0),
                **({"expires": info["expires"]} if info.get("expires") else {}),
            }
    draft = {
        "name": str(vendor.get("id") or vendor.get("name") or ""),
        "protocol": "openai",
        "provider": str(vendor.get("id") or ""),
        "mode": "payg",
        "base_url": base_url,
        "api_key": "",
        "enabled": False,
        "tier": "free",
        "models": models_map,
        **({"model_quota": quota} if quota else {}),
    }
    template = dict(draft)
    template["preset_fingerprint"] = channel_fingerprint(draft)
    return template


def _quota_changes(channel: dict, vendor: dict) -> dict | None:
    """registry 的额度/模型变化与当前渠道的差（只对预置未改渠道自动应用）。"""
    per_model = ((vendor.get("free_quota") or {}).get("per_model") or {})
    reg_ids = {str(m.get("id")) for m in vendor.get("models") or []
               if isinstance(m, dict) and m.get("id")}
    free_ids = [str(m["id"]) for m in vendor.get("models") or []
                if isinstance(m, dict) and m.get("id")
                and (m.get("tier") or "") == "free"]
    cur_quota = {str(k): dict(v) for k, v in (channel.get("model_quota") or {}).items()}
    new_quota = dict(cur_quota)
    added: list[str] = []
    updated: list[str] = []
    for mid, info in per_model.items():
        want = {"tokens": int(info.get("tokens") or 0)}
        if info.get("expires"):
            want["expires"] = info["expires"]
        have = new_quota.get(str(mid))
        if have is None:
            added.append(str(mid))
            new_quota[str(mid)] = want
        elif (have.get("tokens"), have.get("expires")) != (want["tokens"], want.get("expires")):
            updated.append(str(mid))
            new_quota[str(mid)] = want
    removed: list[str] = []
    for mid in list(new_quota):
        if mid not in per_model and mid not in reg_ids and mid in cur_quota:
            removed.append(mid)
            new_quota.pop(mid)
    cur_models = {str(k): str(v) for k, v in (channel.get("models") or {}).items()}
    models_added: list[str] = []
    for mid in free_ids:
        if mid not in cur_models and mid not in cur_models.values():
            models_added.append(mid)
            cur_models[mid] = mid
    if not (added or updated or removed or models_added):
        return None
    changes: dict = {}
    if added or updated or removed:
        changes["model_quota"] = new_quota
    if models_added:
        changes["models"] = cur_models
    changes["added"] = added
    changes["updated"] = updated
    changes["removed"] = removed
    changes["models_added"] = models_added
    return changes


def diff_registry(channels: list[dict], registry: dict,
                  dismissed: set | None = None) -> list[dict]:
    """registry（验签后的 registry.json 载荷）与本地渠道的差异清单。

    每项：{id, kind, vendor, channel, title, summary, payload}。
    kind：vendor_new（推荐添加）/ channel_update（自动更新）/
    preset_modified（只提示）/ offline（下线提醒）。
    用户自建渠道（provider 为空）不在比对范围。
    """
    dismissed = dismissed or set()
    items: list[dict] = []
    vendors: dict[str, dict] = {}
    for vendor in registry.get("vendors") or []:
        if isinstance(vendor, dict) and vendor.get("id"):
            vendors[str(vendor["id"])] = vendor
    by_provider: dict[str, list[dict]] = {}
    for channel in channels:
        provider = str(channel.get("provider") or "")
        if provider:
            by_provider.setdefault(provider, []).append(channel)
    seq = 0
    for vendor_id, vendor in vendors.items():
        seq += 1
        status = str(vendor.get("status") or "active").lower()
        local = by_provider.get(vendor_id, [])
        if not local:
            if status not in OFFLINE_STATUSES and vendor_id not in dismissed:
                items.append({
                    "id": f"vendor_new:{vendor_id}",
                    "kind": "vendor_new", "vendor": vendor_id,
                    "channel": None,
                    "title": f"新厂家：{vendor.get('name') or vendor_id}",
                    "summary": f"注册表里有新厂家 {vendor_id}（{vendor.get('name') or ''}），"
                               f"可一键预填渠道",
                    "payload": vendor_template(vendor),
                })
            continue
        if status in OFFLINE_STATUSES:
            items.append({
                "id": f"offline:{vendor_id}",
                "kind": "offline", "vendor": vendor_id, "channel": None,
                "title": f"{vendor.get('name') or vendor_id} 已下线",
                "summary": "厂家已下线：相关渠道保留但不再自动更新，确认后建议停用",
                "payload": {"status": status},
            })
            continue
        for channel in local:
            name = str(channel.get("name") or vendor_id)
            stored = channel.get("preset_fingerprint")
            if not stored:
                # 旧渠道没有指纹：按「视为已修改」处理（dev-plan T2.4 已决）
                items.append({
                    "id": f"preset_modified:{vendor_id}:{name}",
                    "kind": "preset_modified", "vendor": vendor_id,
                    "channel": name,
                    "title": f"{name} 有新版本可用",
                    "summary": "该渠道安装时未记录预置指纹，按已修改处理："
                               "只提示，不自动覆盖",
                    "payload": {"template": vendor_template(vendor)},
                })
                continue
            if channel_fingerprint(channel) != str(stored):
                items.append({
                    "id": f"preset_modified:{vendor_id}:{name}",
                    "kind": "preset_modified", "vendor": vendor_id,
                    "channel": name,
                    "title": f"{name} 有新版本可用",
                    "summary": "该渠道已被手动修改过：只提示，不覆盖",
                    "payload": {"template": vendor_template(vendor)},
                })
                continue
            changes = _quota_changes(channel, vendor)
            if changes:
                items.append({
                    "id": f"channel_update:{vendor_id}:{name}",
                    "kind": "channel_update", "vendor": vendor_id,
                    "channel": name,
                    "title": f"{name} 额度/模型已更新",
                    "summary": (f"厂家注册表更新：额度 {len(changes['added'])} 增、"
                               f"{len(changes['updated'])} 改、{len(changes['removed'])} 删"),
                    "payload": {"changes": changes,
                                "template": vendor_template(vendor)},
                })
    return items


class CenterClient:
    """中心服务客户端：同步、推送、心跳、预填与消息中心。"""

    def __init__(self, settings_path, state_path, config_path=None,
                 get_raw=None, apply_config=None, get_webhook=None,
                 version: str = "", log=None, sender=None) -> None:
        self.settings_path = Path(settings_path)
        self.state_path = Path(state_path)
        self.config_path = Path(config_path) if config_path else None
        self.get_raw = get_raw or (lambda: {})
        self.apply_config = apply_config
        self.get_webhook = get_webhook
        self.version = str(version or "")
        self.log = log or (lambda text: None)
        self.sender = sender  # (request) -> response；测试注入用
        self._lock = threading.RLock()
        self._sync_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._state: dict = {}
        self._next_sync = 0.0
        self._load_state()

    # ---------- 配置与状态 ----------

    def settings(self) -> dict:
        try:
            raw = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        base, errors = normalize_settings(raw.get("center") if isinstance(raw, dict) else raw)
        for error in errors:
            self.log(error)
        return base

    def save_settings(self, payload: dict) -> tuple[dict, list[str]]:
        """POST /api/center 的载荷：{"settings": {...}} 或直接是设置项。"""
        raw = {}
        if isinstance(payload, dict):
            inner = payload.get("settings")
            raw = inner if isinstance(inner, dict) else payload
            if "center" in payload and isinstance(inner, dict):
                raw = inner
        merged = self.settings()
        merged.update({k: v for k, v in raw.items() if k in SETTING_FIELDS})
        settings, errors = normalize_settings(merged)
        self._write_json(self.settings_path, {"center": settings})
        return settings, errors

    def _load_state(self) -> None:
        try:
            self._state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._state = {}
        if not isinstance(self._state, dict):
            self._state = {}
        self._state.setdefault("install_id", "iid-" + _uuid_hex())
        self._state.setdefault("pending", [])
        self._state.setdefault("archive", [])
        self._state.setdefault("actions", {"day": "", "counts": {}})
        self._state.setdefault("heartbeat", {"next_report": 0.0, "failures": 0})
        self._state.setdefault("seen_item_ids", [])
        # 诊断计数（T4.3）：厂家 -> 错误类型 -> 次数，随心跳聚合上报
        self._state.setdefault("diag", {"day": "", "vendors": {}})

    def _save_state(self) -> None:
        self._write_json(self.state_path, self._state)

    def install_id(self) -> str:
        return str(self._state.get("install_id") or "")

    @property
    def _cache(self) -> dict:
        return self._state.setdefault("cache", {})

    # ---------- 同步 ----------

    def sync_now(self) -> dict:
        """手动或周期触发的同步；返回摘要（永不抛异常）。"""
        with self._sync_lock:
            return self._sync_locked()

    def _sync_locked(self) -> dict:
        settings = self.settings()
        summary = {"ok": True, "registry": None, "intel": None,
                   "updates": None, "errors": []}
        if not settings["enabled"]:
            summary["ok"] = False
            summary["errors"].append("中心服务未启用")
            self._note_sync(False, "中心服务未启用", summary)
            return summary
        base = settings["url"]
        registry_payload = None
        if settings["sync_registry"]:
            registry_payload = self._fetch_signed(
                base + "/v1/registry.json", settings, summary)
            if registry_payload is not None:
                self._merge_registry(registry_payload, summary)
        if settings["push_intel"]:
            intel_payload = self._fetch_signed(
                base + "/v1/intel.json", settings, summary)
            if intel_payload is not None:
                self._push_intel(intel_payload, summary)
        if settings["check_updates"]:
            self._check_updates(base, settings, summary)
        self._note_sync(True, "", summary)
        return summary

    def _fetch_signed(self, url: str, settings: dict, summary: dict):
        """GET 工件 + 验签；失败返回 None（保留本地缓存）。"""
        name = url.rsplit("/", 1)[-1]
        cache = self._cache.setdefault(name, {})
        try:
            status, body, etag = self._http_get(url, etag=cache.get("etag"))
        except Exception as exc:
            summary["errors"].append(f"{name} 拉取失败（{exc}），用本地缓存")
            return None
        if status == 304:
            cache["fetched_at"] = _now_iso()
            return cache.get("payload")
        if status != 200 or not body:
            summary["errors"].append(f"{name} 拉取失败（HTTP {status}），用本地缓存")
            return None
        try:
            sig = self._http_get_text(url + ".sig")
        except Exception as exc:
            summary["errors"].append(f"{name} 签名拉取失败（{exc}），丢弃本次内容")
            return None
        if not ed25519.verify_b64(settings["public_key"], sig, body):
            summary["errors"].append("同步校验失败：签名不匹配，丢弃本次内容（用本地缓存）")
            return None
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            summary["errors"].append(f"{name} 内容不是合法 JSON（{exc}），丢弃")
            return None
        if not isinstance(payload, dict):
            summary["errors"].append(f"{name} 内容结构异常，丢弃")
            return None
        cache["etag"] = etag or ""
        cache["payload"] = payload
        cache["fetched_at"] = _now_iso()
        return payload

    def _merge_registry(self, registry: dict, summary: dict) -> None:
        """设计 5.2 合并规则：四类处理；用户改过的渠道任何情况下不被覆盖。"""
        raw = self.get_raw() or {}
        channels = raw.get("channels") or []
        dismissed = {item["id"].split(":", 1)[1]
                     for item in self._state["pending"] + self._state["archive"]
                     if item.get("id", "").startswith("vendor_new:")}
        items = diff_registry(channels, registry, dismissed=dismissed)
        added = []
        for item in items:
            item_id = item["id"]
            if item_id in self._state["seen_item_ids"]:
                continue
            self._state["seen_item_ids"].append(item_id)
            if len(self._state["seen_item_ids"]) > 500:
                self._state["seen_item_ids"] = self._state["seen_item_ids"][-250:]
            if item["kind"] == "channel_update":
                applied = self._apply_channel_update(item)
                if applied:
                    item = dict(item)
                    item["kind"] = "auto_update"
                    item["summary"] = item["summary"] + "（已自动应用）"
            added.append(item)
        if added:
            self._state["pending"].extend(added)
            self._state["pending"] = self._state["pending"][-MESSAGES_LIMIT:]
        self._save_state()

    def _apply_channel_update(self, item: dict) -> bool:
        """预置未改渠道：自动更新额度/模型并记入「今日更新」。"""
        if not self.apply_config:
            return False
        payload = item.get("payload") or {}
        changes = payload.get("changes") or {}
        applied: dict = {}
        raw = self.get_raw() or {}
        for channel in raw.get("channels") or []:
            if channel.get("name") == item.get("channel"):
                updated = dict(channel)
                if "model_quota" in changes:
                    updated["model_quota"] = changes["model_quota"]
                if "models" in changes:
                    updated["models"] = changes["models"]
                updated["preset_fingerprint"] = item["payload"]["template"].get(
                    "preset_fingerprint", channel.get("preset_fingerprint"))
                applied[channel.get("name")] = updated
        if not applied:
            return False
        new_raw = json.loads(json.dumps(raw))
        new_raw["channels"] = [
            applied.get(c.get("name"), c) for c in new_raw.get("channels") or []]
        try:
            self.apply_config(new_raw)
            return True
        except Exception as exc:
            self.log(f"自动更新渠道失败：{exc!r}")
            return False

    def _push_intel(self, intel: dict, summary: dict) -> None:
        """新情报 → 消息区 + webhook 转发（已读游标去重，设计 5.3/5.5）。

        撤回（T4.4）：status=retracted 的活动向已收到的客户端下发更正——
        本地消息标「已撤回」并移除操作入口；从未见过的直接下发更正说明。
        """
        cursor = self._state.setdefault("intel_cursor", {"seen": []})
        seen = set(cursor.get("seen") or [])
        new_items = []
        retracted = []
        for activity in intel.get("activities") or []:
            if not isinstance(activity, dict):
                continue
            item_id = str(activity.get("id") or "")
            if not item_id:
                continue
            if activity.get("status") == "retracted":
                retracted.append(activity)
                seen.add(item_id)  # 撤回条目不再作为新情报重复推送
                continue
            if item_id in seen:
                continue
            seen.add(item_id)
            new_items.append(activity)
        if retracted:
            self._withdraw_intel(retracted)
        if seen == set(cursor.get("seen") or []):
            return
        cursor["seen"] = list(seen)[-500:]
        webhook_status = None
        webhook_url = ""
        if self.get_webhook:
            try:
                webhook_url = str((self.get_webhook() or {}).get("webhook_url") or "")
            except Exception:
                webhook_url = ""
        for activity in new_items:
            item = self._intel_item(activity)
            self._state["pending"].append(item)
            if webhook_url and self.get_webhook:
                self._state["pending"][-1]["webhook"] = self._forward_webhook(
                    webhook_url, item)
        self._state["pending"] = self._state["pending"][-MESSAGES_LIMIT:]
        self._save_state()

    def _withdraw_intel(self, activities: list) -> None:
        """撤回下发（T4.4）：已收到的条目标「已撤回」+ 更正；从未见过的直接更正。"""
        webhook_url = ""
        if self.get_webhook:
            try:
                webhook_url = str((self.get_webhook() or {}).get("webhook_url") or "")
            except Exception:
                webhook_url = ""
        delivered = self._state["pending"] + self._state["archive"]
        corrected = False
        for activity in activities:
            item_id = f"intel:{activity.get('id')}"
            reason = str(activity.get("retracted_reason") or "内容有误")
            for item in delivered:
                if item.get("id") != item_id:
                    continue
                item["withdrawn"] = True
                item["withdrawn_reason"] = reason
                item["actionable"] = False
                item["summary"] = "【已撤回】" + str(item.get("summary") or "")
                corrected = True
                if item.get("webhook") and webhook_url:
                    try:
                        self._forward_webhook(webhook_url, {
                            "event": "intel_retracted", "id": item_id,
                            "title": item.get("title", ""), "reason": reason})
                    except Exception:
                        pass
            if not any(i.get("id") == item_id for i in delivered):
                # 从未见过的撤回：直接下发更正说明，让用户知道这条为什么没了
                self._state["pending"].append({
                    "id": item_id, "kind": "intel",
                    "vendor": str(activity.get("vendor") or ""),
                    "channel": None,
                    "title": str(activity.get("title") or "情报已撤回"),
                    "summary": f"情报已撤回：{activity.get('title', '')}（{reason}）",
                    "payload": activity, "created_at": _now_iso(),
                    "unread": True, "actionable": False,
                })
                corrected = True
        if corrected:
            self._state["pending"] = self._state["pending"][-MESSAGES_LIMIT:]
            self._save_state()

    def _intel_item(self, activity: dict) -> dict:
        vendor = str(activity.get("vendor") or "")
        title = str(activity.get("title") or "新活动")
        action = str(activity.get("action") or "")
        entry = {
            "id": f"intel:{activity.get('id')}",
            "kind": "intel", "vendor": vendor, "channel": None,
            "title": title,
            "summary": f"{vendor}：{action or title}",
            "payload": activity,
            "created_at": _now_iso(),
            "unread": True,
            "actionable": True,
        }
        registry = (self._cache.get("registry.json") or {}).get("payload") or {}
        vendor_known = any(v.get("id") == vendor for v in registry.get("vendors") or [])
        if "预填" in action or "渠道" in action:
            entry["action"] = {"label": "一键预填该渠道", "vendor": vendor,
                               "known": vendor_known}
        elif activity.get("sources"):
            entry["action"] = {"label": "查看详情",
                               "url": activity["sources"][0]}
        else:
            entry["actionable"] = False
        return entry

    def _forward_webhook(self, url: str, item: dict) -> bool:
        """转发情报到已配置的 webhook；失败退避重试 3 次（设计 3.7）。"""
        from . import notifier as notifier_mod
        text = f"[Facet] 新活动：{item['title']} — {item.get('summary', '')}"
        payload = notifier_mod.webhook_payload(
            url, text, {"vendor": item.get("vendor") or ""})
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        for attempt in range(WEBHOOK_ATTEMPTS):
            try:
                request = urllib.request.Request(
                    url, data=body,
                    headers={"Content-Type": "application/json"}, method="POST")
                response = (self.sender or urllib.request.urlopen)(
                    request, timeout=FETCH_TIMEOUT)
                raw = b""
                if hasattr(response, "read"):
                    raw = response.read() or b""
                if hasattr(response, "close"):
                    response.close()
                kind = notifier_mod.webhook_kind(url)
                if kind in ("feishu", "wecom") and raw:
                    answer = json.loads(raw.decode("utf-8", "replace"))
                    if "errcode" in answer and answer["errcode"] not in (0, "0"):
                        raise RuntimeError(f"webhook 返回错误：{answer.get('errmsg') or answer}")
                return True
            except Exception as exc:
                if attempt + 1 < WEBHOOK_ATTEMPTS:
                    time.sleep(1.0 * (2 ** attempt))
                    continue
                self.log(f"webhook 转发失败（已重试 {WEBHOOK_ATTEMPTS} 次）：{exc!r}")
                return False

    def _check_updates(self, base: str, settings: dict, summary: dict) -> None:
        """版本比对：仅新版本才提示（验收 T2.6）。"""
        try:
            status, body, _etag = self._http_get(base + "/v1/releases/latest")
        except Exception as exc:
            summary["errors"].append(f"版本检查失败（{exc}）")
            return
        if status != 200 or not body:
            return
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(payload, dict):
            return
        latest = str(payload.get("version") or "")
        if not latest or not semver_newer(self.version, latest):
            return
        installer = payload.get("installer") if isinstance(payload.get("installer"), dict) else None
        item = {
            "id": f"update:{latest}",
            "kind": "update", "vendor": "", "channel": None,
            "title": f"Facet {latest} 已发布（当前 {self.version}）",
            "summary": str(payload.get("notes") or "新版本可用"),
            "payload": payload,
            "created_at": _now_iso(),
            "unread": True,
            "actionable": True,
            "action": {"label": "查看发布页",
                       "url": str(payload.get("url")
                                  or (installer or {}).get("url") or "")},
        }
        if item["id"] in {i["id"] for i in self._state["pending"]}:
            return
        self._state["pending"].insert(0, item)
        self._save_state()
        summary["updates"] = latest

    def _note_sync(self, ok: bool, error: str, summary: dict) -> None:
        self._state["last_sync_at"] = _now_iso()
        self._state["last_sync_ok"] = bool(ok)
        self._state["last_error"] = error or None
        self._save_state()
        # 同步成功时清掉"中心不可达"告警（约定：log("") = 恢复）；
        # 失败的具体原因已随 summary.errors 进消息中心，不必再告警一遍。
        if ok and self.log:
            self.log("")

    # ---------- 心跳与行动计数 ----------

    def record_action(self, name: str, count: int = 1) -> None:
        """行动计数（只增计数，随心跳上传聚合值，设计 5.6）。"""
        if name not in ACTION_KEYS:
            return
        actions = self._state["actions"]
        today = time.strftime("%Y-%m-%d")
        if actions.get("day") != today:
            actions["day"] = today
            actions["counts"] = {}
        actions["counts"][name] = int(actions["counts"].get(name, 0)) + int(count)
        self._save_state()

    def note_upstream_error(self, channel: str, status: str = "",
                           http_status=None) -> None:
        """诊断计数（T4.3）：失败请求按厂家聚合错误类型。

        只记厂家 id + 错误类型 + 次数，不含 key 与地址（设计 5.5）。
        report_diagnostics 未开启时只本地累计；心跳成功后清空。
        """
        if not isinstance(channel, str) or not channel:
            return
        vendor = self._channel_vendor(channel)
        etype = self._diag_etype(status, http_status)
        diag = self._state["diag"]
        today = time.strftime("%Y-%m-%d")
        if diag.get("day") != today:
            diag["day"] = today
            diag["vendors"] = {}
        vendors = diag.setdefault("vendors", {})
        buckets = vendors.setdefault(vendor, {})
        buckets[etype] = int(buckets.get(etype, 0)) + 1
        self._save_state()

    def _channel_vendor(self, channel: str) -> str:
        """渠道名 → 厂家 id（内置厂家）；自建渠道记 self-built。"""
        try:
            for c in (self.get_raw() or {}).get("channels") or []:
                if isinstance(c, dict) and str(c.get("name")) == channel:
                    return str(c.get("provider") or "self-built")
        except Exception:
            pass
        return "self-built" if channel else "unknown"

    @staticmethod
    def _diag_etype(status: str, http_status) -> str:
        """错误类型口径：有 HTTP 状态码按码（429/401/403/5xx），否则按审计状态归一。"""
        if isinstance(http_status, bool):
            http_status = None
        if isinstance(http_status, int) and http_status > 0:
            if http_status == 429:
                return "429"
            if http_status in (401, 403):
                return str(http_status)
            if 500 <= http_status < 600:
                return "5xx"
            if 400 <= http_status < 500:
                return "4xx"
            return "http-%d" % http_status
        return {"network_error": "network", "timeout": "timeout",
                "upstream_error": "upstream", "cooldown": "cooldown",
                "quota_exhausted": "quota"}.get(status, status or "other")

    def run_heartbeat(self) -> dict:
        """心跳：相位错峰 + 指数退避；关闭开关立即停止（验收 T2.7）。"""
        settings = self.settings()
        summary = {"ok": False, "reason": ""}
        if not (settings["enabled"] and settings["report_anonymous"]):
            summary["reason"] = "未启用匿名上报"
            return summary
        base = settings["url"]
        period = REPORT_PERIOD
        registry_payload = (self._cache.get("registry.json") or {}).get("payload")
        if isinstance(registry_payload, dict):
            hours = registry_payload.get("report_period_hours")
            if isinstance(hours, (int, float)) and 1 <= float(hours) <= 168:
                period = float(hours) * 3600.0
        channels = self._enabled_channel_count()
        ping = {"install_id": self.install_id(), "version": self.version,
                "os": platform.system().lower() or "unknown", "channels": channels}
        diag = {}
        if settings.get("report_diagnostics"):
            diag = dict((self._state.get("diag") or {}).get("vendors") or {})
        if diag:
            ping["diag"] = diag
        try:
            status = self._http_post(base + "/v1/ping", ping)
            if status not in (200, 204):
                raise RuntimeError(f"HTTP {status}")
            summary["ok"] = True
        except Exception as exc:
            summary["reason"] = f"心跳失败：{exc!r}"
            self._state["heartbeat"]["failures"] = int(
                self._state["heartbeat"].get("failures", 0)) + 1
            self._save_state()
            return summary
        if settings["report_anonymous"] and settings.get("report_actions", True):
            actions = self._state["actions"]
            counts = actions.get("counts") or {}
            if counts:
                try:
                    self._http_post(base + "/v1/actions", {
                        "install_id": self.install_id(),
                        "day": actions.get("day") or time.strftime("%Y-%m-%d"),
                        "counts": counts,
                    })
                except Exception as exc:
                    self.log(f"行动计数上传失败：{exc!r}")
                actions["counts"] = {}
                self._save_state()
        if diag and settings.get("report_diagnostics"):
            # 诊断计数已随心跳上传，清空避免重复上报
            self._state["diag"] = {"day": time.strftime("%Y-%m-%d"), "vendors": {}}
            self._save_state()
        self._state["heartbeat"]["failures"] = 0
        self._state["heartbeat"]["last_report"] = time.time()
        self._schedule_next_report(time.time(), period, failures=0)
        self._save_state()
        return summary

    def _enabled_channel_count(self) -> int:
        try:
            raw = self.get_raw() or {}
            return sum(1 for c in raw.get("channels") or []
                       if c.get("enabled"))
        except Exception:
            return 0

    def _schedule_next_report(self, now: float, period: float,
                             failures: int = 0) -> float:
        """相位错峰：首次安装先等 phase×周期；失败指数退避+抖动。"""
        phase = phase_fraction(self.install_id())
        if failures:
            delay = min(BACKOFF_MAX, BACKOFF_BASE * (2 ** (failures - 1)))
            delay += delay * REPORT_JITTER * random.uniform(-1, 1)
        else:
            delay = phase * period + period * REPORT_JITTER * random.uniform(-1, 1)
        self._state["heartbeat"]["next_report"] = now + max(delay, 30.0)
        return self._state["heartbeat"]["next_report"]

    # ---------- 预填 ----------

    def prefill(self, vendor_id: str) -> dict:
        """一键预填：只接受验签过的 registry 里的厂家数据（T2.8）。

        返回 {"channel": 模板, "domain": 目标域名}；厂家不在签名注册表里
        一律拒绝（伪造链接被拒）。
        """
        registry = (self._cache.get("registry.json") or {}).get("payload") or {}
        vendor = next((v for v in registry.get("vendors") or []
                       if isinstance(v, dict) and str(v.get("id")) == str(vendor_id)), None)
        if vendor is None:
            raise ValueError(f"厂家 {vendor_id} 不在验签过的注册表里，拒绝预填")
        template = vendor_template(vendor)
        domain = ""
        try:
            from urllib.parse import urlsplit
            domain = (urlsplit(template["base_url"]).hostname or "").lower()
        except Exception:
            domain = ""
        return {"channel": template, "domain": domain}

    def confirm_prefill(self, vendor_id: str) -> dict:
        """用户确认后的预填落盘：追加渠道（api_key 永空）。"""
        if not self.apply_config:
            raise RuntimeError("配置写入未启用")
        raw = self.get_raw() or {}
        template = self.prefill(vendor_id)["channel"]
        if any(c.get("provider") == template["provider"] for c in raw.get("channels") or []):
            raise RuntimeError("该厂家渠道已存在")
        new_raw = json.loads(json.dumps(raw))
        new_raw.setdefault("channels", []).append(template)
        self.apply_config(new_raw)
        self.record_action("one_click_add_channel")
        return {"channel": template}

    def download_installer(self) -> dict:
        """Windows 安装包：验签通过才返回直链（设计 5.4）。"""
        item = next((i for i in self._state["pending"] if i["kind"] == "update"), None)
        if not item:
            raise RuntimeError("没有待下载的新版本")
        payload = item.get("payload") or {}
        installer = payload.get("installer") or {}
        url = str(installer.get("url") or "")
        sig_url = str(installer.get("sig_url") or "")
        if not (url and sig_url):
            return {"url": str(payload.get("url") or ""), "verified": False}
        if not ed25519.verify_b64(self.settings()["public_key"],
                                  self._http_get_text(sig_url),
                                  self._http_get_bytes(url)):
            raise RuntimeError("安装包签名校验失败，已拒绝下载")
        return {"url": url, "verified": True}

    # ---------- 消息中心 ----------

    def ui_payload(self) -> dict:
        state = dict(self._state)
        pending = list(state.get("pending") or [])
        update = next((i for i in pending if i["kind"] == "update"), None)
        return {
            "version": self.version,
            "settings": self.settings(),
            "status": {
                "last_sync_at": state.get("last_sync_at"),
                "ok": state.get("last_sync_ok"),
                "error": state.get("last_error"),
                "url": self.settings()["url"],
                "install_id": self.install_id(),
                "next_report": (state.get("heartbeat") or {}).get("next_report"),
            },
            "notice_ack": bool(self.settings().get("notice_ack")),
            "items": pending,
            "archive_count": len(state.get("archive") or []),
            "update": update,
        }

    def mark_all_read(self) -> int:
        """未读 → 已读归档（设计 3.7 / 验收 T2.9）。"""
        with self._lock:
            pending = self._state.get("pending") or []
            archive = self._state.get("archive") or []
            moved = [i for i in pending if i.get("unread")]
            for item in moved:
                item["unread"] = False
            archive = moved + archive
            self._state["archive"] = archive[-MESSAGES_LIMIT:]
            self._state["pending"] = [i for i in pending if not i.get("unread")]
            self._save_state()
            return len(moved)

    def dismiss_item(self, item_id: str) -> bool:
        with self._lock:
            pending = self._state.get("pending") or []
            for item in pending:
                if item.get("id") == item_id:
                    self._state["pending"].remove(item)
                    if item["kind"] == "vendor_new":
                        self._state.setdefault("dismissed_vendors", []).append(
                            item_id.split(":", 1)[1])
                    self._save_state()
                    return True
            return False

    def apply_item(self, item_id: str) -> dict:
        """用户确认后的合并动作（vendor_new / preset_modified）。"""
        with self._lock:
            item = next((i for i in self._state.get("pending") or []
                         if i.get("id") == item_id), None)
            if item is None:
                raise RuntimeError("消息不存在或已处理")
            kind = item.get("kind")
            if kind == "vendor_new":
                vendor_id = item["payload"]["provider"]
                result = self.confirm_prefill(vendor_id)
                item["summary"] = f"已添加渠道 {result['channel']['name']}"
                item["actionable"] = False
            elif kind == "preset_modified":
                if not self.apply_config:
                    raise RuntimeError("配置写入未启用")
                template = item["payload"].get("template") or {}
                raw = self.get_raw() or {}
                new_raw = json.loads(json.dumps(raw))
                for channel in new_raw.get("channels") or []:
                    if channel.get("name") == item.get("channel"):
                        for field in ("protocol", "provider", "mode", "base_url",
                                      "tier", "models", "model_quota", "free_quota"):
                            if field in template:
                                channel[field] = template[field]
                        channel["preset_fingerprint"] = template.get(
                            "preset_fingerprint")
                self.apply_config(new_raw)
                item["summary"] = "已采用新模板（差异已在确认时展示）"
                item["actionable"] = False
            elif kind == "offline":
                item["summary"] = "已确认下线提醒"
                item["actionable"] = False
            else:
                raise RuntimeError(f"消息 {kind} 无需确认动作")
            item["unread"] = False
            self._state["pending"].remove(item)
            self._state["archive"] = [item] + (self._state.get("archive") or [])
            self._save_state()
            return item

    def acknowledge_notice(self) -> dict:
        settings, errors = self.save_settings({"notice_ack": True})
        return {"settings": settings, "errors": errors}

    # ---------- 后台线程 ----------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, name="center-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5.0)

    def _loop(self) -> None:
        """一条守护线程：同步（每 6h±10%）与心跳（相位错峰）共用。"""
        delay = random.uniform(*STARTUP_DELAY)
        while not self._stop_event.wait(delay):
            delay = 30.0
            try:
                settings = self.settings()
                if not settings["enabled"]:
                    continue
                now = time.time()
                if settings["sync_registry"] or settings["push_intel"] \
                        or settings["check_updates"]:
                    if now >= self._next_sync:
                        self.sync_now()
                        self._next_sync = (time.time()
                                          + SYNC_INTERVAL
                                          * random.uniform(1 - SYNC_JITTER,
                                                          1 + SYNC_JITTER))
                if settings["report_anonymous"]:
                    heartbeat = self._state.get("heartbeat") or {}
                    next_report = heartbeat.get("next_report") or 0.0
                    if not next_report:
                        registry_payload = (self._cache.get("registry.json")
                                            or {}).get("payload")
                        period = REPORT_PERIOD
                        if isinstance(registry_payload, dict):
                            hours = registry_payload.get("report_period_hours")
                            if isinstance(hours, (int, float)) and 1 <= float(hours) <= 168:
                                period = float(hours) * 3600.0
                        self._schedule_next_report(now, period)
                        self._save_state()
                    elif now >= next_report:
                        self.run_heartbeat()
            except Exception as exc:  # 任何异常都不许影响网关
                self.log(f"center 后台任务异常：{exc!r}")

    # ---------- HTTP ----------

    def _http_get(self, url: str, etag: str | None = None):
        if self.sender is not None:
            request = urllib.request.Request(url)
            if etag:
                request.add_header("If-None-Match", etag)
            response = self.sender(request, timeout=FETCH_TIMEOUT)
            return (response.status if hasattr(response, "status") else 200,
                    response.read() if hasattr(response, "read") else b"",
                    (response.headers or {}).get("ETag") if hasattr(response, "headers") else None)
        request = urllib.request.Request(url)
        if etag:
            request.add_header("If-None-Match", etag)
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
            return response.status, response.read(), response.headers.get("ETag")

    def _http_get_text(self, url: str) -> str:
        status, body, _ = self._http_get(url)
        if status != 200 or not body:
            raise RuntimeError(f"拉取失败（HTTP {status}）")
        return body.decode("utf-8", "replace").strip()

    def _http_get_bytes(self, url: str) -> bytes:
        status, body, _ = self._http_get(url)
        if status != 200 or not body:
            raise RuntimeError(f"拉取失败（HTTP {status}）")
        return body

    def _http_post(self, url: str, payload: dict) -> int:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            if self.sender is not None:
                response = self.sender(request, timeout=FETCH_TIMEOUT)
                return response.status if hasattr(response, "status") else 200
            with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code

    # ---------- 工具 ----------

    def _write_json(self, path: Path, payload: dict) -> None:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, path)


def _uuid_hex() -> str:
    import uuid
    return uuid.uuid4().hex


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def selftest() -> bool:
    ok = True

    def check(name: str, cond: bool) -> None:
        nonlocal ok
        print(("ok   " if cond else "FAIL ") + name)
        ok &= cond

    import base64
    import shutil
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="facet-center-"))
    try:
        settings_path = tmp / "center.json"
        state_path = tmp / "center.state.json"
        raw = {"listen": {"host": "127.0.0.1", "port": 8787},
               "channels": [
                   {"name": "acme", "protocol": "openai", "provider": "acme",
                    "mode": "payg", "base_url": "https://api.acme.example/v1",
                    "api_key": "env:ACME_KEY", "enabled": True, "tier": "free",
                    "models": {"auto": "acme-lite"},
                    "model_quota": {"acme-lite": {"tokens": 1000}}},
                   {"name": "my-own", "protocol": "openai",
                    "base_url": "https://self.example/v1", "enabled": True,
                    "models": {"auto": "self-model"}},
                   {"name": "dead", "protocol": "openai",
                    "provider": "deadvendor", "mode": "payg",
                    "base_url": "https://api.dead.example/v1", "enabled": True,
                    "tier": "free", "models": {"auto": "dead-lite"}},
               ]}
        # 预置渠道安装时记录过指纹 → 未被修改，可自动更新
        raw["channels"][0]["preset_fingerprint"] = channel_fingerprint(
            raw["channels"][0])
        client = CenterClient(settings_path, state_path, get_raw=lambda: raw)
        check("默认设置含中心地址与公钥",
              client.settings()["url"] == DEFAULT_URL
              and client.settings()["public_key"] == DEFAULT_PUBLIC_KEY)
        check("install_id 稳定且非空", len(client.install_id()) >= 8)
        check("预置指纹可计算且稳定",
              channel_fingerprint(raw["channels"][0])
              == channel_fingerprint(raw["channels"][0]))

        registry = {
            "vendors": [
                {"id": "acme", "name": "Acme AI", "status": "active",
                 "endpoints": {"openai": "https://api.acme.example/v1"},
                 "models": [{"id": "acme-lite", "tier": "free"},
                            {"id": "acme-pro", "tier": "paid"}],
                 "free_quota": {"per_model": {"acme-lite": {"tokens": 2000}}}},
                {"id": "newvendor", "name": "New Vendor", "status": "active",
                 "endpoints": {"openai": "https://api.new.example/v1"},
                 "models": [{"id": "new-model", "tier": "free"}]},
                {"id": "deadvendor", "name": "Dead Vendor", "status": "deprecated",
                 "endpoints": {}, "models": []},
            ]
        }
        items = diff_registry(raw["channels"], registry)
        kinds = {i["kind"] for i in items}
        check("四类合并各有产出（vendor_new/channel_update/offline/自建不处理）",
              kinds == {"vendor_new", "channel_update", "offline"}
              and not any(i["channel"] == "my-own" for i in items))
        check("新厂家生成可预填模板（api_key 永空）",
              next(i for i in items if i["kind"] == "vendor_new")["payload"]["api_key"] == ""
              and "preset_fingerprint" in next(
                  i for i in items if i["kind"] == "vendor_new")["payload"])

        # 用户改过的渠道：改 base_url 后指纹不一致 → 只提示不自动更新
        changed = json.loads(json.dumps(raw))
        changed["channels"][0]["base_url"] = "https://evil.example/v1"
        changed["channels"][0]["preset_fingerprint"] = channel_fingerprint(raw["channels"][0])
        items2 = diff_registry(changed["channels"], registry)
        check("用户改过的渠道按 preset_modified 处理（不自动覆盖）",
              all(i["kind"] != "channel_update"
                  for i in items2 if i["channel"] == "acme")
              and any(i["kind"] == "preset_modified" and i["channel"] == "acme"
                      for i in items2))
        # 旧渠道（无指纹）同样按已修改处理
        legacy = json.loads(json.dumps(raw))
        legacy["channels"][0].pop("preset_fingerprint")
        items3 = diff_registry(legacy["channels"], registry)
        check("旧渠道（无指纹）按「视为已修改」只提示",
              any(i["kind"] == "preset_modified" and i["channel"] == "acme"
                  for i in items3))

        # 相位错峰：1 万个 install_id 的上报相位应铺满 [0,1)
        buckets = [0] * 10
        for i in range(10000):
            buckets[int(phase_fraction(f"iid-{i:08x}") * 10)] += 1
        check("一万 install_id 相位无尖峰（最大桶 < 13%）",
              max(buckets) / 10000 < 0.13)
        check("相位稳定（同 id 同相位）",
              phase_fraction("abc") == phase_fraction("abc"))

        # 伪造预填：不在签名注册表里的厂家一律拒绝
        check("伪造厂家预填被拒绝", _raises(lambda: client.prefill("forged")))
        try:
            client.prefill("forged")
        except ValueError:
            pass
        # 注册表缓存后真实厂家可预填且不含 key
        client._cache["registry.json"] = {"payload": registry}
        prefill = client.prefill("acme")
        check("签名注册表内厂家可预填且 api_key 为空",
              prefill["channel"]["api_key"] == ""
              and prefill["domain"] == "api.acme.example")

        # 同步：验签失败丢弃、断网降级、缓存保留
        from facet.gateway import ed25519 as ed
        payload_bytes = json.dumps(registry, ensure_ascii=False).encode("utf-8")
        key_seed = b"0" * 32
        pub = ed.public_key(key_seed)
        good_sig = ed.sign(key_seed, payload_bytes)
        live_raw = json.loads(json.dumps(raw))

        def apply_config(new_raw):
            # 模拟网关把新配置写回主配置
            live_raw.clear()
            live_raw.update(new_raw)

        client2 = CenterClient(tmp / "c2.json", tmp / "c2.state.json",
                               get_raw=lambda: live_raw,
                               apply_config=apply_config, version="1.0.4")

        def sender_ok(request, timeout=None):
            class R:
                status = 200
                headers = {"ETag": '"v1"'}
                body = payload_bytes
                def read(self):
                    return self.body
            return R()

        pub_b64 = base64.b64encode(pub).decode()

        def make_sender(body, sig):
            def sender(request, timeout=None):
                class R:
                    status = 200
                    headers = {"ETag": '"v1"'}
                    def __init__(self, data):
                        self.data = data
                    def read(self):
                        return self.data
                # 中心服务把签名以 base64 文本提供（与 CI 产物一致）
                if request.full_url.endswith(".sig"):
                    return R(base64.b64encode(sig))
                return R(body)
            return sender

        def seed_key(client):
            client.save_settings({"public_key": pub_b64})

        client2 = CenterClient(tmp / "c2.json", tmp / "c2.state.json",
                               get_raw=lambda: live_raw,
                               apply_config=apply_config, version="1.0.4")
        seed_key(client2)
        client2.sender = make_sender(payload_bytes, good_sig)
        client2._cache["registry.json"] = {"payload": None}
        summary = client2.sync_now()
        check("验签通过的同步成功并缓存",
              summary["ok"] and client2._cache["registry.json"]["payload"] == registry)
        check("自动应用 channel_update（额度更新写回配置）",
              live_raw["channels"][0]["model_quota"]["acme-lite"]["tokens"] == 2000)

        tampered = bytearray(payload_bytes)
        tampered[0] ^= 0xFF
        client3 = CenterClient(tmp / "c3.json", tmp / "c3.state.json",
                               get_raw=lambda: raw, version="1.0.4")
        seed_key(client3)
        client3.sender = make_sender(payload_bytes, ed.sign(key_seed, bytes(tampered)))
        client3.sync_now()
        check("篡改产物验签失败并丢弃（不写缓存）",
              client3._cache["registry.json"].get("payload") is None
              or client3._cache["registry.json"]["payload"] != registry)

        def sender_down(request, timeout=None):
            raise OSError("network down")

        client4 = CenterClient(tmp / "c4.json", tmp / "c4.state.json",
                               get_raw=lambda: raw, version="1.0.4")
        client4.sender = sender_down
        client4._cache["registry.json"] = {"payload": registry, "etag": '"v1"'}
        summary = client4.sync_now()
        check("断网时保留本地缓存且网关不受影响",
              summary["errors"] and client4._cache["registry.json"]["payload"] == registry)

        # 心跳：关闭开关立即停止
        client5 = CenterClient(tmp / "c5.json", tmp / "c5.state.json",
                               get_raw=lambda: raw, version="1.0.4")
        calls = []

        def sender_count(request, timeout=None):
            calls.append(request.full_url)
            class R:
                status = 204
                def read(self):
                    return b""
            return R()

        client5.sender = sender_count
        client5.save_settings({"report_anonymous": False})
        client5.run_heartbeat()
        check("关闭匿名上报后心跳不发任何请求", calls == [])
        client5.save_settings({"report_anonymous": True})
        check("行动计数先本地聚合（只计数不上传）",
              client5.record_action("one_click_add_channel") is None
              and not any(u.endswith("/v1/actions") for u in calls)
              and client5._state["actions"]["counts"]["one_click_add_channel"] == 1)
        client5.run_heartbeat()
        check("开启后心跳按相位错峰上报", calls and calls[0].endswith("/v1/ping"))
        check("行动计数随心跳上传聚合值并清空",
              calls[-1].endswith("/v1/actions")
              and client5._state["actions"]["counts"] == {})

        # 情报去重：同一条不重复推送
        intel = {"activities": [{"id": "a1", "vendor": "acme",
                                 "title": "1 亿 tokens", "action": "去控制台领取"}],
                 "changes": []}
        intel_bytes = json.dumps(intel, ensure_ascii=False).encode("utf-8")
        client6 = CenterClient(tmp / "c6.json", tmp / "c6.state.json",
                              get_raw=lambda: raw, version="1.0.4")
        seed_key(client6)
        client6.sender = make_sender(intel_bytes, ed.sign(key_seed, intel_bytes))
        client6.sync_now()
        first = len([i for i in client6._state["pending"] if i["kind"] == "intel"])
        client6.sync_now()
        second = len([i for i in client6._state["pending"] if i["kind"] == "intel"])
        check("同一条情报不重复推送", first == 1 and second == 1)

        # 撤回下发（T4.4）：已收到的标「已撤回」+ 更正；从未见过的直接更正
        intel_live = {"activities": [{"id": "a1", "vendor": "acme", "status": "published",
                                      "title": "1 亿 tokens", "action": "去控制台领取"}],
                      "changes": []}
        intel_retracted = {"activities": [
            {"id": "a1", "vendor": "acme", "status": "retracted",
             "title": "1 亿 tokens", "retracted_reason": "额度已调整"},
            {"id": "a2", "vendor": "other", "status": "retracted",
             "title": "另一条", "retracted_reason": "信息有误"}],
            "changes": []}
        payloads = [
            json.dumps(intel_live, ensure_ascii=False).encode("utf-8"),
            json.dumps(intel_retracted, ensure_ascii=False).encode("utf-8"),
        ]

        intel_state = {"n": 0, "last": None}

        def sender_withdraw(request, timeout=None):
            url = request.full_url

            class R:
                status = 200
                headers = {}
                def __init__(self, data):
                    self.body = data
                def read(self):
                    return self.body

            if url.endswith("intel.json"):
                body = payloads[min(intel_state["n"], len(payloads) - 1)]
                intel_state["n"] += 1
                intel_state["last"] = body
                return R(body)
            if url.endswith("intel.json.sig"):
                return R(base64.b64encode(
                    ed.sign(key_seed, intel_state["last"] or b"{}")))
            if url.endswith("registry.json"):
                reg = json.dumps({"vendors": [], "changes": []},
                                 ensure_ascii=False).encode("utf-8")
                return R(reg)
            if url.endswith("registry.json.sig"):
                reg = json.dumps({"vendors": [], "changes": []},
                                 ensure_ascii=False).encode("utf-8")
                return R(base64.b64encode(ed.sign(key_seed, reg)))
            return R(b"{}")

        client6b = CenterClient(tmp / "c6b.json", tmp / "c6b.state.json",
                               get_raw=lambda: raw, version="1.0.4")
        seed_key(client6b)
        client6b.sender = sender_withdraw
        client6b.sync_now()
        check("撤回前情报正常推送", len([i for i in client6b._state["pending"]
                                        if i["kind"] == "intel"]) == 1)
        client6b.sync_now()
        items = [i for i in client6b._state["pending"] if i["kind"] == "intel"]
        a1 = next(i for i in items if i["id"] == "intel:a1")
        a2 = next(i for i in items if i["id"] == "intel:a2")
        check("已收到的撤回条目标「已撤回」并移除操作入口",
              a1.get("withdrawn") is True
              and a1.get("withdrawn_reason") == "额度已调整"
              and a1["actionable"] is False
              and a1["summary"].startswith("【已撤回】"))
        check("从未见过的撤回直接下发更正说明",
              a2["actionable"] is False
              and "情报已撤回" in a2["summary"]
              and "信息有误" in a2["summary"])
        client6b.sync_now()
        check("撤回条目不重复推送", len([i for i in client6b._state["pending"]
                                        if i["kind"] == "intel"]) == 2)

        # 更新通知：已是最新不提示
        client7 = CenterClient(tmp / "c7.json", tmp / "c7.state.json",
                              get_raw=lambda: raw, version="1.0.4", log=lambda t: None)

        def sender_release(request, timeout=None):
            class R:
                status = 200
                headers = {}
                body = json.dumps({"version": "1.0.4", "notes": "same"}).encode()
                def read(self):
                    return self.body
            return R()

        client7.sender = sender_release
        client7.sync_now()
        check("已是最新版本时不显示任何更新提示",
              not any(i["kind"] == "update" for i in client7._state["pending"]))

        # 诊断上报（T4.3）：失败请求按厂家聚合错误类型，显式开启才上传
        diag_pings = []

        def sender_diag(request, timeout=None):
            diag_pings.append(json.loads(request.data.decode("utf-8")))
            class R:
                status = 204
                def read(self):
                    return b""
            return R()

        client8 = CenterClient(tmp / "c8.json", tmp / "c8.state.json",
                               get_raw=lambda: raw, version="1.0.4")
        client8.sender = sender_diag
        client8.note_upstream_error("acme", "upstream_error", 429)
        client8.note_upstream_error("acme", "upstream_error", 429)
        client8.note_upstream_error("acme", "network_error")
        client8.note_upstream_error("my-own", "upstream_error", 503)
        check("诊断按厂家聚合错误类型（渠道名归一为厂家 id）",
              client8._state["diag"]["vendors"] == {"acme": {"429": 2, "network": 1},
                                                   "self-built": {"5xx": 1}})
        client8.save_settings({"report_diagnostics": False})
        client8.run_heartbeat()
        check("诊断未开启不上传（心跳照常但不带 diag）",
              diag_pings and all("diag" not in p for p in diag_pings)
              and client8._state["diag"]["vendors"] == {"acme": {"429": 2, "network": 1},
                                                       "self-built": {"5xx": 1}})
        diag_pings.clear()
        client8.save_settings({"report_diagnostics": True})
        client8.run_heartbeat()
        check("开启后诊断随心跳上报并清空",
              len(diag_pings) == 1
              and diag_pings[0].get("diag") == {"acme": {"429": 2, "network": 1},
                                                "self-built": {"5xx": 1}}
              and client8._state["diag"]["vendors"] == {})

        # 设置校验：坏 URL / 坏公钥回退默认
        settings, errors = client7.save_settings({"url": "ftp://bad", "enabled": "yes"})
        check("坏配置回退默认且不崩", settings["url"] == DEFAULT_URL and bool(errors))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("自测通过" if ok else "自测失败")
    return ok


def _raises(fn) -> bool:
    try:
        fn()
    except Exception:
        return True
    return False


if __name__ == "__main__":
    import sys
    sys.exit(0 if selftest() else 1)

"""Daily self-maintenance: keep model mappings in sync with upstream catalogs.

The router maintains itself in three tiers so a human is only needed when
things really break:

- auto (catalog-confirmed only): a model the upstream itself removed from its
  /models list disappears from the channel's mapping; an `auto` target that
  vanished is repointed at a surviving model. A config backup is written
  before each auto-apply.
- suggestions: newly added upstream models and repeated quota cooldowns are
  collected for one-click apply from the console. When
  defaults.maintenance_model routes to a free channel, an LLM adds free-quota
  analysis on top; its output is advisory only, never auto-applied.
- alerts: dead keys and empty catalogs are reported; a human decides.
"""
from __future__ import annotations

import html
import json
import re
from urllib.request import Request, urlopen

from . import providers as providers_mod
from .core import AUTO_MODEL, QUOTA_MARKERS

JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)
PLAN_LIST_BLOCK = re.compile(r"\[.*\]", re.DOTALL)
PLAN_MODEL_TOKEN = re.compile(r"[a-z0-9][a-z0-9._-]{3,60}")
URL_RE = re.compile(r"https?://\S+")


def _common_prefix_len(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def _pick_replacement(old: str, candidates: list[str]) -> str | None:
    """Closest surviving model to a vanished one (longest common prefix)."""
    if not candidates:
        return None
    return max(candidates,
               key=lambda m: (_common_prefix_len(old, m), -len(m), m))


def analyze(config, catalogs: dict, states: dict,
            refresh_results=None) -> dict:
    """Pure deterministic pass: plan what to auto-apply and what to report.

    Mutates nothing; returns a report whose `patch` (channel -> new models
    mapping) the caller applies through the normal config pipeline.
    """
    applied: list[dict] = []
    suggestions: list[dict] = []
    alerts: list[dict] = []
    patch: dict[str, dict] = {}

    for channel in config.channels:
        catalog = catalogs.get(channel.name) or {}
        models = catalog.get("models") or []
        changes = catalog.get("changes") or {}
        removed = set(changes.get("removed") or ())
        added = changes.get("added") or []
        if not catalog.get("fetched_at"):
            continue

        if not models:
            alerts.append({
                "channel": channel.name, "kind": "empty_catalog",
                "message": "模型目录为空：密钥可能失效、套餐可能到期或上游未提供 /models",
            })
            continue

        mapping = dict(channel.models)
        touched = False

        if removed:
            drop = [key for key, wire in mapping.items()
                    if key not in (AUTO_MODEL, "*") and wire in removed]
            survivors = {k: v for k, v in mapping.items() if k not in drop}
            if drop and survivors:
                for key in drop:
                    del mapping[key]
                touched = True
                applied.append({
                    "channel": channel.name, "action": "remove_mappings",
                    "models": drop,
                    "reason": "上游目录确认这些模型已下架",
                })
            elif drop:
                alerts.append({
                    "channel": channel.name, "kind": "all_models_removed",
                    "message": "目录显示该渠道的全部已配置模型均已下架，需人工确认",
                })

        auto_target = mapping.get(AUTO_MODEL)
        if auto_target and auto_target in removed and models:
            mapped_survivors = [v for v in mapping.values()
                                if v != auto_target and v != "*" and v in models]
            replacement = (mapped_survivors[0] if mapped_survivors
                           else _pick_replacement(auto_target,
                                                  [m for m in models
                                                   if m != auto_target]))
            if replacement:
                mapping[AUTO_MODEL] = replacement
                touched = True
                applied.append({
                    "channel": channel.name, "action": "repoint_auto",
                    "from": auto_target, "to": replacement,
                    "reason": "auto 目标模型已被上游下架，自动切换到最接近的存活模型",
                })
        elif auto_target and auto_target in removed:
            alerts.append({
                "channel": channel.name, "kind": "auto_target_gone",
                "message": f"auto 目标 {auto_target} 已下架且目录无存活模型，需人工处理",
            })

        if touched:
            patch[channel.name] = mapping

        if added:
            fresh = [m for m in added if m not in mapping]
            if fresh:
                suggestions.append({
                    "kind": "add_model", "channel": channel.name,
                    "models": fresh,
                    "message": f"上游新增模型：{', '.join(fresh[:8])}"
                               + ("…" if len(fresh) > 8 else ""),
                })

    for result in refresh_results or []:
        if not isinstance(result, dict) or result.get("ok"):
            continue
        if result.get("http_status") in (401, 403):
            alerts.append({
                "channel": result.get("name"), "kind": "auth",
                "message": f"拉取模型目录被拒（HTTP {result.get('http_status')}），"
                           "密钥可能已失效",
            })

    for channel in config.channels:
        state = states.get(channel.name) or {}
        reason = str(state.get("reason") or "").lower()
        has_rate = any((channel.rate.rpm, channel.rate.rpd,
                        channel.rate.tpm, channel.rate.tpd))
        if (state.get("cooling") and reason
                and any(m in reason for m in QUOTA_MARKERS) and not has_rate):
            suggestions.append({
                "kind": "set_rate", "channel": channel.name,
                "message": f"渠道因额度冷却（{state.get('reason')}）且未设置本地限流，"
                           "建议在配置中设置 rate 以提前分流",
            })

    return {"applied": applied, "suggestions": suggestions,
            "alerts": alerts, "patch": patch}


def build_prompt(config, catalogs: dict, states: dict, report: dict) -> str:
    """Compact router summary for the maintenance LLM (free-quota analysis)."""
    channels = []
    for channel in config.channels:
        catalog = catalogs.get(channel.name) or {}
        state = states.get(channel.name) or {}
        changes = catalog.get("changes") or {}
        channels.append({
            "name": channel.name,
            "protocol": channel.protocol,
            "priority": channel.priority,
            "enabled": channel.enabled,
            "models": channel.models,
            "catalog_count": len(catalog.get("models") or []),
            "catalog_added": changes.get("added") or [],
            "catalog_removed": changes.get("removed") or [],
            "cooling": bool(state.get("cooling")),
            "cooldown_reason": state.get("reason") or "",
            "consecutive_errors": state.get("consecutive_errors") or 0,
            "last_error": str(state.get("last_error") or "")[:160],
        })
    summary = {
        "channels": channels,
        "auto_applied": report.get("applied") or [],
        "already_suggested": report.get("suggestions") or [],
    }
    return (
        "你是一个本地 LLM 网关的运维助手。下面是每个渠道的配置、上游模型目录变化"
        "（added=上游新上线，removed=上游已下架）与运行状态。请分析：\n"
        "1) 模型映射是否需要增删（新模型值得加映射？下架模型是否还有残留引用？）\n"
        "2) 额度/冷却模式是否提示包月政策或限流政策变化（如新渠道反复 429）\n"
        "3) 其他值得人工注意的风险\n"
        "只输出一个 JSON 对象，不要输出其他文字，格式：\n"
        '{"notes": ["一句话结论", ...], "suggestions": '
        '[{"channel": "渠道名", "message": "具体建议", "action": "add_model|set_rate|none"}]}\n'
        "suggestions 只描述建议，不会自动执行。数据：\n"
        + _dump(summary)
    )


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def parse_llm_output(text: str) -> dict:
    """Extract notes/suggestions from an LLM reply; never raises."""
    result = {"notes": [], "suggestions": []}
    if not isinstance(text, str) or not text.strip():
        return result
    match = JSON_BLOCK.search(text)
    if not match:
        result["notes"] = [text.strip()[:300]]
        return result
    try:
        data = json.loads(match.group(0))
    except ValueError:
        result["notes"] = [text.strip()[:300]]
        return result
    if not isinstance(data, dict):
        return result
    notes = data.get("notes")
    if isinstance(notes, list):
        result["notes"] = [str(n)[:300] for n in notes if n][:10]
    suggestions = data.get("suggestions")
    if isinstance(suggestions, list):
        clean = []
        for item in suggestions:
            if not isinstance(item, dict):
                continue
            message = str(item.get("message") or "").strip()
            if not message:
                continue
            clean.append({
                "kind": str(item.get("action") or "none"),
                "channel": str(item.get("channel") or ""),
                "message": message[:300],
                "source": "llm",
            })
        result["suggestions"] = clean[:10]
    return result


# ------------------------------------------------------- plan model refresh
def fetch_plan_doc(url: str) -> str:
    """GET a subscription's plan documentation page (no auth, 10s timeout)."""
    req = Request(url, headers={"User-Agent": "planrouter-maintenance/1.0"})
    with urlopen(req, timeout=10) as resp:
        return resp.read().decode("utf-8", "replace")


def html_to_text(raw: str) -> str:
    """Reduce an HTML page to plain text: drop script/style, tags, entities.

    <li> items become "- " lines so the list-line extractor still sees them
    after tag stripping.
    """
    raw = re.sub(r"(?is)<(script|style)\b[^>]*>.*?</\1\s*>", " ", raw)
    raw = re.sub(r"(?is)<li\b[^>]*>", "\n- ", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    raw = html.unescape(raw)
    out: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


def extract_plan_models(text: str) -> list[str]:
    """Deterministic pull of plan model ids from list lines of the doc text.

    Only lines that look like list items (-, *, <li>) are considered; from
    each line keep lowercase tokens matching the model-id shape that contain
    a separator (- or .) and are not URL fragments.
    """
    found: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith(("-", "*", "<li>")):
            continue
        for token in PLAN_MODEL_TOKEN.findall(URL_RE.sub(" ", stripped).lower()):
            if ("-" not in token and "." not in token) \
                    or "http" in token or "://" in token or "/" in token:
                continue
            if token not in seen:
                seen.add(token)
                found.append(token)
    return found


def build_plan_models_prompt(doc_text: str) -> str:
    return (
        "从以下文档文本提取套餐支持的模型 id，仅输出 JSON 字符串数组，"
        "不要输出其他文字：\n" + doc_text[:12000]
    )


def parse_plan_models_reply(text: str) -> list[str]:
    """Extract a JSON string array from the LLM reply; empty list on failure."""
    if not isinstance(text, str) or not text.strip():
        return []
    match = PLAN_LIST_BLOCK.search(text[:6000])
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    return [str(m).strip() for m in data if str(m).strip()]


def refresh_plan_models(app, report: dict) -> None:
    """Refresh each subscription's plan model list from its documentation URL.

    Fetch failures keep the old list and land in the report alerts; a changed
    list is written back onto the in-memory subscription record and reported
    as applied (which also lands in maintenance.jsonl via the caller).
    """
    for record in app.subscriptions:
        url = str(record.get("models_url") or "").strip()
        if not url:
            continue
        label = f"{record.get('vendor') or '—'} / {record.get('plan') or '—'}"
        try:
            text = html_to_text(fetch_plan_doc(url))
        except Exception as exc:  # any fetch failure must keep the old list
            report["alerts"].append({
                "channel": "", "kind": "plan_models_fetch",
                "message": f"抓取套餐文档失败：{label}（{str(exc)[:160]}），保留原模型清单",
            })
            continue
        extracted = extract_plan_models(text)
        source = "regex"
        if not extracted:
            model = app.config.defaults.maintenance_model
            if not model:
                continue
            try:
                reply = app._maintenance_llm_call(model, build_plan_models_prompt(text))
            except Exception as exc:
                report["alerts"].append({
                    "channel": "", "kind": "plan_models_llm",
                    "message": f"LLM 提取套餐模型请求失败：{label}（{str(exc)[:160]}），保留原模型清单",
                })
                continue
            parsed = parse_plan_models_reply(str(reply)[:6000])
            if not parsed:
                report["alerts"].append({
                    "channel": "", "kind": "plan_models_llm",
                    "message": f"LLM 未能从套餐文档提取出模型清单：{label}，保留原模型清单",
                })
                continue
            extracted, source = parsed, "llm"
        old = [str(m) for m in record.get("models") or []]
        new = [str(m).strip() for m in extracted if str(m).strip()]
        added = [m for m in new if m not in old]
        removed = [m for m in old if m not in new]
        if not new or (not added and not removed):
            continue
        record["models"] = new
        report["applied"].append({
            "channel": ", ".join(record.get("channels") or []) or record.get("vendor") or "",
            "action": "update_plan_models",
            "vendor": record.get("vendor") or "",
            "plan": record.get("plan") or "",
            "models": new,
            "added": len(added), "removed": len(removed), "source": source,
            "reason": f"套餐文档更新模型清单：新增 {len(added)} 个、移除 {len(removed)} 个（来源 {source}）",
        })


def refresh_providers(app, report: dict) -> None:
    """Refresh each registry mode's model list from its docs_url page.

    For every vendor mode that exposes a docs_url, the model list is pulled
    with the same deterministic extraction used for subscription plans. A
    changed list is written onto the in-memory registry entry and persisted
    into the user overlay file (with a backup); failures only raise alerts
    and never touch the data.
    """
    changed: list[dict] = []
    for provider in getattr(app, "providers", None) or []:
        if not isinstance(provider, dict):
            continue
        for mode_key, mode in (provider.get("modes") or {}).items():
            if not isinstance(mode, dict):
                continue
            url = str(mode.get("docs_url") or "").strip()
            if not url:
                continue
            label = f"{provider.get('name') or provider.get('id') or '—'} / {mode.get('label') or mode_key}"
            try:
                text = html_to_text(fetch_plan_doc(url))
            except Exception as exc:  # any fetch failure must keep the old list
                report["alerts"].append({
                    "channel": "", "kind": "provider_models_fetch",
                    "message": f"抓取厂家文档失败：{label}（{str(exc)[:160]}），保留原模型清单",
                })
                continue
            extracted = extract_plan_models(text)
            if not extracted:
                report["alerts"].append({
                    "channel": "", "kind": "provider_models_empty",
                    "message": f"未能从厂家文档提取出模型清单：{label}，保留原模型清单",
                })
                continue
            old = [str(m) for m in mode.get("models") or []]
            new = [str(m).strip() for m in extracted if str(m).strip()]
            added = [m for m in new if m not in old]
            removed = [m for m in old if m not in new]
            if not added and not removed:
                continue
            mode["models"] = new
            if not any(p is provider for p in changed):
                changed.append(provider)
            report["applied"].append({
                "channel": provider.get("id") or "",
                "action": "update_provider_models",
                "vendor": provider.get("name") or "",
                "mode": mode_key,
                "models": new,
                "added": len(added), "removed": len(removed), "source": "regex",
                "reason": f"厂家文档更新模型清单：{label} 新增 {len(added)} 个、移除 {len(removed)} 个",
            })
    if not changed:
        return
    config_dir = app.config_path.parent if getattr(app, "config_path", None) else None
    if not config_dir:
        return
    try:
        providers_mod.save_overlay(config_dir, changed)
    except OSError as exc:
        report["alerts"].append({
            "channel": "", "kind": "provider_overlay_write",
            "message": f"写入厂家注册表覆盖文件失败（{str(exc)[:160]}），本次变更仅在内存生效",
        })

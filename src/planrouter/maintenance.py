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

import json
import re

from .core import AUTO_MODEL, QUOTA_MARKERS

JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


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

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
  analysis on top; its output is advisory only, never auto-applied. The same
  LLM is also the primary extractor of plan/vendor model lists from doc pages
  (deterministic list-line rules stay as the fallback and when unset).
- alerts: dead keys and empty catalogs are reported; a human decides.
"""
from __future__ import annotations

import html
import json
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.request import Request

from . import providers as providers_mod
from .core import AUTO_MODEL, QUOTA_MARKERS, open_with_proxy

JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)
PLAN_LIST_BLOCK = re.compile(r"\[.*\]", re.DOTALL)
PLAN_MODEL_TOKEN = re.compile(r"[a-z0-9][a-z0-9._-]{3,60}")
# A real model id is an ASCII slug; Harness/feature names from doc tables
# ("豆包搜索", "Computer Use Agent") contain spaces or non-ASCII characters.
MODEL_ID_SHAPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*")
URL_RE = re.compile(r"https?://\S+")

# Doc-page tokens shaped like model ids but not models: dates, filenames and
# vendor plan names picked up from docs sidebars or list headings.
PLAN_NOISE_RE = re.compile(
    r"\d{4}-\d{2}(-\d{2})?"
    r"|[\w-]+\.(?:json|js|ts|md|txt|py|sh|ya?ml|toml|ini|cfg|html?|css)")
PLAN_MODEL_STOPWORDS = frozenset({"coding-plan", "agent-plan"})


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
def fetch_plan_doc(url: str, proxy: str = "") -> str:
    """GET a subscription's plan documentation page (no auth, 10s timeout)."""
    req = Request(url, headers={"User-Agent": "planrouter-maintenance/1.0"})
    with open_with_proxy(req, 10, proxy) as resp:
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
            if token in PLAN_MODEL_STOPWORDS or PLAN_NOISE_RE.fullmatch(token):
                continue
            if not re.search(r"[a-z]", token):
                # Bare numbers / version fragments picked up from page chrome.
                continue
            if token not in seen:
                seen.add(token)
                found.append(token)
    return found


def build_plan_models_prompt(doc_text: str) -> str:
    return (
        "从以下文档文本提取套餐支持的模型 id。模型 id 是形如 kimi-k3、glm-5.3 的"
        " ASCII 标识符（不含空格和中文；豆包/doubao 开头的是真实模型，必须保留）。"
        "必须完整覆盖文档表格里列出的全部模型（文本、图像、视频、语音等全模态模型"
        "都要包含）。排除 Harness/功能名称（如 Agent 记忆、Computer Use Agent）。"
        "仅输出 JSON 字符串数组，不要输出其他文字：\n" + doc_text[:12000]
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


def clean_plan_models(ids) -> list[str]:
    """Dedupe model ids and drop ones that cannot be model names.

    A real model id always contains at least one letter, so bare numeric
    fragments like ``00-8`` (page chrome picked up by extraction) are dropped.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in ids or []:
        name = str(raw).strip()
        if not name or not re.search(r"[A-Za-z]", name) or name in seen:
            continue
        if not MODEL_ID_SHAPE.fullmatch(name):
            # Harness/feature names from doc tables ("豆包搜索", "Computer
            # Use Agent") are not model ids: real ids are ASCII slugs.
            continue
        seen.add(name)
        out.append(name)
    return out


def _llm_extract_doc_models(app, doc_text: str, label: str, kind: str,
                            report: dict) -> list[str] | None:
    """LLM extraction of plan model ids from doc text.

    Calls the first working maintenance model (explicit ids or the verified
    queue when defaults.maintenance_model is empty/auto; a model whose reply
    does not parse is dropped from the queue). Returns the cleaned model
    list, or None when no candidate worked. Stays silent when no maintenance
    model is configured and the queue is empty — rules are the designed
    fallback then; alerts only when candidates were tried and all failed.
    """
    models, _mode = pick_maintenance_models(app)
    if not models:
        return None
    reply, _model = app._maintenance_llm_call_any(
        build_plan_models_prompt(doc_text),
        validate=lambda r: bool(parse_plan_models_reply(str(r)[:6000])))
    if reply is None:
        report["alerts"].append({
            "channel": "", "kind": kind,
            "message": f"LLM 提取模型清单失败：{label}（全部候选失败），"
                       "改用规则提取；可点「测试维护模型」探测可用免费模型",
        })
        return None
    return clean_plan_models(parse_plan_models_reply(str(reply)[:6000]))


def extract_doc_models(app, doc_text: str, label: str, kind: str,
                       report: dict) -> tuple[list[str], str]:
    """Extract plan model ids from a doc page's text.

    The LLM (explicit maintenance models or the verified auto-discovered
    queue) is the primary extractor because it understands prose tables and
    marketing pages; the deterministic list-line rules are the fallback when
    no model is available or the LLM fails.
    Returns (models, source) with source being "llm" or "regex".
    """
    llm = _llm_extract_doc_models(app, doc_text, label, kind, report)
    if llm:
        return llm, "llm"
    return clean_plan_models(extract_plan_models(doc_text)), "regex"


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
            text = html_to_text(fetch_plan_doc(url, proxy=app.docs_proxy()))
        except Exception as exc:  # any fetch failure must keep the old list
            report["alerts"].append({
                "channel": "", "kind": "plan_models_fetch",
                "message": f"抓取套餐文档失败：{label}（{str(exc)[:160]}），保留原模型清单",
            })
            continue
        extracted, source = extract_doc_models(
            app, text, label, "plan_models_llm", report)
        if not extracted:
            continue
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


def fetch_models_json(url: str, proxy: str = "") -> str:
    """GET a vendor models endpoint (no auth, 10s timeout), JSON expected."""
    req = Request(url, headers={"User-Agent": "planrouter-maintenance/1.0"})
    with open_with_proxy(req, 10, proxy) as resp:
        return resp.read().decode("utf-8", "replace")


def parse_models_payload(text: str) -> list[str]:
    """Extract sorted model ids from a models endpoint payload.

    Supports ``{"data": [{"id": ...}]}``, ``{"models": [...]}`` and a bare
    array; elements may be plain strings or objects carrying an ``id``
    field. Raises ValueError when the body is not valid JSON or the shape
    is unsupported.
    """
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"响应不是合法 JSON（{exc}）") from exc
    if isinstance(data, dict):
        if isinstance(data.get("data"), list):
            data = data["data"]
        elif isinstance(data.get("models"), list):
            data = data["models"]
        else:
            raise ValueError("JSON 结构不支持：需要 data/models 数组或纯数组")
    if not isinstance(data, list):
        raise ValueError("JSON 结构不支持：需要 data/models 数组或纯数组")
    models: list[str] = []
    seen: set[str] = set()
    for item in data:
        if isinstance(item, dict):
            item = item.get("id")
        if item is None:
            continue
        name = str(item).strip()
        if name and name not in seen:
            seen.add(name)
            models.append(name)
    return sorted(models)


def _refresh_provider_mode(app, provider: dict, mode_key: str,
                           mode: dict, report: dict) -> bool:
    """Refresh one registry mode's model list in place.

    docs_url takes precedence; when it is absent a models_url JSON
    endpoint is consulted. Returns True when the mode was changed; fetch
    or extraction failures only append to report["alerts"] and keep the
    old list.
    """
    docs_url = str(mode.get("docs_url") or "").strip()
    models_url = str(mode.get("models_url") or "").strip()
    if not docs_url and not models_url:
        return False
    label = f"{provider.get('name') or provider.get('id') or '—'} / {mode.get('label') or mode_key}"

    if docs_url:
        try:
            text = html_to_text(fetch_plan_doc(docs_url, proxy=app.docs_proxy()))
        except Exception as exc:  # any fetch failure must keep the old list
            report["alerts"].append({
                "channel": "", "kind": "provider_models_fetch",
                "message": f"抓取厂家文档失败：{label}（{str(exc)[:160]}），保留原模型清单",
            })
            return False
        extracted, source = extract_doc_models(
            app, text, label, "provider_models_llm", report)
        if not extracted:
            report["alerts"].append({
                "channel": "", "kind": "provider_models_empty",
                "message": f"未能从厂家文档提取出模型清单：{label}，保留原模型清单",
            })
            return False
        where = "文档"
    else:
        try:
            text = fetch_models_json(models_url, proxy=app.docs_proxy())
        except Exception as exc:  # any fetch failure must keep the old list
            report["alerts"].append({
                "channel": "", "kind": "provider_models_fetch",
                "message": f"抓取厂家模型接口失败：{label}（{str(exc)[:160]}），保留原模型清单",
            })
            return False
        try:
            extracted = parse_models_payload(text)
        except ValueError as exc:
            report["alerts"].append({
                "channel": "", "kind": "provider_models_parse",
                "message": f"厂家模型接口解析失败：{label}（{exc}），保留原模型清单",
            })
            return False
        if not extracted:
            report["alerts"].append({
                "channel": "", "kind": "provider_models_empty",
                "message": f"厂家模型接口返回空模型清单：{label}，保留原模型清单",
            })
            return False
        source = "api"
        where = "模型接口"

    old = [str(m) for m in mode.get("models") or []]
    new = [str(m).strip() for m in extracted if str(m).strip()]
    added = [m for m in new if m not in old]
    removed = [m for m in old if m not in new]
    if not added and not removed:
        return False
    mode["models"] = new
    report["applied"].append({
        "channel": provider.get("id") or "",
        "action": "update_provider_models",
        "vendor": provider.get("name") or "",
        "mode": mode_key,
        "models": new,
        "added": len(added), "removed": len(removed), "source": source,
        "reason": f"厂家{where}更新模型清单：{label} 新增 {len(added)} 个、移除 {len(removed)} 个",
    })
    return True


def refresh_providers(app, report: dict, vendor_id=None) -> None:
    """Refresh registry modes' model lists from docs_url / models_url.

    For every vendor mode that exposes a docs_url the LLM extraction runs
    first when defaults.maintenance_model is set, with the deterministic
    list-line rules as fallback; otherwise a models_url JSON endpoint is
    consulted. A changed list is written onto the in-memory registry entry
    and persisted into the user overlay file (with a backup); failures only
    raise alerts and never touch the data. When vendor_id is given only
    the vendor with that id is refreshed.
    """
    changed: list[dict] = []
    for provider in getattr(app, "providers", None) or []:
        if not isinstance(provider, dict):
            continue
        if vendor_id is not None and str(provider.get("id") or "") != vendor_id:
            continue
        for mode_key, mode in (provider.get("modes") or {}).items():
            if not isinstance(mode, dict):
                continue
            if _refresh_provider_mode(app, provider, mode_key, mode, report) \
                    and not any(p is provider for p in changed):
                changed.append(provider)
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


# ------------------------------------------------- maintenance model pool
# Free models come and go, so maintenance never pins one forever. Candidates
# are quizzed with a synthetic doc page whose expected answer is known; only
# models that extract exactly that answer join a persisted queue. When
# defaults.maintenance_model is empty or "auto" the queue drives every
# maintenance LLM call; explicit ids (comma/whitespace separated) are used
# verbatim instead.

MAINTENANCE_QUIZ_DOC = """\
豆包 Agent 套餐（个人版）说明

一、套餐概述
Agent 套餐是面向个人开发者的全模态订阅服务，覆盖对话、编码、图像、视频与语音。
文档编号：agent-plan-personal-plan-overview.md；更新时间：2026-10-28。

二、套餐内支持的模型
- 文本对话：doubao-seed-2.1-pro、doubao-seed-2.0-mini、kimi-k3、glm-5.3、deepseek-v4-pro
- 图像生成：doubao-seedream-5.0-flash、doubao-seedream-5-0-pro
- 视频生成：doubao-seedance-2.0、doubao-seedance-2.0-mini
- 语音合成与识别：doubao-seed-tts-2.0、doubao-seed-asr-2.0

三、套餐内 Agent 能力（功能，不是模型）
- Agent 记忆
- Computer Use Agent
- 豆包搜索
- 深度研究

四、计费说明
套餐按 tokens 计费，超出部分按量付费；以上内容更新于 2026-10-01。
"""

MAINTENANCE_QUIZ_ANSWER = frozenset({
    "doubao-seed-2.1-pro", "doubao-seed-2.0-mini", "kimi-k3", "glm-5.3",
    "deepseek-v4-pro", "doubao-seedream-5.0-flash", "doubao-seedream-5-0-pro",
    "doubao-seedance-2.0", "doubao-seedance-2.0-mini",
    "doubao-seed-tts-2.0", "doubao-seed-asr-2.0",
})

PROBE_CANDIDATES = 10         # models quizzed per probe run
PROBE_WORKERS = 5             # concurrent quiz calls
PROBE_COOLDOWN = 6 * 3600     # never re-probe more often than this


def check_maintenance_quiz(reply) -> tuple[bool, list[str]]:
    """A candidate passes only by returning the exact expected answer set."""
    parsed = clean_plan_models(parse_plan_models_reply(str(reply or "")[:6000]))
    return set(parsed) == set(MAINTENANCE_QUIZ_ANSWER), parsed


def _pool_path(app):
    if getattr(app, "config_path", None):
        return app.config_path.parent / "maintenance_models.json"
    return None


def load_maintenance_pool(app) -> dict:
    """Read the persisted verified queue; broken files yield an empty pool."""
    pool: dict = {"queue": [], "results": [], "verified_at": 0.0,
                  "last_probe_at": 0.0}
    path = _pool_path(app)
    if not path:
        return pool
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return pool
    if isinstance(data, dict):
        for key in ("queue", "results"):
            if isinstance(data.get(key), list):
                pool[key] = data[key]
        pool["verified_at"] = float(data.get("verified_at") or 0)
        pool["last_probe_at"] = float(data.get("last_probe_at") or 0)
    return pool


def save_maintenance_pool(app) -> None:
    path = _pool_path(app)
    if not path:
        return  # no config dir (tests): keep the pool in memory only
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(app.maintenance_pool, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass  # a failed pool write must never break maintenance


def read_jsonl_tail(path, limit: int) -> tuple[list[str], bool]:
    """Return the last `limit` non-empty JSONL lines plus whether older exist.

    Reads the file backwards in chunks so a long history never forces the
    caller to load the whole log into memory.
    """
    limit = max(1, int(limit))
    try:
        size = path.stat().st_size
    except OSError:
        return [], False
    chunk = 65536
    pos = size
    buf = b""
    lines: list[bytes] = []
    with path.open("rb") as fh:
        while True:
            segs = buf.split(b"\n")
            if pos > 0 and segs:
                segs = segs[1:]  # head segment may be cut mid-line
            lines = [s for s in segs if s.strip()]
            if pos == 0 or len(lines) > limit:
                break
            step = min(chunk, pos)
            pos -= step
            fh.seek(pos)
            buf = fh.read(step) + buf
    has_more = pos > 0 or len(lines) > limit
    return [s.decode("utf-8", "replace") for s in lines[-limit:]], has_more


def free_model_candidates(app, limit: int = PROBE_CANDIDATES) -> list[str]:
    """Quiz candidates: verified queue members first, then :free catalog ids.

    Only channels marked tier "free" contribute catalog ids, so probing never
    burns paid quota; the queue is re-verified on every probe.
    """
    with app.maintenance_pool_lock:
        queue = [str(m) for m in (app.maintenance_pool.get("queue") or [])]
    free: list[str] = []
    for channel in app.config.channels:
        if channel.tier != "free":
            continue
        catalog = app.store.catalogs.get(channel.name) or {}
        free.extend(str(m) for m in catalog.get("models") or []
                    if str(m).endswith(":free"))
    rest = [m for m in free if m not in queue]
    filler = sorted(random.sample(rest, min(len(rest), max(0, limit - len(queue)))))
    return (queue + filler)[:limit]


def probe_maintenance_models(app, candidates=None) -> dict:
    """Quiz candidate models with the synthetic doc; perfect scorers queue up.

    The report records every candidate's outcome; the queue is persisted so
    restarts keep using verified models. Called on demand from the console
    and automatically when the pool mode finds an empty queue.
    """
    if candidates is None:
        candidates = free_model_candidates(app)
    candidates = [str(m) for m in candidates][:PROBE_CANDIDATES]
    prompt = build_plan_models_prompt(MAINTENANCE_QUIZ_DOC)
    results: list[dict] = []
    if candidates:
        with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as executor:
            futures = {executor.submit(app._maintenance_llm_call, m, prompt): m
                       for m in candidates}
            for future in as_completed(futures):
                model = futures[future]
                try:
                    ok, parsed = check_maintenance_quiz(future.result())
                    results.append({
                        "model": model, "ok": ok,
                        "hit": len(parsed), "expected": len(MAINTENANCE_QUIZ_ANSWER),
                        "error": "" if ok else "提取结果与标准答案不一致",
                    })
                except Exception as exc:
                    results.append({
                        "model": model, "ok": False, "hit": 0,
                        "expected": len(MAINTENANCE_QUIZ_ANSWER),
                        "error": str(exc)[:160],
                    })
    results.sort(key=lambda r: (not r["ok"], r["model"]))
    passing = [r["model"] for r in results if r["ok"]]
    with app.maintenance_pool_lock:
        app.maintenance_pool["queue"] = passing
        app.maintenance_pool["results"] = results
        app.maintenance_pool["verified_at"] = time.time()
        app.maintenance_pool["last_probe_at"] = time.time()
        verified_at = app.maintenance_pool["verified_at"]
        save_maintenance_pool(app)
    return {"queue": list(passing), "results": results,
            "candidates": len(candidates), "verified_at": verified_at}


def pick_maintenance_models(app) -> tuple[list[str], str]:
    """Ordered LLM candidates for maintenance work: (models, mode).

    mode is "fixed" when defaults.maintenance_model lists explicit ids and
    "pool" when the verified queue drives selection. In pool mode an empty
    queue triggers one probe (bounded by PROBE_COOLDOWN so a fully dead
    free pool cannot turn maintenance into a probe storm).
    """
    raw = str(app.config.defaults.maintenance_model or "").strip()
    if raw and raw.lower() != "auto":
        ids = [m for m in (s.strip() for s in re.split(r"[,;\s]+", raw)) if m]
        return ids, "fixed"
    with app.maintenance_pool_lock:
        queue = list(app.maintenance_pool.get("queue") or [])
        last_probe = float(app.maintenance_pool.get("last_probe_at") or 0)
    if not queue and time.time() - last_probe >= PROBE_COOLDOWN:
        probe_maintenance_models(app)
        with app.maintenance_pool_lock:
            queue = list(app.maintenance_pool.get("queue") or [])
    return queue, "pool"


def drop_maintenance_model(app, model: str, error: str) -> None:
    """Remove a model that just failed real maintenance work from the queue."""
    with app.maintenance_pool_lock:
        queue = app.maintenance_pool.get("queue") or []
        if model not in queue:
            return
        queue.remove(model)
        results = app.maintenance_pool.setdefault("results", [])
        results.insert(0, {
            "model": model, "ok": False, "hit": 0,
            "expected": len(MAINTENANCE_QUIZ_ANSWER),
            "error": f"维护调用失败：{str(error)[:140]}",
        })
        del results[20:]
        save_maintenance_pool(app)

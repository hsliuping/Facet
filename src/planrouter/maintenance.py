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
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qsl, urlencode, urlsplit
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


def build_prompt(config, catalogs: dict, states: dict, report: dict,
                 usage: dict | None = None) -> str:
    """Compact router summary for the maintenance LLM (free-quota analysis).

    `usage` (channel name -> today's counters) is optional; None keeps the
    prompt free of usage data.
    """
    channels = []
    for channel in config.channels:
        catalog = catalogs.get(channel.name) or {}
        state = states.get(channel.name) or {}
        changes = catalog.get("changes") or {}
        entry = {
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
        }
        if usage is not None:
            day = usage.get(channel.name) or {}
            entry["today"] = {k: day.get(k, 0)
                              for k in ("requests", "success", "failed", "tokens")}
        channels.append(entry)
    summary = {
        "channels": channels,
        "auto_applied": report.get("applied") or [],
        "already_suggested": report.get("suggestions") or [],
    }
    if usage is not None:
        summary["usage_today"] = usage
    return (
        "你是一个本地 LLM 网关的运维助手。下面是每个渠道的配置、上游模型目录变化"
        "（added=上游新上线，removed=上游已下架）与运行状态。请分析：\n"
        "1) 模型映射是否需要增删（新模型值得加映射？下架模型是否还有残留引用？）\n"
        "2) 额度/冷却模式是否提示包月政策或限流政策变化（如新渠道反复 429）；"
        "结合当日用量（失败占比、token 量）判断额度模式\n"
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

    <li> items and table rows become "- " lines so the list-line extractor
    still sees them after tag stripping. Some vendors (Volcengine's agent plan
    overview) publish their model list only inside an HTML table, which the
    plain tag stripper would otherwise flatten into one unlabeled blob.
    """
    raw = re.sub(r"(?is)<(script|style)\b[^>]*>.*?</\1\s*>", " ", raw)
    raw = re.sub(r"(?is)<li\b[^>]*>", "\n- ", raw)
    # Collapse a whole table row into one "- " line; cell text keeps a tab
    # separator so neighbouring cells cannot merge into one fake token.
    raw = re.sub(r"(?is)<tr\b[^>]*>(.*?)</tr>",
                 lambda m: "\n- " + " ".join(m.group(1).split()) + " ", raw)
    raw = re.sub(r"(?is)</(td|th)>", " \t", raw)
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
        "严格按文档中出现的原始拼写输出，不得自行改写分隔符：文档写点号就输出点号、"
        "写横杠就输出横杠，不要把 doubao-seed-2.0-mini 改成 doubao-seed-2-0-mini，"
        "也不要把 glm-5.3 改成 glm_5_3。仅输出 JSON 字符串数组，不要输出其他文字：\n"
        + doc_text[:12000]
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


def _normalize_model_id(model_id: str) -> str:
    """Normalize separators for variant detection (`.`/`-`/`_` are equivalent)."""
    return re.sub(r"[._-]", "", str(model_id)).lower()


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
    queue plus the pool's untried candidates when defaults.maintenance_model
    is empty/auto; each failure is dropped and the next candidate is tried).
    Returns the cleaned model list, or None only when no candidate is left —
    every candidate having been tried before rules take over. Stays silent
    when no maintenance model is configured and no pool candidate exists —
    rules are the designed fallback then; alerts only when candidates were
    tried and all failed.
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
        label = f"{app.subscription_label(record) or '—'} / {record.get('plan') or '—'}"
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
        if source == "regex":
            # The regex fallback is noisy: it can miss real ids (a table-only
            # page yields a subset), so it may add but never remove. Removing
            # models needs the confirmed LLM extraction.
            new = old + added
            removed = []
        if not new or (not added and not removed):
            continue
        record["models"] = new
        label = app.subscription_label(record)
        report["applied"].append({
            "channel": ", ".join(app.subscription_channels(record)) or label,
            "action": "update_plan_models",
            "vendor": label,
            "plan": record.get("plan") or "",
            "models": new,
            "added": len(added), "removed": len(removed), "source": source,
            "reason": f"套餐文档更新模型清单：新增 {len(added)} 个、移除 {len(removed)} 个（来源 {source}）",
        })


def fetch_models_json(url: str, proxy: str = "", api_key: str = "") -> str:
    """GET a vendor models endpoint (optional Bearer auth), JSON expected.

    Public catalog endpoints need no auth; key-protected ones like Bailian's
    GET /api/v1/models authenticate with the user's own vendor key, which the
    caller passes after borrowing it from a channel of the same vendor.
    """
    headers = {"User-Agent": "planrouter-maintenance/1.0"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = Request(url, headers=headers)
    with open_with_proxy(req, 10, proxy) as resp:
        return resp.read().decode("utf-8", "replace")


def _model_ids_from(data) -> list[str]:
    """Pull sorted unique model ids from a decoded models payload.

    Supports OpenAI ``{"data": [...]}``, ``{"models": [...]}``, Bailian
    ``{"output": {"models": [...]}}`` and bare arrays; elements may be plain
    strings or objects carrying ``id`` / ``model`` fields.
    """
    if isinstance(data, dict):
        if isinstance(data.get("data"), list):
            data = data["data"]
        elif isinstance(data.get("models"), list):
            data = data["models"]
        elif (isinstance(data.get("output"), dict)
                and isinstance(data["output"].get("models"), list)):
            data = data["output"]["models"]
        else:
            raise ValueError(
                "JSON 结构不支持：需要 data/models/output.models 数组或纯数组")
    if not isinstance(data, list):
        raise ValueError(
            "JSON 结构不支持：需要 data/models/output.models 数组或纯数组")
    models: list[str] = []
    seen: set[str] = set()
    for item in data:
        if isinstance(item, dict):
            item = item.get("id") or item.get("model")
        if item is None:
            continue
        name = str(item).strip()
        if name and name not in seen:
            seen.add(name)
            models.append(name)
    return sorted(models)


def parse_models_payload(text: str) -> list[str]:
    """Extract sorted model ids from a models endpoint payload.

    Raises ValueError when the body is not valid JSON or the shape is
    unsupported.
    """
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"响应不是合法 JSON（{exc}）") from exc
    return _model_ids_from(data)


def _page_url(url: str, page_no: int, page_size: int = 100) -> str:
    """Append/replace page_no & page_size query params on a models URL."""
    parts = urlsplit(url)
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    params["page_no"] = str(page_no)
    params["page_size"] = str(page_size)
    return parts._replace(query=urlencode(params)).geturl()


def _pagination_total(text: str) -> int:
    """Advertised total for a Bailian-style paginated models payload, else 0.

    Bailian's GET /api/v1/models answers ``{"output": {"total": N, "models":
    [...]}}`` and pages via page_no/page_size query params (20 per page by
    default); plain OpenAI-style payloads are not paginated and yield 0.
    """
    try:
        data = json.loads(text)
    except ValueError:
        return 0
    if not isinstance(data, dict):
        return 0
    output = data.get("output")
    if not isinstance(output, dict) or not isinstance(output.get("models"), list):
        return 0
    try:
        return max(0, int(output.get("total") or 0))
    except (TypeError, ValueError):
        return 0


def _channel_for_provider(app, provider_id: str, base_url: str = ""):
    """First configured channel of this vendor carrying an API key.

    Vendor model endpoints such as Bailian's /api/v1/models authenticate with
    the user's own key; the channel created from the same vendor preset is
    the natural source for it (and for its outbound proxy). Vendors with
    several billing modes may run one key per endpoint (Bailian payg uses
    sk-... while Token Plan uses sk-sp-...), so a channel whose base_url
    matches the mode is preferred over any other channel of the vendor.
    """
    if not provider_id:
        return None
    channels = getattr(getattr(app, "config", None), "channels", None) or []
    same = [c for c in channels
            if getattr(c, "provider", "") == provider_id and c.api_key]
    if base_url:
        wanted = base_url.rstrip("/")
        for channel in same:
            if channel.base_url.rstrip("/") == wanted:
                return channel
    return same[0] if same else None


def _refresh_provider_mode(app, provider: dict, mode_key: str,
                           mode: dict, report: dict) -> bool:
    """Refresh one registry mode's model list in place.

    docs_url takes precedence; when it is absent a models_url JSON endpoint
    is consulted — with the Bearer key of a configured channel of the same
    vendor when one exists (Bailian's /api/v1/models needs the user's own
    key), and following Bailian-style pagination when the payload advertises
    a larger total. Returns True when the mode was changed; fetch or
    extraction failures only append to report["alerts"] and keep the old
    list.
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
        channel = _channel_for_provider(app, str(provider.get("id") or ""),
                                        str(mode.get("base_url") or ""))
        api_key = channel.api_key if channel else ""
        proxy = channel.proxy if channel and channel.proxy else app.docs_proxy()
        try:
            text = fetch_models_json(models_url, proxy=proxy, api_key=api_key)
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
        total = _pagination_total(text)
        if total > len(extracted):
            # Bailian-style paging: collect the remaining pages (capped so a
            # lying total cannot loop forever); any failure keeps the old list.
            try:
                for page_no in range(2, 11):
                    page_text = fetch_models_json(
                        _page_url(models_url, page_no), proxy=proxy,
                        api_key=api_key)
                    page_ids = parse_models_payload(page_text)
                    if not page_ids:
                        break
                    extracted = sorted(set(extracted) | set(page_ids))
                    if len(extracted) >= total:
                        break
            except Exception as exc:
                report["alerts"].append({
                    "channel": "", "kind": "provider_models_fetch",
                    "message": f"抓取厂家模型接口失败（分页）：{label}"
                               f"（{str(exc)[:160]}），保留原模型清单",
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
    # LLM extraction occasionally rewrites separators (doubao-seed-2.0-mini ->
    # doubao-seed-2-0-mini). Since ``mode["models"]`` is replaced wholesale, a
    # variant would silently oust the correct spelling. Map any extracted model
    # whose normalized form matches an existing entry back to that entry's
    # literal spelling, so the canonical spelling wins and nothing is dropped.
    old_norm = {_normalize_model_id(m): m for m in old}
    normalized: list[str] = []
    for m in new:
        canonical = old_norm.get(_normalize_model_id(m))
        normalized.append(canonical if canonical else m)
    new = normalized
    added = [m for m in new if m not in old]
    removed = [m for m in old if m not in new]
    if source == "regex":
        # Regex doc extraction is the noisiest source (doc sidebars yield
        # tokens like "agent."): it may add but never remove. Removing models
        # needs a confirmed LLM extraction or the vendor's own models API.
        new = old + added
        removed = []
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
PROBE_WORKERS = 5             # channel groups quizzed concurrently;
                              # models inside one group stay serial (429)
PROBE_COOLDOWN = 6 * 3600     # never re-probe more often than this
MODEL_TEST_LIMIT = 200        # per-model quiz outcomes kept in the pool file


def _trim_tests(tests: dict) -> None:
    """Keep only the newest MODEL_TEST_LIMIT per-model quiz outcomes."""
    if len(tests) <= MODEL_TEST_LIMIT:
        return
    oldest = sorted(tests, key=lambda m: float(tests[m].get("tested_at") or 0))
    for model in oldest[:len(tests) - MODEL_TEST_LIMIT]:
        tests.pop(model, None)


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
    pool: dict = {"queue": [], "results": [], "tests": {}, "verified_at": 0.0,
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
        if isinstance(data.get("tests"), dict):
            pool["tests"] = data["tests"]
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


# Vendor catalogs list every registered id; ids matching these fragments are
# embedding/reward/rerank/guard/vision utilities that cannot answer the quiz.
NON_CHAT_PATTERNS = (
    "embed", "reward", "rerank", "clip", "riva", "guard", "content-safety",
    "parse", "detector", "diffusion", "kosmos", "deplot", "fuyu", "neva",
    "vila", "transcribe", "tts",
)


def is_chat_model(model: str) -> bool:
    """Heuristic: False for ids that cannot serve chat completions."""
    low = model.lower()
    return not any(p in low for p in NON_CHAT_PATTERNS)


def _free_mapping_values(app, channel) -> tuple[list[str], int, int]:
    """Own mapping values that look usable, plus why the rest were hidden.

    Returns (values, paid_hidden, nonchat_hidden). A catalog that contains
    :free-suffixed ids (OpenRouter style) marks the channel as mixed: then
    only mapping values suffixed :free are free; a catalog without any :free
    id belongs to a wholly-free channel (NIM, doubao, ...), so every own
    mapping counts. Ids that look like embedding/reward/vision utilities are
    always skipped — they cannot answer the quiz — and counted as non-chat.
    """
    values = [str(v) for v in channel.models.values()
              if v and v not in (AUTO_MODEL, "*")]
    catalog = app.store.catalogs.get(channel.name) or {}
    models = [str(m) for m in catalog.get("models") or []]
    if any(m.endswith(":free") for m in models):
        usable = [v for v in values if v.endswith(":free")]
    else:
        usable = values
    paid_hidden = len(values) - len(usable)
    chat = [v for v in usable if is_chat_model(v)]
    return chat, paid_hidden, len(usable) - len(chat)


def free_model_candidates(app, limit: int = PROBE_CANDIDATES) -> list[str]:
    """Quiz candidates: verified queue, then the free channels' own models
    round-robin, then a random sample of the vendor catalogs.

    Only channels marked tier "free" contribute ids, so probing never burns
    paid quota; the queue is re-verified on every probe. Channel mappings
    donate round-robin — one id per channel per round — so a channel stuffed
    with dead mappings cannot starve the others (its leftovers still fill any
    free slots). Mappings count as free only when they carry the :free suffix
    or the channel's catalog has no :free id at all (a wholly-free tier). A
    catalog that mixes :free-suffix and unsuffixed ids (OpenRouter style)
    donates only its :free subset; a catalog without any :free id belongs to
    a wholly-free channel, so every model is a candidate. Before the first
    catalog fetch the channel's own mapping stands in.
    """
    with app.maintenance_pool_lock:
        queue = [str(m) for m in (app.maintenance_pool.get("queue") or [])]
    own_by_channel: list[list[str]] = []
    free: list[str] = []
    for channel in app.config.channels:
        if channel.tier != "free":
            continue
        free_own, _paid, _nonchat = _free_mapping_values(app, channel)
        free_own = [m for m in free_own if not app.free_quota_exhausted(channel, m)]
        own_by_channel.append(free_own)
        catalog = app.store.catalogs.get(channel.name) or {}
        models = [str(m) for m in catalog.get("models") or []]
        marked = [m for m in models if m.endswith(":free")]
        pool = marked if marked else models
        free.extend(m for m in pool if is_chat_model(m)
                    and not app.free_quota_exhausted(channel, m))
    chosen = list(dict.fromkeys(queue))
    for round_index in range(max((len(own) for own in own_by_channel),
                                 default=0)):
        for own in own_by_channel:
            if round_index < len(own) and own[round_index] not in chosen:
                chosen.append(own[round_index])
    rest = [m for m in dict.fromkeys(free) if m not in chosen]
    if len(chosen) < limit and rest:
        chosen.extend(sorted(random.sample(rest, min(len(rest), limit - len(chosen)))))
    return chosen[:limit]


# A verified-dead mapping is skipped by routing only for a while: free quotas
# reset, upstreams recover from outages, and a model can come back. Expiring
# the label keeps one bad probe from shrinking the free pool forever.
DEAD_LABEL_TTL = 6 * 3600
VERIFY_FREE_PROMPT = "只回复 OK 两个字母，不要输出任何其他内容。"
_HTTP_STATUS = re.compile(r"HTTP Error (\d{3})")


def _dead_key(channel: str, model: str) -> str:
    return f"{channel}\u0000{model}"


def dead_model_labels(app, now: float | None = None) -> dict[str, dict]:
    """Current dead labels, expired ones pruned; keyed by channel+model."""
    now = time.time() if now is None else now
    with app.maintenance_pool_lock:
        ledger = app.maintenance_pool.setdefault("dead_models", {})
        expired = [key for key, entry in ledger.items()
                   if now - float(entry.get("marked_at") or 0) > DEAD_LABEL_TTL]
        for key in expired:
            ledger.pop(key, None)
        return {key: dict(entry) for key, entry in ledger.items()}


def dead_model_set(app, now: float | None = None) -> set[tuple[str, str]]:
    """(channel, model) pairs routing must skip; labels expire on their own."""
    keys = dead_model_labels(app, now)
    pairs = set()
    for key in keys:
        channel, _, model = key.partition("\u0000")
        if channel and model:
            pairs.add((channel, model))
    return pairs


def dead_models_payload(app) -> list[dict]:
    """Console view: which mappings are currently labelled dead and why."""
    labels = dead_model_labels(app)
    rows = [dict(entry) for entry in labels.values()]
    rows.sort(key=lambda r: (r.get("channel") or "", r.get("model") or ""))
    return rows


def label_dead_model(app, channel_name: str, model: str, ok: bool,
                     error: str = "") -> None:
    """Record one verification outcome against a channel's own mapping."""
    if not channel_name:
        return
    with app.maintenance_pool_lock:
        _label_locked(app.maintenance_pool, channel_name, model, ok, error)


def _label_locked(pool: dict, channel_name: str, model: str, ok: bool,
                  error: str = "") -> None:
    """Caller already holds maintenance_pool_lock."""
    ledger = pool.setdefault("dead_models", {})
    key = _dead_key(channel_name, model)
    if ok:
        ledger.pop(key, None)
        return
    ledger[key] = {"channel": channel_name, "model": model,
                   "reason": str(error or "调用失败")[:160],
                   "marked_at": time.time()}


def free_mapping_pairs(app) -> list[tuple[object, str]]:
    """Every chat model an enabled tier-free channel maps.

    Same free-mapping rules the queue probe uses (_free_mapping_values), but
    covering all of them instead of the first handful — this is the "is the
    whole free pool still usable" sweep.
    """
    pairs: list[tuple[object, str]] = []
    for channel in app.config.channels:
        if not channel.enabled or channel.tier != "free":
            continue
        for wire in dict.fromkeys(_free_mapping_values(app, channel)[0]):
            if app.free_quota_exhausted(channel, wire):
                continue
            pairs.append((channel, wire))
    return pairs


def _failure_marks_dead(text: str) -> bool:
    """Only a definitive "this model id is gone" verdict may kill a mapping.

    Auth errors, quota (402/429), 5xx and timeouts are transient for a free
    tier — labelling them would shrink the pool on an outage, so they stay
    inconclusive and the mapping keeps its place.
    """
    match = _HTTP_STATUS.search(text or "")
    if not match:
        return False
    status = int(match.group(1))
    if status == 404:
        return True
    return status in (400, 422) and "model" in (text or "").lower()


def verify_free_models(app, pairs=None) -> dict:
    """Ping every mapped model of every enabled free channel.

    A one-word reply proves the mapping answers; the 11-id quiz stays
    reserved for the maintenance queue because it costs far more per call.
    Channels run concurrently, models inside one channel strictly serially:
    a free key answers one request at a time (parallel calls get 429, which
    would be misread as the pool being dead).
    """
    pairs = free_mapping_pairs(app) if pairs is None else pairs
    groups: dict[str, list[tuple[object, str]]] = {}
    for channel, wire in pairs:
        groups.setdefault(channel.name, []).append((channel, wire))

    def run_ping(channel, wire: str) -> dict:
        row = {"channel": channel.name, "model": wire}
        try:
            reply = app._maintenance_direct_call(channel, wire,
                                                 VERIFY_FREE_PROMPT)
        except Exception as exc:  # noqa: BLE001 - upstream errors vary
            text = str(exc)[:160]
            row.update({"ok": False, "dead": _failure_marks_dead(text),
                        "error": text})
        else:
            ok = bool(str(reply or "").strip())
            row.update({"ok": ok, "dead": False,
                        "error": "" if ok else "空回复（无法判定）"})
        return row

    def run_group(entry: tuple[str, list[tuple[object, str]]]) -> list[dict]:
        _name, items = entry
        return [run_ping(channel, wire) for channel, wire in items]

    results: list[dict] = []
    if groups:
        with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as executor:
            for rows in executor.map(run_group, groups.items()):
                results.extend(rows)
    results.sort(key=lambda r: (not r["ok"], r["channel"], r["model"]))
    now = time.time()
    with app.maintenance_pool_lock:
        for row in results:
            if row["ok"] or row["dead"]:
                _label_locked(app.maintenance_pool, row["channel"], row["model"],
                              row["ok"], row["error"])
        app.maintenance_pool["free_scan_at"] = now
        save_maintenance_pool(app)
    return {
        "checked": len(results),
        "alive": sum(1 for r in results if r["ok"]),
        "dead": sum(1 for r in results if r["dead"]),
        "inconclusive": sum(1 for r in results if not r["ok"] and not r["dead"]),
        "scanned_at": now,
        "results": results,
        "dead_models": dead_models_payload(app),
    }


def probe_maintenance_models(app, candidates=None) -> dict:
    """Quiz candidate models with the synthetic doc; perfect scorers queue up.

    The report records every candidate's outcome; the queue is persisted so
    restarts keep using verified models. Called on demand from the console
    and automatically when the pool mode finds an empty queue. Models are
    grouped by their serving channel: groups run concurrently, but the
    models inside one group run strictly serially because free tiers such
    as OpenRouter answer only one concurrent request per key (else 429).
    """
    if candidates is None:
        candidates = free_model_candidates(app)
    candidates = [str(m) for m in candidates][:PROBE_CANDIDATES]
    prompt = build_plan_models_prompt(MAINTENANCE_QUIZ_DOC)
    results: list[dict] = []
    if candidates:
        def run_quiz(model: str, channel_name: str) -> dict:
            row = {"model": model, "channel": channel_name}
            try:
                ok, parsed = check_maintenance_quiz(
                    app._maintenance_llm_call(model, prompt))
                row.update({"ok": ok, "hit": len(parsed),
                            "expected": len(MAINTENANCE_QUIZ_ANSWER),
                            "error": "" if ok else "提取结果与标准答案不一致"})
            except Exception as exc:
                row.update({"ok": False, "hit": 0,
                            "expected": len(MAINTENANCE_QUIZ_ANSWER),
                            "error": str(exc)[:160]})
            return row

        def run_group(entry: tuple[str, list[str]]) -> list[dict]:
            # The channel name must travel with the group: reading the loop
            # variable here would label every group with the last candidate's
            # channel, because the pool runs after the grouping loop ends.
            channel_name, models = entry
            return [run_quiz(model, channel_name) for model in models]

        groups: dict[str, list[str]] = {}
        for model in candidates:
            channel = app._maintenance_channel_for(model)
            channel_name = channel.name if channel is not None else ""
            groups.setdefault(channel_name, []).append(model)

        with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as executor:
            for rows in executor.map(run_group, groups.items()):
                results.extend(rows)
    results.sort(key=lambda r: (not r["ok"], r["model"]))
    passing = [r["model"] for r in results if r["ok"]]
    with app.maintenance_pool_lock:
        app.maintenance_pool["queue"] = passing
        app.maintenance_pool["results"] = results
        tests = app.maintenance_pool.setdefault("tests", {})
        now = time.time()
        for row in results:
            tests[row["model"]] = {**row, "tested_at": now}
        _trim_tests(tests)
        for row in results:
            if row["channel"] and (row["ok"]
                                   or _failure_marks_dead(row.get("error") or "")):
                _label_locked(app.maintenance_pool, row["channel"], row["model"],
                              row["ok"], row.get("error") or "")
        app.maintenance_pool["verified_at"] = now
        app.maintenance_pool["last_probe_at"] = now
        verified_at = now
        save_maintenance_pool(app)
    return {"queue": list(passing), "results": results,
            "candidates": len(candidates), "verified_at": verified_at}


def _quiz_row(app, channel, model: str) -> dict:
    """Run the 11-id quiz once against (channel, model) and return its row."""
    row = {"model": model, "channel": channel.name, "tested_at": time.time()}
    try:
        ok, parsed = check_maintenance_quiz(app._maintenance_direct_call(
            channel, model, build_plan_models_prompt(MAINTENANCE_QUIZ_DOC)))
        row.update({"ok": ok, "hit": len(parsed),
                    "expected": len(MAINTENANCE_QUIZ_ANSWER),
                    "error": "" if ok else "提取结果与标准答案不一致"})
    except Exception as exc:
        row.update({"ok": False, "hit": 0,
                    "expected": len(MAINTENANCE_QUIZ_ANSWER),
                    "error": str(exc)[:160]})
    return row


def _record_quiz_row(app, row: dict) -> None:
    """Store one quiz outcome in the pool (tests map, dead labels, queue).

    Caller must hold maintenance_pool_lock. A pass clears any dead label and
    joins the verified queue; a failure labels the mapping dead only when it
    is a definitive "this model id is gone" verdict (_failure_marks_dead).
    """
    tests = app.maintenance_pool.setdefault("tests", {})
    tests[row["model"]] = row
    _trim_tests(tests)
    if row["ok"] or _failure_marks_dead(row.get("error") or ""):
        _label_locked(app.maintenance_pool, row["channel"], row["model"],
                      row["ok"], row.get("error") or "")
    queue = app.maintenance_pool.setdefault("queue", [])
    if row["ok"] and row["model"] not in queue:
        queue.append(row["model"])
        queue.sort()


def test_single_model(app, channel_name: str, model: str) -> dict:
    """Manual quiz for one model on one named channel; a pass joins the queue.

    Raises ValueError for unknown channel/model pairs so the console gets a
    400 with a readable message; quiz failures are a normal result, not an
    error.
    """
    channel = next((c for c in app.config.channels if c.name == channel_name),
                   None)
    if channel is None:
        raise ValueError(f"未知渠道：{channel_name}")
    catalog = app.store.catalogs.get(channel.name) or {}
    offered = (set(str(v) for v in channel.models.values())
               | set(str(m) for m in catalog.get("models") or []))
    if model not in offered:
        raise ValueError(f"渠道 {channel_name} 未提供模型 {model}")
    row = _quiz_row(app, channel, model)
    with app.maintenance_pool_lock:
        _record_quiz_row(app, row)
        save_maintenance_pool(app)
    return row


def retest_failed_models(app) -> dict:
    """Quiz every model whose last recorded quiz failed, in one click.

    Pairs come from the pool's own test ledger, so this covers exactly what
    the free-model page shows as 测验未通过 (manual tests and the maintenance
    probe alike). Channels run concurrently but models inside one channel
    strictly serially — a free key answers one request at a time and parallel
    calls come back 429, which would read as the model failing again.
    """
    with app.maintenance_pool_lock:
        failed = [(row.get("channel") or "", row["model"])
                  for row in (app.maintenance_pool.get("tests") or {}).values()
                  if row.get("ok") is False]
    channels = {c.name: c for c in app.config.channels}
    offered: dict[str, set[str]] = {}
    pairs: list[tuple[object, str]] = []
    for channel_name, model in failed:
        channel = channels.get(channel_name)
        if channel is None:
            continue
        if channel_name not in offered:
            catalog = app.store.catalogs.get(channel_name) or {}
            offered[channel_name] = (set(str(v) for v in channel.models.values())
                                     | set(str(m) for m in catalog.get("models") or []))
        if model in offered[channel_name]:
            pairs.append((channel, model))

    groups: dict[str, list[tuple[object, str]]] = {}
    for channel, model in pairs:
        groups.setdefault(channel.name, []).append((channel, model))

    def run_group(entry: tuple[str, list[tuple[object, str]]]) -> list[dict]:
        _name, items = entry
        return [_quiz_row(app, channel, model) for channel, model in items]

    results: list[dict] = []
    if groups:
        with ThreadPoolExecutor(max_workers=PROBE_WORKERS) as executor:
            for rows in executor.map(run_group, groups.items()):
                results.extend(rows)
    results.sort(key=lambda r: (not r["ok"], r["channel"], r["model"]))
    now = time.time()
    with app.maintenance_pool_lock:
        for row in results:
            _record_quiz_row(app, row)
        app.maintenance_pool["verified_at"] = now
        save_maintenance_pool(app)
    return {"retested": len(results),
            "passed": sum(1 for r in results if r["ok"]),
            "failed": sum(1 for r in results if not r["ok"]),
            "results": results,
            "dead_models": dead_models_payload(app)}


FREE_USAGE_DAYS = 30  # window for per-model request/token figures in the console


def _free_quota_sync_note(app, channel) -> str:
    """Warn when vendor quota sync is absent: the local baseline may drift.

    Channels whose vendor has a free-quota command are expected to run the
    scheduled sweep; without it the router still enforces the baseline quota
    locally, but the numbers can differ from the vendor's dashboard.
    """
    mode = app.provider_mode(channel.provider, channel.mode) if hasattr(
        app, "provider_mode") else {}
    if not mode.get("free_quota_cmd"):
        return ""
    reports = getattr(app, "free_align_reports", None) or []
    if not reports:
        return "尚未同步厂家免费额度：当前按基础额度 + 本地用量估算，可能与实际用量不一致"
    latest = reports[-1]
    for alert in latest.get("alerts") or []:
        if (alert.get("channel") == channel.name
                and alert.get("kind") == "free_quota_cmd"):
            return "厂家免费额度同步失败：当前按基础额度 + 本地用量估算，可能与实际用量不一致"
    return ""


def free_models_payload(app) -> dict:
    """Free-tier channels and every free model they could donate to the pool.

    Each (channel, model) row carries its last quiz outcome plus what the
    router sees right now: whether it is actually routable (mapping-based,
    channel enabled/not blocked/not cooling, no dead label), and the
    recorded requests/tokens/cache-hit counters for the recent window.
    Paid-looking mappings on mixed catalogs stay hidden but are counted in
    "excluded"."""
    snapshot = app.store.snapshot()["channels"]
    with app.maintenance_pool_lock:
        tests = dict(app.maintenance_pool.get("tests") or {})
        queue = list(app.maintenance_pool.get("queue") or [])
    dead = dead_model_labels(app)
    blocked = app.blocked_channels()
    usage_by_model: dict[tuple[str, str], dict] = {}
    cache_by_channel: dict[str, dict] = {}
    if getattr(app, "usage", None):
        stats = app.usage.model_stats(FREE_USAGE_DAYS)
        usage_by_model = {(row["channel"], row["model"]): row
                          for row in stats["models"]}
        cache_by_channel = {row["name"]: row for row in stats["cache"]}
    channels = []
    for channel in app.config.channels:
        if channel.tier != "free":
            continue
        vendor = next((p for p in getattr(app, "providers", None) or []
                       if isinstance(p, dict) and p.get("id") == channel.provider),
                      {})
        mode_meta = ((vendor.get("modes") or {}).get(channel.mode) or {})
        state = snapshot.get(channel.name) or {}
        mapped, paid_hidden, nonchat_hidden = _free_mapping_values(app, channel)
        catalog = app.store.catalogs.get(channel.name) or {}
        marked = [str(m) for m in catalog.get("models") or []
                  if str(m).endswith(":free") and is_chat_model(m)]
        entries: list[dict] = []
        for source, models in (("mapping", mapped), ("catalog", marked)):
            for model in models:
                if any(e["model"] == model for e in entries):
                    continue
                test = tests.get(model)
                label = dead.get(_dead_key(channel.name, model))
                usage = usage_by_model.get((channel.name, model))
                quota = app.free_quota_status(channel, model)
                routable = (source == "mapping" and channel.enabled
                            and not label and channel.name not in blocked
                            and not state.get("cooling") and bool(channel.api_key)
                            and not (quota and quota.get("exhausted")))
                if routable:
                    skip = ""
                elif not channel.enabled:
                    skip = "渠道已停用"
                elif channel.name in blocked:
                    skip = str(blocked[channel.name])
                elif label:
                    skip = "测验失败已标记失效，待复验"
                elif quota and quota.get("exhausted"):
                    skip = "免费额度已用完，已停止路由（避免按量计费）"
                elif state.get("cooling"):
                    skip = f"渠道冷却中：{state.get('reason') or ''}".rstrip("：")
                elif not channel.api_key:
                    skip = "未配置密钥"
                else:
                    skip = "仅目录候选，未映射到渠道"
                entries.append({
                    "model": model, "source": source,
                    "in_queue": model in queue,
                    "test": dict(test) if test else None,
                    "dead": dict(label) if label else None,
                    "routable": routable,
                    "skip_reason": skip,
                    "usage": dict(usage) if usage else None,
                    "quota": quota,
                })
        channels.append({
            "name": channel.name,
            "provider": channel.provider,
            "provider_name": str(vendor.get("name") or channel.provider),
            "mode": channel.mode,
            "docs_url": str(mode_meta.get("docs_url") or ""),
            "rate": {"rpm": channel.rate.rpm, "rpd": channel.rate.rpd,
                     "tpm": channel.rate.tpm, "tpd": channel.rate.tpd},
            "free_limits": mode_meta.get("free_limits") or None,
            "enabled": channel.enabled,
            "cooling": bool(state.get("cooling")),
            "cooldown_reason": str(state.get("reason") or ""),
            "has_key": bool(channel.api_key),
            "quota_sync": _free_quota_sync_note(app, channel),
            "excluded": paid_hidden,
            "skipped": nonchat_hidden,
            "cache": cache_by_channel.get(channel.name),
            "models": entries,
        })
    everything = [entry for channel in channels for entry in channel["models"]]
    summary = {
        "channels": len(channels),
        "models": len(everything),
        "routable": sum(1 for entry in everything if entry["routable"]),
        "dead": sum(1 for entry in everything if entry["dead"]),
        "quota_exhausted": sum(1 for entry in everything
                               if (entry["quota"] or {}).get("exhausted")),
        "quota_warning": sum(1 for entry in everything
                             if (entry["quota"] or {}).get("warning")),
        "quota_unsynced": sum(1 for channel in channels
                              if channel["quota_sync"]),
        "rate_limited": sum(
            1 for channel in channels
            if channel["free_limits"] or any(
                (channel["rate"] or {}).get(k) for k in ("rpm", "rpd", "tpm", "tpd"))
            for entry in channel["models"] if entry["routable"]),
        "queue": len(queue),
        "tested": sum(1 for entry in everything if entry["test"]),
        "passed": sum(1 for entry in everything if (entry["test"] or {}).get("ok")),
        "used": sum(1 for entry in everything
                    if (entry["usage"] or {}).get("requests")),
        "tokens": sum(int((entry["usage"] or {}).get("tokens") or 0)
                      for entry in everything),
        "days": FREE_USAGE_DAYS,
    }
    return {"channels": channels, "summary": summary}


def pick_maintenance_models(app) -> tuple[list[str], str]:
    """Ordered LLM candidates for maintenance work: (models, mode).

    mode is "fixed" when defaults.maintenance_model lists explicit ids and
    "pool" when the verified queue drives selection. In pool mode an empty
    queue triggers one probe (bounded by PROBE_COOLDOWN so a fully dead
    free pool cannot turn maintenance into a probe storm). Behind the
    verified queue the pool's untried free candidates are appended: a model
    that fails falls through to the next one instead of dropping the work
    onto the regex fallback while candidates remain.
    """
    raw = str(app.config.defaults.maintenance_model or "").strip()
    if raw and raw.lower() != "auto":
        ids = [m for m in (s.strip() for s in re.split(r"[,;\s]+", raw)) if m]
        return ids, "fixed"
    with app.maintenance_pool_lock:
        queue = list(app.maintenance_pool.get("queue") or [])
        tested = set(app.maintenance_pool.get("tests") or ())
        last_probe = float(app.maintenance_pool.get("last_probe_at") or 0)
    if not queue and time.time() - last_probe >= PROBE_COOLDOWN:
        probe_maintenance_models(app)
        with app.maintenance_pool_lock:
            queue = list(app.maintenance_pool.get("queue") or [])
            tested = set(app.maintenance_pool.get("tests") or ())
    dead = {key.partition("\u0000")[2] for key in dead_model_labels(app)}
    extra = [m for m in free_model_candidates(app)
             if m not in queue and m not in tested and m not in dead]
    return queue + extra, "pool"


def drop_maintenance_model(app, model: str, error: str) -> None:
    """Record a model that just failed real maintenance work as tried-and-failed.

    It leaves the verified queue and its failure joins `tests`, so never-tried
    pool candidates stay ahead of it next time instead of the same dead
    weight being retried on every maintenance run.
    """
    with app.maintenance_pool_lock:
        queue = app.maintenance_pool.get("queue") or []
        if model in queue:
            queue.remove(model)
        message = f"维护调用失败：{str(error)[:140]}"
        tests = app.maintenance_pool.setdefault("tests", {})
        tests[model] = {"model": model, "ok": False, "hit": 0,
                        "expected": len(MAINTENANCE_QUIZ_ANSWER),
                        "error": message, "tested_at": time.time()}
        _trim_tests(tests)
        results = app.maintenance_pool.setdefault("results", [])
        results.insert(0, {
            "model": model, "ok": False, "hit": 0,
            "expected": len(MAINTENANCE_QUIZ_ANSWER),
            "error": message,
        })
        del results[20:]
        save_maintenance_pool(app)

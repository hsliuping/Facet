"""Free-quota alignment: keep free-tier channel mappings in sync with vendors.

Some vendors expose their free-quota state only through a local CLI instead of
an HTTP endpoint (Bailian: ``bl usage free --output json``). A channel serving
such a quota must map exactly the models that still carry free quota — every
unmapped-but-billed model would silently charge per-token, and every expired
model would 4xx at call time. This module is the scheduled sweep (defaults.
free_align_hours, 0 disables): run the vendor command once, keep rows that
speak chat completions (type "Text") with remaining quota above the
provider's safety margin (FREE_RESERVE_TOKENS) and a not-yet-passed expiry
as identity mappings, and replace the channel's concrete model keys.
``auto``/``*`` keys are preserved; command failures keep the old mapping and
land in the report alerts so the next sweep can retry.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import date, datetime

from .ark_quota import resolve_callable_names
from .core import (AUTO_MODEL, FREE_RESERVE_DEFAULT_TOKENS,
                   FREE_RESERVE_TOKENS, resolve_secret)
from .probe import probe_models

# `bl usage free` shells out to the vendor CLI and may do network I/O.
CMD_TIMEOUT = 120.0


def _resolve_cmd(cmd: list[str]) -> list[str]:
    """Resolve the vendor CLI through PATH, wrapping Windows shims.

    npm installs global CLIs as .cmd/.bat shims which CreateProcess refuses
    to exec without a shell; route those through `cmd /c`. Everything else
    (real .exe, POSIX scripts) runs as-is.
    """
    exe = shutil.which(cmd[0])
    if exe and exe.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", exe, *cmd[1:]]
    return cmd


def parse_free_quota_output_rows(text: str) -> list[dict]:
    """Parse a quota tool's ``--output json`` payload into row dicts.

    Accepts a bare array or an object wrapping one ({"data": [...]} etc.);
    anything else yields [] — the caller treats that as "no free quota".
    """
    try:
        data = json.loads(text)
    except ValueError:
        return []
    if isinstance(data, dict):
        data = next((v for v in data.values() if isinstance(v, list)), [])
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict)]


def free_models_from_rows(rows, today: str | None = None,
                          reserve: float = 0.0) -> list[str]:
    """Model ids with usable free chat quota, in the tool's own order.

    A row qualifies when its type is "Text" (vision/embedding/audio rows
    never serve /chat/completions), remaining quota is above ``reserve`` and
    the expiry date has not passed. Rows without a parsable remaining count
    or without an expiry are skipped — quota state must be proven, not
    assumed. ``reserve`` is a per-model margin (FREE_RESERVE_TOKENS): a
    vendor that bills silently once a grant runs dry (Volcengine) keeps the
    model out of the mapping while a single request's worth of tokens is all
    that separates it from zero, so auto is repointed at a model with real
    headroom instead of one the dispatch gate will refuse.
    """
    today = today or date.today().isoformat()
    out: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("type") or "").strip() != "Text":
            continue
        try:
            if float(row.get("remaining") or 0) <= reserve:
                continue
        except (TypeError, ValueError):
            continue
        expires = str(row.get("expires") or "").strip()
        if not expires or expires < today:
            continue
        model = str(row.get("model") or "").strip()
        if model and model not in seen:
            seen.add(model)
            out.append(model)
    return out


def run_vendor_cmd(cmd: list[str], timeout: float = CMD_TIMEOUT,
                   extra_env: dict[str, str] | None = None) -> str:
    """Run a vendor quota CLI and return its stdout; raises RuntimeError.

    `extra_env` values (management credentials configured through the
    editor) override the inherited environment for this child only; an
    empty map keeps the plain inherit-parent behavior.
    """
    env = {**os.environ, **extra_env} if extra_env else None
    try:
        proc = subprocess.run(
            _resolve_cmd(cmd), capture_output=True, timeout=timeout,
            encoding="utf-8", errors="replace", env=env)
    except FileNotFoundError as exc:
        raise RuntimeError(f"命令不存在（{exc.filename}），请先安装厂家 CLI") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"命令超时（>{int(timeout)}s）") from None
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:200]
        raise RuntimeError(f"命令退出码 {proc.returncode}：{detail}")
    return proc.stdout


def align_mapping(channel, models: list[str]) -> dict[str, str]:
    """New mapping for a channel: identity for free models, auto keys kept.

    An ``auto`` target that fell out of the vendor's free list is repointed at
    the first surviving free model: a stale id kept every request on a model
    the vendor now rejects (HTTP 403) or no longer grants, long after the rest
    of the channel still had allowance.
    """
    keep = {k: v for k, v in channel.models.items() if k in (AUTO_MODEL, "*")}
    mapping = {**keep, **{m: m for m in models}}
    auto = mapping.get(AUTO_MODEL)
    if auto and models and auto not in models:
        mapping[AUTO_MODEL] = models[0]
    elif auto and not models:
        # No free model survived: blank the target so auto traffic stops
        # instead of routing to a model that now bills the account balance.
        mapping[AUTO_MODEL] = ""
    return mapping


def _mode_for(app, channel) -> list[str] | None:
    """The channel's registry mode free_quota_cmd, or None when absent."""
    pid = str(getattr(channel, "provider", "") or "")
    mode_key = str(getattr(channel, "mode", "") or "")
    if not pid or not mode_key:
        return None
    provider = next((p for p in getattr(app, "providers", None) or []
                     if isinstance(p, dict) and p.get("id") == pid), None)
    mode = (provider or {}).get("modes", {}).get(mode_key) if provider else None
    cmd = mode.get("free_quota_cmd") if isinstance(mode, dict) else None
    if isinstance(cmd, list) and cmd and all(isinstance(x, str) for x in cmd):
        return cmd
    return None


def _creds_for(app, channel) -> dict[str, str]:
    """Management credentials for the channel: its own first, vendor fallback.

    Uses the new resolve_creds chain: explicit channel override > account
    credentials (when channel.account is set) > vendor-level fallback.
    The channel remains the only runtime identity for routing; an account
    is a convenience object that removes duplicate AK/SK when one vendor
    account has multiple channels (coding/agent/payg).
    """
    from .accounts import resolve_creds
    return resolve_creds(channel, app.config)


def _mode_envs(app, channel) -> list[str]:
    """credential_envs declared by the channel's registry mode (may be [])."""
    pid = str(getattr(channel, "provider", "") or "")
    mode_key = str(getattr(channel, "mode", "") or "")
    if not pid or not mode_key:
        return []
    provider = next((p for p in getattr(app, "providers", None) or []
                     if isinstance(p, dict) and p.get("id") == pid), None)
    mode = (provider or {}).get("modes", {}).get(mode_key) if provider else None
    if not isinstance(mode, dict):
        return []
    out = []
    for entry in mode.get("credential_envs") or []:
        env = str((entry.get("env") if isinstance(entry, dict) else entry)
                  or "").strip()
        if env:
            out.append(env)
    return out


def _missing_envs(envs: list[str], creds: dict[str, str]) -> list[str]:
    """Declared credential envs with no resolvable value anywhere.

    A saved ``***`` placeholder counts as missing (the real secret lives in
    the config, not in this snapshot); ``env:VAR`` references resolve
    against the process environment like the console pull does.
    """
    out = []
    for env in envs:
        value = resolve_secret(str(creds.get(env) or ""))
        if not value or value == "***":
            value = os.environ.get(env, "").strip()
        if not value:
            out.append(env)
    return out


def _resolve_volcengine_callables(channel, families: list[str]) -> list[str]:
    """Map ark free-quota family names to data-plane callable ids.

    ``facet-ark-quota`` reports the management-plane family name (e.g.
    ``kimi-k3-1-pro``); the chat call needs the versioned id the data-plane
    ``/models`` route lists (``kimi-k3-1-pro-261024``). The channel's own
    ark-... key reads that route, so this runs only when such families exist.
    """
    raw = {
        "name": channel.name,
        "protocol": channel.protocol,
        "base_url": channel.base_url,
        "api_key": channel.api_key,
        "models": dict(channel.models) or {"auto": "*"},
        "upstream_path": channel.upstream_path,
        "proxy": getattr(channel, "proxy", ""),
    }
    dp = probe_models(raw)
    if not dp.get("ok"):
        raise RuntimeError(
            f"数据面 /models 解析失败：{dp.get('error') or '未知错误'}")
    return resolve_callable_names(families, dp.get("models") or [])


def _quota_snapshot(rows: list[dict], models: list[str]) -> dict[str, dict]:
    """Vendor-reported quota per routable model id.

    The vendor command names models by family (``kimi-k3-1-pro``) while the
    mapping holds callable ids (``kimi-k3-1-pro-261024``); the sweep resolved
    one into the other, so match by the same prefix rule and keep the
    numbers the vendor actually reported.
    """
    out: dict[str, dict] = {}
    for wire in models:
        for row in rows:
            family = str(row.get("model") or "")
            if not family:
                continue
            tail = wire[len(family):] if wire.startswith(family) else ""
            if wire != family and not (tail.startswith("-") and tail[1:].isdigit()):
                continue
            total, remaining = row.get("total"), row.get("remaining")
            consumed = row.get("consumed")
            if consumed is None and isinstance(total, (int, float)) \
                    and isinstance(remaining, (int, float)):
                # Some CLIs report only total/remaining; keep all three in
                # sync so the console never prints a blank "used" figure.
                consumed = max(0, total - remaining)
            out[wire] = {"remaining": remaining, "total": total,
                         "consumed": consumed, "kind": "token"}
            break
    return out


def run_free_align(app, trigger: str = "manual", _run_cmd=None) -> dict:
    """One sweep over free-tier channels linked to a free_quota_cmd mode.

    The same vendor command is executed once per distinct (command line,
    credentials) pair even when several channels share it. Returns a report
    whose ``patch`` (channel -> new models mapping) the caller applies
    through the normal config pipeline. Never raises; failures become
    alerts and the channel keeps its current mapping until the next sweep.
    ``quota`` carries the vendor's own remaining/total/consumed per model so
    the console can show the real numbers instead of the gateway's counter.
    """
    report: dict = {"trigger": trigger, "aligned": [], "alerts": [],
                    "skipped": [], "patch": {}, "quota": {},
                    "quota_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    runner = _run_cmd or run_vendor_cmd
    results: dict[tuple, list[dict] | None] = {}
    for channel in list(app.config.channels):
        if not channel.enabled or channel.tier != "free":
            continue
        cmd = _mode_for(app, channel)
        if not cmd:
            continue
        creds = _creds_for(app, channel)
        missing = _missing_envs(_mode_envs(app, channel), creds)
        if missing:
            # No management credentials: the channel keeps its current
            # mapping (for TokenHub that is the registry free_models
            # subset applied at pull time). Skip silently — an alert here
            # would nag every sweep for a configuration the user may
            # simply not need.
            report["skipped"].append({"channel": channel.name,
                                      "missing": missing})
            continue
        key = (tuple(cmd), tuple(sorted(creds.items())))
        if key not in results:
            try:
                results[key] = parse_free_quota_output_rows(
                    runner(list(cmd), extra_env=creds))
            except Exception as exc:
                results[key] = None
                report["alerts"].append({
                    "channel": channel.name, "kind": "free_quota_cmd",
                    "message": f"免费额度命令执行失败（{str(exc)[:160]}），保留原映射",
                })
                continue
        if results[key] is None:
            # A sibling channel on the same (cmd, creds) already failed and
            # alerted. Aligning to rows=[] would wipe this channel's mapping
            # (auto -> paid fallback) — the opposite of the promise above.
            report["skipped"].append({
                "channel": channel.name,
                "missing": "共享凭据的免费额度命令失败，保留原映射"})
            continue
        rows = results[key] or []
        # Per-model safety margin: a model sitting inside the reserve is
        # treated as spent here too, or auto would keep pointing at a model
        # the dispatch gate refuses while its siblings still have real
        # headroom. Volcengine holds 100k back (it bills silently); every
        # other provider keeps the shared 10k default — below that a call
        # fails outright anyway and would only trigger another retry.
        reserve = FREE_RESERVE_TOKENS.get(
            str(getattr(channel, "provider", "") or ""),
            FREE_RESERVE_DEFAULT_TOKENS)
        families = free_models_from_rows(rows, reserve=reserve)
        if getattr(channel, "provider", "") == "volcengine" and families:
            try:
                models = _resolve_volcengine_callables(channel, families)
            except Exception as exc:
                report["alerts"].append({
                    "channel": channel.name, "kind": "free_quota_cmd",
                    "message": f"免费额度版本名解析失败（{str(exc)[:160]}），保留原映射",
                })
                continue
        else:
            models = families
        mapping = align_mapping(channel, models)
        changed = mapping != dict(channel.models)
        report["aligned"].append({
            "channel": channel.name, "models": list(models), "changed": changed,
        })
        report["quota"][channel.name] = _quota_snapshot(rows, models)
        if changed:
            report["patch"][channel.name] = mapping
    return report

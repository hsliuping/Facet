"""First start: generate a seed config when none exists.

``facet serve`` with no router.json writes a seed config with a set of
disabled free-tier templates (NVIDIA NIM / Google AI Studio / Z.AI /
SiliconFlow) plus an anthropic placeholder, so the gateway starts
immediately. Traffic is served only after the user registers a free key
and enables a template channel (anonymous upstreams were retired — too
slow and rate-limited).
The seed is a normal, user-editable config file; it is only written when the
path does not exist yet.
"""
from __future__ import annotations

import json
from pathlib import Path


def build_seed() -> dict:
    """The seed config dict; must pass planrouter.core.parse_config."""
    return {
        # First start seeds no management password; the operator is expected
        # to set one when the console opens (the console is open on the
        # loopback address by default).
        "listen": {"host": "127.0.0.1", "port": 8787},
        "defaults": {"maintenance_model": "auto"},
        "channels": [
            {
                "name": "nvidia-nim",
                "protocol": "openai",
                "provider": "nvidia",
                "mode": "payg",
                "base_url": "https://integrate.api.nvidia.com/v1",
                "api_key": "env:NVIDIA_API_KEY",
                "enabled": False,
                "tier": "free",
                "models": {"auto": "nvidia/nemotron-3.5-nano-30b-a3b"},
            },
            {
                "name": "google-ai-studio",
                "protocol": "openai",
                "provider": "google",
                "mode": "payg",
                "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
                "api_key": "env:GOOGLE_AI_STUDIO_API_KEY",
                "enabled": False,
                "tier": "free",
                "models": {"auto": "gemini-3.5-flash-lite"},
            },
            {
                "name": "z-ai-glm",
                "protocol": "openai",
                "provider": "zai",
                "mode": "payg",
                "base_url": "https://api.z.ai/api/paas/v4",
                "api_key": "env:ZAI_API_KEY",
                "enabled": False,
                "tier": "free",
                "models": {"auto": "glm-4.7-flash"},
            },
            {
                "name": "siliconflow",
                "protocol": "openai",
                "provider": "siliconflow",
                "mode": "payg",
                "base_url": "https://api.siliconflow.cn/v1",
                "api_key": "env:SILICONFLOW_API_KEY",
                "enabled": False,
                "tier": "free",
                "models": {"auto": "XingChenAGI/Xing4.0-29B"},
            },
            # One-time allowance packs: vendors with no remaining-quota API
            # (中国电信 teleAI / 国家超算互联网). The ledger's opening balance
            # is what the operator registered after claiming the grant on the
            # vendor console; expiry, when the vendor states one, goes into
            # each pack. Before enabling, re-check the trial model ids and
            # the granted amounts on the vendor console.
            {
                "name": "teleai-xing",
                "protocol": "openai",
                "provider": "teleai",
                "mode": "payg",
                "base_url": "https://ai.ctaigw.cn/v1",
                "api_key": "env:CTYUN_APP_KEY",
                "enabled": False,
                "tier": "free",
                "models": {"auto": "Xing4.0-29B"},
                "model_quota": {"Xing4.0-29B": {"tokens": 100000000}},
            },
            {
                "name": "scnet",
                "protocol": "openai",
                "provider": "scnet",
                "mode": "payg",
                "base_url": "https://api.scnet.cn/api/llm/v1",
                "api_key": "env:SCNET_API_KEY",
                "enabled": False,
                "tier": "free",
                "models": {"auto": "DeepSeek-R1-Distill-Qwen-7B"},
                "model_quota": {"DeepSeek-R1-Distill-Qwen-7B":
                                {"tokens": 10000000}},
            },
            {
                "name": "anthropic-template",
                "protocol": "anthropic",
                "provider": "anthropic",
                "mode": "payg",
                "base_url": "https://api.anthropic.com",
                "api_key": "env:ANTHROPIC_API_KEY",
                "enabled": False,
                "models": {"auto": "claude-haiku-4-5"},
            },
        ],
        "routes": [
            {"name": "default", "protocol": "openai",
             "channels": ["nvidia-nim", "siliconflow"]},
            {"name": "default", "protocol": "anthropic",
             "channels": ["anthropic-template"]},
        ],
        "setup": {"wizard_done": False},
    }


def write_seed(path: str | Path) -> Path:
    """Write the seed config unless the file already exists (idempotent)."""
    path = Path(path)
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(build_seed(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def print_seed_banner(path: str | Path) -> None:
    """First-run guidance, printed right after the listening banner."""
    print(f"  首启引导：已生成种子配置 {path}", flush=True)
    print("  管理密码未设置：首次打开管理控制台会引导您设置（本机回环地址默认开放）", flush=True)
    print("  匿名上游已退役（限速/延迟不实用）：请注册免费 key 并在控制台启用模板渠道，", flush=True)
    print("  或运行「快速开始」向导；免费层模板支持 NVIDIA NIM / Google AI Studio / Z.AI / 硅基流动，", flush=True)
    print("        对应环境变量 NVIDIA_API_KEY / GOOGLE_AI_STUDIO_API_KEY / ZAI_API_KEY / SILICONFLOW_API_KEY", flush=True)
    print("  anthropic 协议客户端暂无可用渠道（协议不转换）：添加 anthropic 渠道后即可使用", flush=True)

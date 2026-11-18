"""facet-siliconflow-quota: report SiliconFlow (硅基流动) free chat models as
freealign JSON rows.

SiliconFlow charges ¥0 for a small set of chat models — calls to them never
debit the account balance (赠金 + 充值), so a free-tier channel's mapping can
follow the vendor's pricing policy instead of an account allowance. The
pricing page (https://cloud.siliconflow.cn/models) carries the 免费 tags but
is HTML and there is no JSON endpoint for the free subset, so the catalog
below is curated here and re-checked on vendor lineup changes:

    facet-siliconflow-quota --output json

stdout is a bare JSON array of ``{"model", "type": "Text", "remaining",
"total", "consumed", "expires"}`` rows — the contract ``planrouter.freealign``
consumes to keep free-tier channel mappings in sync.

No credentials are needed: the free list is policy-level (the same for every
account) and the data-plane key stays on the channel for inference. The
vendor's balance endpoint (``GET /v1/user/info``) was retired on 2026-08-14
(HTTP 410, code 20092) with no announced replacement, so there is no
usage card for this provider: the channel page intentionally offers none.

Free models carry no token allowance — the only cap is account-level RPM —
so rows report a large sentinel remaining/total. The sweep's reserve margin
(FREE_RESERVE_TOKENS) and the gateway's between-sweep budget extrapolation
both need a positive figure that ordinary traffic cannot spend down, and the
console then reads "plenty left" instead of a fabricated grant. ``expires``
is a far horizon for the same reason: free models do not lapse. Depletion at
call time is still surfaced by HTTP status (429/403) and healed by the
gateway's cooldown logic, so the sentinel cannot route requests into billing.

Free endpoints left out on purpose: embeddings (BAAI/bge-*) and speech models
never serve /chat/completions; PaddleOCR-VL-1.5 is chat-compatible but
OCR-specific, so it stays out of the $free default mapping (map it
explicitly on the channel when needed).
"""
from __future__ import annotations

import argparse
import json

# Curated from the official pricing page (对话模型, price 免费). Order is the
# $free preference: the general 29B model first so ``auto`` repoints at it on
# lineup changes, then the 9B reasoning model, then the translation expert.
FREE_MODELS = (
    "XingChenAGI/Xing4.0-29B",
    "THUDM/GLM-Z1-9B-0414",
    "tencent/Hunyuan-MT-7B",
)
# "No token cap" sentinel: above every reserve margin and far beyond what a
# gateway can route between sweeps.
NO_CAP_TOKENS = 1_000_000_000
# Free models do not lapse; the sweep only needs a not-yet-passed horizon.
EXPIRES = "2099-12-31"


def rows_from_models(models=FREE_MODELS) -> list[dict]:
    """freealign rows for the free chat catalog (sentinel allowance)."""
    return [{"model": model, "type": "Text", "remaining": NO_CAP_TOKENS,
             "total": NO_CAP_TOKENS, "consumed": 0, "expires": EXPIRES}
            for model in models]


def main(argv: list[str] | None = None) -> int:
    """CLI: print the free-model rows; the sweep consumes bare JSON arrays."""
    parser = argparse.ArgumentParser(
        prog="facet-siliconflow-quota",
        description="输出硅基流动免费模型的 freealign JSON 行")
    parser.add_argument("--output", choices=("json",), default="json")
    parser.parse_args(argv)
    print(json.dumps(rows_from_models(), ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

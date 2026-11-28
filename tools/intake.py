#!/usr/bin/env python
"""T4.2 投稿入口：把 GitHub issue 投稿写入中心待审队列。

GitHub issue forms 渲染的正文形如：

    ### 厂家

    某某模型

    ### 活动标题

    0 元购 10 亿 tokens

本脚本把正文解析成 dict，连同标题/链接打包成 JSON payload，
POST 到中心 POST /admin/queue（intake token 鉴权，仅能写队列）。

约定：
- CENTER_INTAKE_URL / CENTER_INTAKE_TOKEN 环境变量（未配置 → 退出码 2）
- ISSUE_TITLE / ISSUE_BODY / ISSUE_NUMBER / ISSUE_URL / INTAKE_KIND 环境变量
- 退出码：0 = 已入队；1 = 中心拒绝或不可达；2 = 配置缺失

用法（CI）：
  INTAKE_KIND=activity ISSUE_TITLE=... ISSUE_BODY=@body.md ISSUE_NUMBER=1 \
    ISSUE_URL=... CENTER_INTAKE_URL=... CENTER_INTAKE_TOKEN=... python tools/intake.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

KINDS = ("activity", "announcement", "change")


def parse_issue_body(body: str) -> dict:
    """把 issue forms 正文解析成 {字段名: 值}。"""
    fields: dict[str, str] = {}
    current: str | None = None
    lines: list[str] = []
    for line in body.splitlines():
        if line.startswith("### "):
            if current is not None:
                fields[current] = "\n".join(lines).strip()
            current = line[4:].strip()
            lines = []
        elif current is not None:
            lines.append(line)
    if current is not None:
        fields[current] = "\n".join(lines).strip()
    return fields


def build_payload(issue_title: str, issue_body: str, issue_number: int,
                 issue_url: str) -> dict:
    return {
        "issue": issue_number,
        "title": issue_title,
        "url": issue_url,
        "fields": parse_issue_body(issue_body),
    }


def enqueue(center_url: str, token: str, kind: str, payload: dict) -> tuple[bool, str]:
    req = urllib.request.Request(
        center_url.rstrip("/") + "/admin/queue",
        data=json.dumps({
            "source": "github-issue",
            "kind": kind,
            "payload": json.dumps(payload, ensure_ascii=False),
        }).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Admin-Token": token},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read() or b"{}")
            return resp.status < 300, f"queued id={data.get('id')}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except (OSError, ValueError) as e:
        return False, f"{type(e).__name__}: {e}"


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return selftest()

    url = os.environ.get("CENTER_INTAKE_URL", "")
    token = os.environ.get("CENTER_INTAKE_TOKEN", "")
    kind = os.environ.get("INTAKE_KIND", "")
    title = os.environ.get("ISSUE_TITLE", "")
    number = int(os.environ.get("ISSUE_NUMBER", "0") or 0)
    issue_url = os.environ.get("ISSUE_URL", "")
    body_file = os.environ.get("ISSUE_BODY_FILE", "")
    body = os.environ.get("ISSUE_BODY", "")
    if body_file:
        body = Path(body_file).read_text(encoding="utf-8")

    if not (url and token and kind and title and body and number):
        print("intake: missing config (CENTER_INTAKE_URL/TOKEN, INTAKE_KIND, "
              "ISSUE_TITLE/BODY/NUMBER)", file=sys.stderr)
        return 2
    if kind not in KINDS:
        print(f"intake: unknown INTAKE_KIND {kind!r}", file=sys.stderr)
        return 2

    payload = build_payload(title, body, number, issue_url)
    ok, detail = enqueue(url, token, kind, payload)
    if not ok:
        print(f"intake: enqueue failed: {detail}", file=sys.stderr)
        return 1
    print(f"intake: {kind} issue #{number} queued ({detail})")
    return 0


def selftest() -> int:
    sample = (
        "### 厂家\n\n某某模型\n\n"
        "### 活动标题\n\n0 元购 10 亿 tokens\n\n"
        "### 来源链接\n\nhttps://example.com/offer"
    )
    fields = parse_issue_body(sample)
    assert fields["厂家"] == "某某模型", fields
    assert fields["活动标题"] == "0 元购 10 亿 tokens", fields
    assert fields["来源链接"] == "https://example.com/offer", fields
    payload = build_payload("测试标题", sample, 42, "https://github.com/x/y/issues/42")
    assert payload["issue"] == 42 and payload["title"] == "测试标题"
    assert payload["fields"]["活动标题"] == "0 元购 10 亿 tokens"
    # 空正文 → 空 dict（workflow 端用 400 拒绝，中心仍可入队）
    assert parse_issue_body("") == {}
    assert parse_issue_body("### 只有标题\n\n值") == {"只有标题": "值"}
    print("intake selftest: all passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

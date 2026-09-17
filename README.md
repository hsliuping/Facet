# Facet

**English** | [简体中文](https://github.com/hsliuping/Facet/blob/main/README.zh-CN.md)

**Facet answers one question for your application: "for this task, which model should I use?"**

**Facet 回答你应用里最常见的问题："这个任务该用哪个模型？"**——一张覆盖 217 个 provider、约 2,400 个模型能力/限制/价格的事实表，加一个零依赖的 pip 包（`pip install facet-models`）。

It is not a router. It is a **facts table** — a single JSON file describing
the capabilities, limits, and prices of ~2,400 models across 217 providers —
plus the tools that keep the table fresh and a demo consumer. Your project
reads the table, applies its own business rules, and picks a model.

```
models.dev (primary, 217 providers)   OpenRouter /api/v1/models (secondary)
        │                                        │
        └──────────── weekly sync ───────────────┘
                        │  tools/sync_models_dev.py
                        ▼
registry/model-registry.json   ← the deliverable: one file, < 1 MB, zero deps
        │
        ├── your service (fetch the JSON, filter, decide)
        └── examples/pick_model.py (reference consumer / demo)
```

## Why a table instead of a router?

- Routers encode *someone else's* policy. Your business rules are yours.
- A router is a dependency; a JSON file is data. Fetch it, vendor it, or copy
  the fields you need.
- Facts change weekly (prices, limits, new models). Policy changes daily. We
  ship the part that changes weekly.

## The table

`registry/model-registry.json` — keyed `provider/model_id`, one record per
model identity, deduped across providers (aggregator copies lose to
first-party ones; ties prefer richer records).

| Field | Meaning | Notes |
|---|---|---|
| `provider` | endpoint owner (e.g. `openai`, `volcengine`) | the only mandatory field |
| `aliases` | other names for the same model, incl. aggregator spellings | name matching ignores case, dots, hyphens: `glm-5.3` = `glm5.3` = `GLM5.3` |
| `context_window`, `max_output` | token limits | |
| `tool_call`, `reasoning`, `structured_output`, `attachment` | capability booleans | |
| `modalities` | `{"input": [...], "output": [...]}` | |
| `open_weights` | weights publicly available | |
| `cost` | `{"input_per_mtok", "output_per_mtok", "cache_read_per_mtok"}` | **USD per MTok, always** |
| `quota_tier` | `free_forever` / `free_daily` / `paid` | manual, never auto-filled |
| `release_date`, `last_updated` | `YYYY-MM-DD` or `YYYY-MM` | freshness hints |
| `family` | model family | |
| `quality_hint` | 1-5 subjective prior | the ONLY subjective field, manual |

### Hard rules

1. **Single file < 1 MB.**
2. **ABSENT == null.** Unknown facts are omitted (never `null` in the file,
   never a default). Consumers use `.get()` and treat absence as unknown.
3. **Facts only** — except `quality_hint`, which is clearly marked subjective
   and ignorable.
4. **USD per MTok, always.** Any other currency/unit never enters the table.
5. **Fields are append-only.** Semantics never change within a
   `schema_version`; breaking changes bump it. Consumers ignore unknown
   fields.

The full standard: [schema/model-registry.schema.json](https://github.com/hsliuping/Facet/blob/main/schema/model-registry.schema.json).

## Consume it

Fetch the JSON from this repo (raw URL / CDN) at whatever cadence you like,
then decide with your own rules:

```python
import json, urllib.request

table = json.load(urllib.request.urlopen(
    "https://raw.githubusercontent.com/hsliuping/Facet/main/registry/model-registry.json"))

candidates = [
    (key, rec) for key, rec in table["models"].items()
    if (rec.get("context_window") or 0) >= 200_000          # absent == unknown
    and rec.get("tool_call") is True
    and "image" in (rec.get("modalities") or {}).get("input", [])
    and (rec.get("cost") or {}).get("output_per_mtok", 1e9) <= 2.0
]
```

Or try the demo (stdlib only):

```bash
python examples/pick_model.py --min-context 200000 --tools --image-input --limit 5
python examples/pick_model.py claude-sonnet-4.5      # resolve one model + aliases
```

## Install (pip)

The same read → resolve → find logic ships as a tiny package with **zero
runtime dependencies** (stdlib only):

```bash
pip install facet-models
```

```python
import facet

t = facet.load()                      # bundled snapshot (package version = table date)
t = facet.load("path/to/table.json")  # explicit local path
t = facet.load(refresh=True)          # fetch the latest table (see below)

facet.resolve(t, "glm-5.3")           # -> "alibaba-cn/glm-5.3"
facet.find(t, tool_call=True, min_context=200_000, image_input=True,
           max_output_price=2.0, provider="zhipuai", limit=10)
```

- The wheel bundles a snapshot of the table; **the package version is the
  snapshot date** (`2026.9.0` = the September 2026 table). Install and use
  offline; upgrade the package or `load(refresh=True)` for freshness.
- Refresh URL resolution: `url=` argument → `FACET_TABLE_URL` env var →
  built-in default (the raw table on this repo's main). Failures raise —
  never a silent fallback to stale facts.
- Fetching the JSON directly (below) stays fully supported — the package is a
  convenience, not a lock-in.

CLI: `facet "glm-5.3"`, `facet --tools --min-context 200000 --limit 5`.

### Integration contract: three names, three layers

A model carries three different names across your stack. Keep them apart:

| Name | Example | Lives in |
|---|---|---|
| facet key | `zhipuai/glm-5.3` | your scheduler: selection, logs, quota, audit |
| wire name | `rec.model_id` (request body `model`) | the access layer |
| gateway name | whatever your one-api/new-api deployment lists in `/v1/models` | derived at startup, never configured |

The table record **is the handoff**: pass (key, provider, model_id, aliases)
from the scheduler to the access layer. The access layer picks the adapter and
credentials by `provider`, and `model_id` goes into the request body.

Gateway channels need **no mapping config** — the gateway already knows its
own names (`GET /v1/models`); derive the mapping at startup with the resolver
you already have (it tolerates spelling drift by design):

```python
names = [m["id"] for m in get(f"{GW_URL}/v1/models").json()["data"]]
wire = {name: facet.resolve(t, name) for name in names}   # gateway name -> facet key
```

The one convention that makes this work: keep the vendor's original model
names on the gateway (one-api/new-api do by default). A name that resolves to
`None` was invented by the gateway admin — fix it on the gateway side, not
with config.

## Maintain it

```bash
python tools/sync_models_dev.py            # models.dev -> table (weekly, or on demand)
python tools/validate_registry.py          # quality gate (size, nulls, shapes, counts)
python -m unittest discover -s tests       # unit tests
```

A GitHub Action (`sync.yml`) refreshes the table weekly and opens a PR.
Manual annotations (`quota_tier`, `quality_hint`) survive refreshes
automatically. Sources: [models.dev](https://models.dev) (primary, widest
provider coverage) + [OpenRouter](https://openrouter.ai/api/v1/models)
(secondary, fills capability gaps — notably `structured_output`). Capability
fields may be borrowed across channels of the same model (fill-only, never
overwritten); prices are never borrowed since channel pricing differs. No
benchmark scores — those are judgments, not facts; add your own via
`quality_hint`.

## Verify claims (optional)

Everything in the table is an upstream *claim*, never a measurement.
`tools/verify_claims.py` calls a model for real and checks whether its declared
capabilities hold — tool calling, structured output, reasoning traces, and
image input. Results land in `reports/verify-results.json` (match / mismatch /
discovery / inconclusive) and are **never written back to the table**: the
registry stays a pure claims aggregation, and humans decide what to do with
contradictions.

```bash
python tools/verify_claims.py gpt-5 --dry-run        # plan only, no network
python tools/verify_claims.py gpt-5                  # resolve by name or alias
python tools/verify_claims.py --provider zhipuai     # cheapest models first
python tools/verify_claims.py --base-url http://localhost:1234/v1 --api-key ... local-model
```

The table keeps ONE record per model identity (the dedup winner), but the
same model is often served by several vendors — and capability differs per
channel. Qualify the vendor to pin the endpoint: `volcengine/glm-5.3`,
`zhipuai/glm-5.3` and `alibaba-cn/glm-5.3` test the same identity at
different vendors' endpoints (declared facts come from the table record and
are traced via `table_key` in the report). A bare name tests the winner's
channel.

Bring your own API key (environment variables — `OPENAI_API_KEY`,
`ZHIPU_API_KEY`, `ARK_API_KEY`, ...; or `--api-key`). Keys are never stored in
reports. First-party providers are built in; anything else takes
`--base-url --protocol`. Probes cost a few hundred tokens per model;
`--max-models` (default 20) caps `--provider` scans. Found a mismatch? Open an
issue with the report attached, or fix the row via `manual-overrides.json`.

## Non-goals

No routing, no API keys, no telemetry, no per-request ranking service, no
benchmark aggregation. If you need those, build them on the table.

## Governance

MIT licensed. Contributions under the CLA in [CLA.md](https://github.com/hsliuping/Facet/blob/main/CLA.md). Process in
[CONTRIBUTING.md](https://github.com/hsliuping/Facet/blob/main/CONTRIBUTING.md).

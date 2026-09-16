# Facet

**Facet answers one question for your application: "for this task, which model should I use?"**

It is not a router. It is a **facts table** — a single JSON file describing
the capabilities, limits, and prices of ~2,400 models across 217 providers —
plus the tools that keep the table fresh and a demo consumer. Your project
reads the table, applies its own business rules, and picks a model.

```
models.dev (upstream facts)
        │  weekly sync (tools/sync_models_dev.py)
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

The full standard: [schema/model-registry.schema.json](schema/model-registry.schema.json).

## Consume it

Fetch the JSON from this repo (raw URL / CDN) at whatever cadence you like,
then decide with your own rules:

```python
import json, urllib.request

table = json.load(urllib.request.urlopen(
    "https://raw.githubusercontent.com/<org>/Facet/main/registry/model-registry.json"))

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

## Maintain it

```bash
python tools/sync_models_dev.py            # models.dev -> table (weekly, or on demand)
python tools/validate_registry.py          # quality gate (size, nulls, shapes, counts)
python -m unittest discover -s tests       # 26 unit tests
```

A GitHub Action (`sync.yml`) refreshes the table weekly and opens a PR.
Manual annotations (`quota_tier`, `quality_hint`) survive refreshes
automatically. Data source: [models.dev](https://models.dev) (no benchmark
scores — those are judgments, not facts; add your own via `quality_hint`).

## Non-goals

No routing, no API keys, no telemetry, no per-request ranking service, no
benchmark aggregation. If you need those, build them on the table.

## Governance

MIT licensed. Contributions under the CLA in [CLA.md](CLA.md). Process in
[CONTRIBUTING.md](CONTRIBUTING.md). 中文文档：[README.zh-CN.md](README.zh-CN.md).

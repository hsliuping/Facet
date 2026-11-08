# Facet

**English** | [简体中文](https://github.com/hsliuping/Facet/blob/main/README.zh-CN.md)

**Facet is a self-maintaining LLM gateway over free-tier quotas — with a model facts table as its data layer.**

**Facet 回答两个问题："这个任务该用哪个模型？"（事实表）与"这个模型现在能免费跑吗？"（自维护网关）。**

`facet serve` starts a local OpenAI-compatible gateway that aggregates free-tier
quotas from major vendors and keeps its channel directory fresh on its own —
probes, repairs, reports; an AI SRE for your LLM infrastructure. Alongside it
ships the **facts table** it is named after: one JSON file describing the
capabilities, limits, and prices of ~2,400 models across 217 providers
(`pip install facet-models`, zero dependencies). Your project reads the table,
applies its own business rules, and picks a model.

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

## One wheel, two layers

The package ships both layers; there is one command family.

| Layer | Owns | Lives in |
|---|---|---|
| **Facts** (data) | per-model capabilities, prices, `quota_tier`, aliases | `registry/model-registry.json` → bundled snapshot |
| **Gateway** (runtime) | channels, routes, the free pool, daily self-maintenance | `src/planrouter/` |

The bridge between them:

- **Name resolution** — `planrouter/facts.py` maps well-known names to
  canonical keys, read-only, stdlib-only, and degrades to "unavailable" if
  the snapshot is missing. The gateway's *routing* still runs purely on what
  its maintenance layer observes; routing never depends on table freshness.
- **`quota_tier`** — manual annotations (`registry/manual-overrides.json`)
  seed onboarding (`facet --free`). Zero-key start is built in: `facet serve`
  with no `router.json` auto-generates a seed config (anonymous Pollinations
  plus disabled free-tier templates) and starts serving. Maintenance
  probe results only ever *suggest* updates to this file; the sync never
  auto-fills it.

```
facet "glm-5.3"            # facts: resolve + show one model
facet --free --tools ...   # facts: filter the table
facet serve                # gateway: start the self-maintaining proxy
planrouter                 # deprecated alias of `facet serve`
```

## Why a facts table under the gateway?

- The gateway's routing policy is ours and visible; your selection rules are
  still yours. A JSON file is data — fetch it, vendor it, or copy the fields
  you need.
- Facts change weekly (prices, limits, new models). Policy changes daily. We
  ship the part that changes weekly; the gateway adapts on its own.
- The same table powers name resolution — what `gpt-4o` actually resolves to
  among today's free tiers. `quota_tier` is the join key between layers.

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
| `quota_tier` | `free_forever` / `free_daily` / `paid` | manual, never auto-filled (major free-tier vendors pre-seeded) |
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

## The gateway

The gateway layer (`src/planrouter/`) is the runtime half of the package:
`facet serve` aggregates channels — upstream vendor endpoints you configure —
routes OpenAI- and Anthropic-compatible traffic across them, and maintains
itself daily. The console and the `/api/*` endpoints ship on the same port.
Five things worth knowing when you run it:

### Self-maintenance

Runs ~2 minutes after startup, then every `defaults.maintenance_hours`
(default 24; `0` disables). One run: refresh channel catalogs → auto-apply
catalog-confirmed fixes (config backed up first) → refresh subscription plan
models and the vendor registry → ask the maintenance LLM for advisory notes →
record a report. Every finding lands in exactly one of three buckets:

| Bucket | Meaning | Who acts |
|---|---|---|
| `applied` | catalog-confirmed (models removed upstream, `auto` repointed, plan/vendor model lists refreshed) | nobody — already applied, with a config backup |
| `suggestions` | worth doing, not provable from the catalog alone (new upstream models, repeated quota cooldowns, advisory LLM notes — never auto-applied) | you — one click in the console's 自动运维 ("auto-ops") card |
| `alerts` | dead keys, empty catalogs, fetch failures | you, off-band |

Reports are kept in memory (the last 20) and appended to `maintenance.jsonl`
next to `config.json`, so history survives restarts. The console's 维护日志
("maintenance log") tab reads both; the same data is plain HTTP:

```bash
GET  /api/maintenance              # current state: enabled, hours, model pool, last run
GET  /api/maintenance/logs/50      # run history, newest first (default 50)
POST /api/maintenance/run          # force a run now
POST /api/maintenance/test-models  # re-quiz the free pool, rebuild the queue
POST /api/maintenance/apply        # adopt a suggestion ({"channel", "models"})
```

### The free pool

With `defaults.maintenance_model` empty or `auto`, every maintenance LLM call
runs on a verified queue of free models instead of a pinned id:

- Only channels explicitly marked `tier: "free"` donate candidates — probing
  never burns paid quota.
- A catalog that mixes `:free`-suffixed and unsuffixed ids (OpenRouter style)
  donates only its `:free` subset; a catalog without any `:free` id belongs to
  a wholly-free channel, so all of its models are candidates. Before the first
  catalog fetch, the channel's own model mapping stands in.
- Candidates are quizzed against a synthetic doc page with a known answer;
  perfect scorers join the queue. The queue persists in
  `maintenance_models.json` next to `config.json`, is re-verified on every
  probe, and a model that fails real maintenance work is dropped on the spot.
  Probes are cooldown-bounded so a fully dead free pool cannot turn
  maintenance into a probe storm.

Explicit ids in `defaults.maintenance_model` (comma/whitespace separated)
bypass the pool and are used verbatim.

### Maintenance calls go direct, not through routes

A maintenance model id is an upstream id, so it is addressed the way the
upstream would be: straight to the first enabled channel whose own mapping
exposes it (tier-`free` channels first, config order otherwise), as one
non-streaming turn — Anthropic channels get `x-api-key` +
`anthropic-version: 2023-06-01`, OpenAI-compatible ones get `Bearer`, and the
channel's `proxy` applies. Routes are never consulted; maintenance must not
depend on route membership. Only ids no channel exposes (branded names such
as `facet-auto`) fall back to a loopback call through the gateway itself —
ordinary routing, normal failover.

### Outbound proxy

A channel may carry `proxy: "http://..."` (a plain URL or a secret
reference). Every upstream request made on that channel's behalf — chat
traffic and maintenance direct calls alike — goes through it; without it,
connections are direct.

### Adding a vendor

Two layers, two registries:

| Layer | Registry | How to extend |
|---|---|---|
| Gateway | builtin `src/planrouter/providers.json` + user overlay `<config dir>/providers.json` | PR a builtin entry, or drop an overlay entry locally |
| Facts | `registry/model-registry.json` | vendors arrive via `tools/sync_models_dev.py`; manual facts (e.g. `quota_tier`) via `registry/manual-overrides.json` |

A gateway vendor entry needs `id` (`[a-z0-9-]+`), `name`, and at least one
mode — `coding`, `agent`, or `payg` — each with `label`, `protocol`
(`openai` / `anthropic`), `base_url`, and optional `tier` (`free` / `paid`),
`docs_url`, `models_url`, `models`. Overlay entries replace builtin entries
whole, by id. Maintenance refreshes model lists daily: modes with `docs_url`
are re-extracted (LLM first, deterministic list-line rules as fallback),
`models_url` is read as a plain JSON catalog — changes persist into your
overlay with a backup. On the facts side, tiers stay manual: `quota_tier` is
never auto-filled.

## Non-goals

The table does no routing, holds no API keys, collects no telemetry, performs
no per-request ranking, and aggregates no benchmarks — if you need those,
build them on the table. The gateway layer does route, but only on what its
maintenance layer observes (see [The gateway](#the-gateway)); it never
depends on table freshness.

## Governance

MIT licensed. Contributions under the CLA in [CLA.md](https://github.com/hsliuping/Facet/blob/main/CLA.md). Process in
[CONTRIBUTING.md](https://github.com/hsliuping/Facet/blob/main/CONTRIBUTING.md).

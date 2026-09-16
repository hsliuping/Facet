# Contributing to Facet

Facet is deliberately tiny: a JSON table, two stdlib-only tools, one demo,
tests, CI. Keep it that way.

## Setup

Python 3.10+. No dependencies to install.

```bash
python -m unittest discover -s tests   # run tests
python tools/validate_registry.py      # check the committed table
python tools/sync_models_dev.py        # refresh from models.dev (network)
```

## Repo layout

```
schema/model-registry.schema.json   field standard (JSON Schema 2020-12)
registry/model-registry.json        the table (generated, single file < 1 MB)
tools/sync_models_dev.py            fetch -> normalize -> dedup -> emit
tools/validate_registry.py          quality gate for the table
examples/pick_model.py              reference consumer (read -> filter -> rank)
tests/                              stdlib unittest
```

## The hard rules ( enforced by tools/validate_registry.py )

1. Single file < 1 MB.
2. ABSENT field == null (unknown). Never fill a missing value with a default,
   never write a literal `null` — omit the key.
3. Facts only, except `quality_hint` (the single subjective field).
4. Cost is USD per million tokens, nothing else.
5. Fields are only ever added, never re-purposed; breaking changes bump
   `schema_version`.
6. Consumers ignore unknown fields.

## Manual annotations

`quota_tier` (free tier info) and `quality_hint` (1-5 subjective prior) are
maintained by hand on the winning record in `registry/model-registry.json`.
`tools/sync_models_dev.py` preserves them across weekly refreshes — edit the
JSON directly and open a PR. Sync never writes these fields.

## Pull requests

- Run the tests and the validator before pushing.
- Changes to the sync pipeline need a test covering the changed rule.
- Regenerate the table only with the sync tool; do not hand-edit synced
  fields (manual-annotation fields above are the exception).
- Sign off commits (`git commit -s`) to accept the CLA (see CLA.md).

## Sync cadence

The `sync` workflow refreshes the table weekly from models.dev and opens a
PR. Reviewer focus: winner flips across providers, big price moves, new
models.

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

Package development (the `facet-models` pip package, stdlib only — thin fact
client, integration contract documented in the README):

```bash
python tools/build_package.py --skip-build   # snapshot table + derive version
pip install -e .                             # editable install
facet "glm-5.3"                              # CLI smoke test
```

## Repo layout

```
schema/model-registry.schema.json   field standard (JSON Schema 2020-12)
registry/model-registry.json        the table (generated, single file < 1 MB)
registry/manual-overrides.json      manual additions & corrections (survive sync)
tools/sync_models_dev.py            fetch -> normalize -> dedup -> emit
tools/validate_registry.py          quality gate for the table
tools/verify_claims.py              live-verify declared capabilities (optional)
tools/build_package.py              validate -> snapshot -> version -> build
src/facet/                          the pip package (load / resolve / find)
examples/pick_model.py              reference consumer (read -> filter -> rank)
tests/                              stdlib unittest
reports/                            verify_claims output (local, gitignored)
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

## Manual data

Two mechanisms, both applied after the upstream sync and never clobbered
by weekly refreshes:

1. **Annotations** — `quota_tier` (free tier info) and `quality_hint`
   (1-5 subjective prior) on the winning record in
   `registry/model-registry.json`. Edit directly and open a PR; sync
   preserves them automatically.
2. **Additions & corrections** — `registry/manual-overrides.json`:
   - vendors the upstream sources ignore (Baidu ERNIE, InternLM, Kunlun...)
     get full manual entries
   - verified corrections to synced fields (manual value wins)
   - every value must be checked against the vendor's official
     docs/console before committing — facts only, USD per MTok, absent
     == unknown.

Live verification results (`tools/verify_claims.py`) never flow into the
table automatically: attach the report to an issue or PR, and after human
review apply corrections through `manual-overrides.json`.

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

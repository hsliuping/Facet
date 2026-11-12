Plan: Stable Channel ID for Rename-Safe Subscription Linkage + Remove Vendor Concept from Subscriptions

Context:
- User Issue 1: Subscription channels are linked by name. Renaming a channel breaks the link. Need stable channel id so rename is safe; the linked id stays valid as long as the channel record exists.
- User Issue 2: Subscription's "vendor" (厂家) label is wrong; it should be the subscription/套餐名称. Vendor should be derived from the linked channel's provider label, not stored. Remove vendor as a required subscription concept.

Problem:
- Channels have only name, no stable id; every reference (routes, scores, sticky, subscription linkage) keyed by name.
- Subscriptions store vendor and plan as separate required fields; vendor values are composite junk (e.g. "火山引擎_0241_codingplan").
- _plan_creds_for and report modules derive vendor/plan/kind from vendor text strings, tightly coupling to this text format.

Solution:
1. Introduce a stable `id` field on channels (uuid4, generated on first load if absent, persisted in router.json). All channel references that previously keyed by name will, at the subscription linkage layer, first attempt id resolution, fall back to legacy name resolution for backward compat. Rename operation preserves channel.id, so renaming the name field does not break linked subscriptions.

2. Remove `vendor` from subscription normalize requirements; keep `plan` as the subscription/套餐名称. The displayed vendor in UI is derived from the linked channel(s) `provider` label (e.g. "火山引擎", "阿里云百炼"). If no channel linked or provider absent, display "—". _plan_creds_for now derives Volcengine/agent/coding from channel.provider + channel.mode + business_line rather than vendor text substrings.

3. Subscriptions loaded from legacy data (name-based channels) are auto-migrated on first startup: match each subscription channel name → channel name → id; persist the new id; henceforth the subscription stores channel ids.

Files to modify (critical path, not exhaustive):
- src/planrouter/core.py: Channel id field; parse_config auto-generate; merge_secrets id-first.
- src/planrouter/subscriptions.py: normalize vendor optional; helper _resolve_channel(ref) -> Channel; plan kept as primary name.
- src/planrouter/server.py: channel_tier/model_tier/id-aware; _plan_creds_for from channels.provider; subscription load-time name→id migration; blocked_channels/id-aware.
- src/planrouter/static/app.js: subscription editor remove vendor field; channel picker id-value+name display; table vendor column derived from channels provider; banner/report derived vendor.
- src/planrouter/static/index.html: updated sub-note text about score decay / tie-breaking (already completed in prior work; unchanged here).
- data/subscriptions.json: one-time migration executed by server on startup.

Approach rationale:
- Minimal scope: only touches subscription linkage + vendor removal. Existing channel name-based references continue to work via name→id fallback, so no breakage.
- Rename-safe by construction: going forward, channel.id persists across renames; old subscriptions with names still resolve.
- Preserves backward compatibility: all existing data continues to work without migration until edited.

Verification steps after implementation:
1. Run pytest suite (PYTHONPATH=src) — all 718 tests pass.
2. Start network gateway (facet serve), load config + subscriptions.
3. Verify: new channel added via editor gets an id; renaming the channel name does NOT break existing subscription linkage.
4. Verify: subscription table shows plan name as primary, vendor derived from channel provider (e.g. 火山引擎).
5. Verify: _plan_creds_for derives agent vs coding from channel mode/provider, not vendor text.
6. Legacy subscription data: first-run migration produces updated records with channel ids.
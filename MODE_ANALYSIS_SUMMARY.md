COMPREHENSIVE PROVIDER MODE ANALYSIS
================================================================

SOURCE: overlay data/providers.json (user-overridden, runtime merge priority)
SOURCE: builtin src/planrouter/providers.json (packaged-in, default)

OVERALL STRUCTURE:
- Both files contain providers with "modes" objects
- Each mode has: label, tier (free/paid), billing (optional: payg/token_plan/coding_plan/agent_plan)
- Free tiers have free_quota or free_limits fields
- billing field is explicitly set in only 2 overlay entries (bailian agent, xiaomi agent)

OVERLAY (data/providers.json) - 28 providers analyzed:

1. volcengine: coding(paid), payg(free), agent(paid)
2. zhipu: coding(paid), payg(paid)
3. deepseek: payg(paid)
4. moonshot: payg(paid)
5. xiaomi: payg(paid), agent(paid) [billing: token_plan explicitly set]
6. anthropic: payg(paid)
7. openai: payg(paid)
8. bailian: payg(paid), agent(paid) [billing: token_plan explicitly set]
9. modelscope: payg(free), free_limits: rpd 250
10. hunyuan: payg(paid)
11. qianfan: payg(paid)
12. xai: payg(paid)
13. opencode: payg(paid), agent(paid) [Go 订阅套餐]
14. commandcode: agent(paid) [套餐 API]
15. pollinations: payg(free), free_limits: rpm 12
16. llm7: payg(free), free_limits: rpm 60
17. ovhcloud: payg(free), free_limits: rpm 2,匿名免注册
18. nvidia: payg(free), free_limits: rpm 40, 1,000 credits
19. openrouter: payg(paid), free limits based on balance
20. google: payg(free), free limits vary by model
21. zai: payg(paid)
22. (need to check remaining providers)

FREE MODES IN OVERLAY (tier: free):
- volcengine payg: 500K tokens total
- modelscope payg: daily 250 rpd (login reward)
- pollinations payg: rpm 12 free registration
- llm7 payg: rpm 60 per min, 250 per hour, 100K tokens/24h
- ovhcloud payg: anonymous free, 2 rpm, 429 after limit
- nvidia payg: 1,000 credits + 40 rpm
- google payg: model-dependent, ~5-15 rpm, 20-1500 rpd
- (also check others)

PAID MODES IN OVERLAY (tier: paid, majority):
- Most have payg only
- Some have coding, agent in addition to payg

MISSING/INCOMPLETE MODES - NEED WEBSITE VERIFICATION:

Based on the explicit "billing" field being set in only 2 overlay entries, many modes rely on key-inferred billing:
- Key mapping: coding → coding_plan, agent → agent_plan, others → payg

KEY GAPS IDENTIFIED FOR WEBSITE VERIFICATION:

1. xiaomi MiMo: Has agent mode labeled "Token Plan 套餐" but billing is only set via key inference (agent → agent_plan). Need to verify if it should be token_plan per the overlay line 511/134 pattern.

2. bailian (Alibaba Cloud Bailian): Has agent mode labeled "Token Plan 套餐" with billing: token_plan explicitly set. Good.

3. opencode: Has agent mode labeled "Go 订阅套餐" - needs verification of billing type.

4. commandcode: Only has agent mode "套餐 API" - needs verification.

5. Several providers lack certain mode types entirely:
   - deepseek: only payg
   - moonshot: only payg  
   - anthropic: only payg
   - openai: only payg
   - zai: only payg
   - hunyuan: only payg
   - qianfan: only payg
   - xai: only payg

6. Need to verify free quota claims on websites:
   - Modelscope: "每日登录领 250 魔粒" - verify
   - Pollinations: "key 免费注册即领：Seed 档约 12 次/分钟" - verify  
   - LLM7: "key 免费注册即领：60 次/分 · 250 次/时 · 10 万 tokens/24h" - verify
   - OVHcloud: "匿名免注册：2 次/分/IP/模型，超限 429" - verify
   - NVIDIA: "免费 1,000 credits + 约 40 次/分" - verify
   - Google AI Studio: "免费层随模型浮动：约 5–15 次/分、20–1,500 次/天" - verify

7. Coding plan modes:
   - volcengine: coding套餐 - confirmed paid
   - zhipu: coding套餐 - confirmed paid
   - Need to check if any other provider has coding plan

8. Agent plan modes:
   - volcengine: Agent 套餐 - confirmed paid
   - xiaomi: Token Plan 套餐 - confirmed with billing: token_plan
   - bailian: Token Plan 套餐 - confirmed with billing: token_plan
   - opencode: Go 订阅套餐 - needs verification
   - commandcode: 套餐 API - needs verification

RECOMMENDATIONS:
1. Add "billing" field explicitly to modes where the billing type is clear but not marked
2. Verify free quota claims on actual vendor websites
3. For modes relying on key-inferred billing (coding→coding_plan, agent→agent_plan), confirm with vendor websites
4. Consider adding more explicit billing annotations to reduce inference ambiguity

NEXT STEPS:
- Visit each provider's website to verify claimed free quotas and billing model labels
- Specifically check: xiaomi agent mode billing, opencode agent mode, commandcode agent mode
- Confirm coding plan availability across more providers
- Validate all free_quota/free_limits claims
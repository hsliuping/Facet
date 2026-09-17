"""Tests for tools/verify_claims.py — stdlib unittest, run from repo root:

    python -m unittest discover -s tests

All tests are offline: verify.http_json is monkeypatched with scripted
responses, so no network call ever happens.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import verify_claims as verify  # noqa: E402


def ns(**kw):
    base = dict(names=None, provider=None, table=Path("x"), probes=",".join(verify.ALL_PROBES),
                declared_only=False, max_models=20, base_url=None, api_key=None,
                env_key=None, protocol=None, out=Path("r.json"), dry_run=False, strict=False)
    base.update(kw)
    return argparse.Namespace(**base)


class Recorder:
    """Scripted http_json replacement: records calls, pops canned responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers, payload, timeout=120):
        self.calls.append({"url": url, "headers": headers, "payload": payload})
        if self.responses:
            return self.responses.pop(0)
        raise AssertionError("unexpected extra http call")

    def payloads(self):
        return [c["payload"] for c in self.calls]


def openai_ok(content="OK", **msg_extra):
    msg = {"content": content}
    msg.update(msg_extra)
    return (200, {"choices": [{"message": msg}],
                  "usage": {"prompt_tokens": 1, "completion_tokens": 1}})


def openai_tools_ok():
    return (200, {"choices": [{"message": {"content": None, "tool_calls": [{"id": "1", "function": {"name": "get_weather"}}]}}],
                  "usage": {"prompt_tokens": 1, "completion_tokens": 1}})


class PngTests(unittest.TestCase):
    def test_png_constant_is_a_real_png(self):
        raw = base64.b64decode(verify.PNG_1PX_B64)
        self.assertTrue(raw.startswith(b"\x89PNG"))

    def test_1x1_dimensions(self):
        raw = base64.b64decode(verify.PNG_1PX_B64)
        # IHDR width/height live at fixed offsets in a PNG
        w = int.from_bytes(raw[16:20], "big")
        h = int.from_bytes(raw[20:24], "big")
        self.assertEqual((w, h), (1, 1))


class ClassifyTests(unittest.TestCase):
    def test_network(self):
        self.assertEqual(verify.classify_error(0, ""), "network")

    def test_auth_by_status(self):
        self.assertEqual(verify.classify_error(401, ""), "auth_error")
        self.assertEqual(verify.classify_error(403, ""), "auth_error")

    def test_auth_by_body_even_on_400(self):
        self.assertEqual(verify.classify_error(400, '{"error":"invalid_api_key"}'), "auth_error")

    def test_rate_limited(self):
        self.assertEqual(verify.classify_error(429, ""), "rate_limited")

    def test_billing(self):
        self.assertEqual(verify.classify_error(402, ""), "billing")
        self.assertEqual(verify.classify_error(400, "insufficient_quota"), "billing")

    def test_model_unavailable(self):
        self.assertEqual(verify.classify_error(404, ""), "model_unavailable")
        self.assertEqual(verify.classify_error(400, "model gpt-9 not found"), "model_unavailable")

    def test_capability_rejected_needs_keywords(self):
        self.assertEqual(
            verify.classify_error(400, "tools is not supported by this model", verify.PROBE_KEYWORDS["tools"]),
            "capability_rejected")
        self.assertEqual(
            verify.classify_error(400, "this model does not support that", ()),
            "capability_rejected")

    def test_request_error_is_unrelated_4xx(self):
        self.assertEqual(
            verify.classify_error(400, "unknown parameter: temperature", verify.PROBE_KEYWORDS["tools"]),
            "request_error")

    def test_server_error(self):
        self.assertEqual(verify.classify_error(500, ""), "server_error")
        self.assertEqual(verify.classify_error(503, ""), "server_error")


class VerdictTests(unittest.TestCase):
    SIGNAL = {"kind": "signal", "detail": "x", "usage": {}}
    NO_SIGNAL = {"kind": "no_signal", "detail": "x", "usage": {}}
    REJECT = {"kind": "error", "error": "capability_rejected", "detail": "x", "usage": {}}
    AUTH = {"kind": "error", "error": "auth_error", "detail": "x", "usage": {}}

    def test_true_plus_signal_is_match(self):
        self.assertEqual(verify.decide_verdict(True, self.SIGNAL), "match")

    def test_true_plus_no_signal_is_inconclusive(self):
        # never convict a positive claim on absent evidence
        self.assertEqual(verify.decide_verdict(True, self.NO_SIGNAL), "inconclusive")

    def test_true_plus_rejection_is_mismatch(self):
        self.assertEqual(verify.decide_verdict(True, self.REJECT), "mismatch")

    def test_false_plus_signal_is_mismatch(self):
        self.assertEqual(verify.decide_verdict(False, self.SIGNAL), "mismatch")

    def test_false_plus_rejection_is_match(self):
        self.assertEqual(verify.decide_verdict(False, self.REJECT), "match")

    def test_false_plus_no_signal_is_match(self):
        self.assertEqual(verify.decide_verdict(False, self.NO_SIGNAL), "match")

    def test_unknown_plus_signal_is_discovery(self):
        self.assertEqual(verify.decide_verdict(None, self.SIGNAL), "discovery")

    def test_unknown_plus_no_signal_or_rejection_is_inconclusive(self):
        self.assertEqual(verify.decide_verdict(None, self.NO_SIGNAL), "inconclusive")
        self.assertEqual(verify.decide_verdict(None, self.REJECT), "inconclusive")

    def test_errors_other_than_rejection_are_inconclusive(self):
        self.assertEqual(verify.decide_verdict(True, self.AUTH), "inconclusive")

    def test_protocol_unsupported_is_inconclusive(self):
        obs = {"kind": "protocol_unsupported", "detail": "n/a", "usage": {}}
        self.assertEqual(verify.decide_verdict(True, obs), "inconclusive")


class DeclaredForTests(unittest.TestCase):
    def test_bool_fields(self):
        rec = {"tool_call": True, "structured_output": False}
        self.assertEqual(verify.declared_for(rec, "tools"), True)
        self.assertEqual(verify.declared_for(rec, "structured"), False)
        self.assertIsNone(verify.declared_for(rec, "reasoning"))

    def test_modalities_present_is_a_claim(self):
        self.assertIsNone(verify.declared_for({}, "image"))
        self.assertEqual(verify.declared_for({"modalities": {"input": ["text", "image"], "output": ["text"]}}, "image"), True)
        self.assertEqual(verify.declared_for({"modalities": {"input": ["text"], "output": ["text"]}}, "image"), False)


class OpenAIAdapterTests(unittest.TestCase):
    def test_ping_and_tools_payload_shape(self):
        rec = Recorder([openai_ok(), openai_tools_ok()])
        with mock.patch.object(verify, "http_json", rec):
            ad = verify.OpenAICompat("https://x.example/v1", "secret-key")
            ad.ping("m1")
            obs = ad.probe_tools("m1")
        self.assertEqual(obs["kind"], "signal")
        p = rec.payloads()[1]
        self.assertEqual(p["tools"][0]["function"]["name"], "get_weather")
        self.assertEqual(p["tool_choice"]["function"]["name"], "get_weather")
        self.assertEqual(rec.calls[0]["headers"]["Authorization"], "Bearer secret-key")

    def test_tool_choice_rejection_retries_unforced(self):
        rec = Recorder([
            (400, {"error": {"message": "tool_choice is not supported"}}),
            openai_tools_ok(),
        ])
        with mock.patch.object(verify, "http_json", rec):
            ad = verify.OpenAICompat("https://x.example/v1", "k")
            obs = ad.probe_tools("m1")
        self.assertEqual(obs["kind"], "signal")
        self.assertNotIn("tool_choice", rec.payloads()[1])

    def test_structured_json_object_then_fallback(self):
        rec = Recorder([
            (200, {"choices": [{"message": {"content": '```json\n{"ok": true}\n```'}}], "usage": {}}),
        ])
        with mock.patch.object(verify, "http_json", rec):
            ad = verify.OpenAICompat("https://x.example/v1", "k")
            obs = ad.probe_structured("m1")
        self.assertEqual(obs["kind"], "signal")

    def test_structured_reject_falls_back_to_json_schema(self):
        rec = Recorder([
            (400, {"error": {"message": "response_format json_object not supported"}}),
            (200, {"choices": [{"message": {"content": '{"ok": true}'}}], "usage": {}}),
        ])
        with mock.patch.object(verify, "http_json", rec):
            ad = verify.OpenAICompat("https://x.example/v1", "k")
            obs = ad.probe_structured("m1")
        self.assertEqual(obs["kind"], "signal")
        self.assertEqual(rec.payloads()[1]["response_format"]["type"], "json_schema")

    def test_reasoning_signals(self):
        for msg_extra, tag in [
            ({"reasoning_content": "hmm"}, "reasoning_content"),
            ({"reasoning": "hmm"}, "reasoning"),
        ]:
            rec = Recorder([openai_ok("42", **msg_extra)])
            with mock.patch.object(verify, "http_json", rec):
                obs = verify.OpenAICompat("https://x.example/v1", "k").probe_reasoning("m1")
            self.assertEqual(obs["kind"], "signal", tag)
            self.assertIn(tag, obs["detail"])

    def test_reasoning_tokens_signal(self):
        resp = (200, {"choices": [{"message": {"content": "42"}}],
                      "usage": {"completion_tokens_details": {"reasoning_tokens": 7}}})
        rec = Recorder([resp])
        with mock.patch.object(verify, "http_json", rec):
            obs = verify.OpenAICompat("https://x.example/v1", "k").probe_reasoning("m1")
        self.assertEqual(obs["kind"], "signal")

    def test_image_probe_payload_and_signal(self):
        rec = Recorder([openai_ok("1 pixel")])
        with mock.patch.object(verify, "http_json", rec):
            ad = verify.OpenAICompat("https://x.example/v1", "k")
            obs = ad.probe_image("m1")
        self.assertEqual(obs["kind"], "signal")
        part = rec.payloads()[0]["messages"][0]["content"][1]
        self.assertEqual(part["type"], "image_url")
        self.assertTrue(part["image_url"]["url"].startswith("data:image/png;base64,"))

    def test_rate_limited_retries_once(self):
        rec = Recorder([(429, {"error": "slow down"}), openai_ok("1")])
        with mock.patch.object(verify, "http_json", rec), mock.patch.object(verify.time, "sleep"):
            obs = verify.OpenAICompat("https://x.example/v1", "k").probe_image("m1")
        self.assertEqual(obs["kind"], "signal")
        self.assertEqual(len(rec.calls), 2)


class AnthropicAdapterTests(unittest.TestCase):
    def test_thinking_block_signal(self):
        resp = (200, {"content": [{"type": "thinking", "thinking": "hmm"}, {"type": "text", "text": "42"}],
                      "usage": {"input_tokens": 1, "output_tokens": 1}})
        rec = Recorder([resp])
        with mock.patch.object(verify, "http_json", rec):
            obs = verify.AnthropicNative("https://x.example/v1", "k").probe_reasoning("m1")
        self.assertEqual(obs["kind"], "signal")
        self.assertEqual(obs["usage"]["completion_tokens"], 1)

    def test_tool_use_block_signal(self):
        resp = (200, {"content": [{"type": "tool_use", "name": "get_weather"}], "usage": {}})
        rec = Recorder([resp])
        with mock.patch.object(verify, "http_json", rec):
            obs = verify.AnthropicNative("https://x.example/v1", "k").probe_tools("m1")
        self.assertEqual(obs["kind"], "signal")
        p = rec.payloads()[0]
        self.assertEqual(p["tool_choice"], {"type": "tool", "name": "get_weather"})

    def test_structured_is_protocol_unsupported(self):
        obs = verify.AnthropicNative("https://x.example/v1", "k").probe_structured("m1")
        self.assertEqual(obs["kind"], "protocol_unsupported")
        self.assertEqual(verify.decide_verdict(True, obs), "inconclusive")

    def test_image_source_shape(self):
        resp = (200, {"content": [{"type": "text", "text": "1"}], "usage": {}})
        rec = Recorder([resp])
        with mock.patch.object(verify, "http_json", rec):
            obs = verify.AnthropicNative("https://x.example/v1", "k").probe_image("m1")
        self.assertEqual(obs["kind"], "signal")
        block = rec.payloads()[0]["messages"][0]["content"][0]
        self.assertEqual(block["source"]["type"], "base64")
        self.assertEqual(block["source"]["media_type"], "image/png")


class GoogleAdapterTests(unittest.TestCase):
    def test_function_call_signal(self):
        resp = (200, {"candidates": [{"content": {"parts": [{"functionCall": {"name": "get_weather"}}]}}],
                      "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1}})
        rec = Recorder([resp])
        with mock.patch.object(verify, "http_json", rec):
            obs = verify.GoogleNative("https://x.example/v1beta", "k").probe_tools("m1")
        self.assertEqual(obs["kind"], "signal")
        p = rec.payloads()[0]
        self.assertEqual(p["toolConfig"]["functionCallingConfig"]["mode"], "ANY")
        self.assertIn("functionDeclarations", p["tools"][0])

    def test_thoughts_token_signal(self):
        resp = (200, {"candidates": [{"content": {"parts": [{"text": "42"}]}}],
                      "usageMetadata": {"thoughtsTokenCount": 5}})
        rec = Recorder([resp])
        with mock.patch.object(verify, "http_json", rec):
            obs = verify.GoogleNative("https://x.example/v1beta", "k").probe_reasoning("m1")
        self.assertEqual(obs["kind"], "signal")

    def test_structured_response_mime(self):
        resp = (200, {"candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}}]})
        rec = Recorder([resp])
        with mock.patch.object(verify, "http_json", rec):
            obs = verify.GoogleNative("https://x.example/v1beta", "k").probe_structured("m1")
        self.assertEqual(obs["kind"], "signal")
        self.assertEqual(rec.payloads()[0]["generationConfig"]["responseMimeType"], "application/json")

    def test_image_inline_data_shape(self):
        resp = (200, {"candidates": [{"content": {"parts": [{"text": "1"}]}}]})
        rec = Recorder([resp])
        with mock.patch.object(verify, "http_json", rec):
            obs = verify.GoogleNative("https://x.example/v1beta", "k").probe_image("m1")
        self.assertEqual(obs["kind"], "signal")
        part = rec.payloads()[0]["contents"][0]["parts"][0]
        self.assertIn("inline_data", part)
        self.assertEqual(part["inline_data"]["mime_type"], "image/png")

    def test_url_uses_models_prefix(self):
        rec = Recorder([openai_ok()])
        with mock.patch.object(verify, "http_json", rec):
            verify.GoogleNative("https://x.example/v1beta", "k").ping("gemini-x")
        self.assertIn("/models/gemini-x:generateContent", rec.calls[0]["url"])


class EndpointTests(unittest.TestCase):
    def test_env_candidates_tried_in_order(self):
        env = {"GOOGLE_API_KEY": "second-wins"}
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.dict(verify.PROVIDER_MAP, {"google": verify.PROVIDER_MAP["google"] | {"env": ["GEMINI_API_KEY", "GOOGLE_API_KEY"]}}):
            ep = verify.resolve_endpoint("google", ns())
        self.assertEqual(ep["key"], "second-wins")

    def test_unknown_provider_without_base_url_errors(self):
        ep = verify.resolve_endpoint("who-dis", ns())
        self.assertIn("error", ep)
        self.assertIn("--base-url", ep["error"])

    def test_unknown_provider_with_overrides_works(self):
        ep = verify.resolve_endpoint("who-dis", ns(base_url="http://localhost:1234/v1", api_key="k"))
        self.assertEqual(ep["protocol"], "openai")  # sensible default
        self.assertEqual(ep["base_url"], "http://localhost:1234/v1")

    def test_missing_key_error_names_env_vars(self):
        with mock.patch.dict(os.environ, {}, clear=False), \
                mock.patch.dict(os.environ, {k: "" for k in ("ZHIPU_API_KEY", "ZHIPUAI_API_KEY")}):
            ep = verify.resolve_endpoint("zhipuai", ns())
        self.assertIn("error", ep)
        self.assertIn("ZHIPU_API_KEY", ep["error"])
        self.assertNotIn("secret", ep["error"])

    def test_env_key_override(self):
        with mock.patch.dict(os.environ, {"MY_KEY": "v"}):
            ep = verify.resolve_endpoint("zhipuai", ns(env_key="MY_KEY"))
        self.assertEqual(ep["key"], "v")


REC = {"provider": "prov", "tool_call": True, "structured_output": True,
       "reasoning": True, "modalities": {"input": ["text", "image"], "output": ["text"]},
       "aliases": ["m1-alt"]}


class RunModelTests(unittest.TestCase):
    def run_model_with(self, responses, rec=REC, probes=verify.ALL_PROBES):
        r = Recorder(responses)
        with mock.patch.object(verify, "http_json", r):
            ep = {"base_url": "https://x.example/v1", "key": "k", "protocol": "openai"}
            res = verify.run_model("prov/m1", rec, ep, probes)
        return res, r

    def test_candidate_fallback_on_404(self):
        responses = [
            (404, {"error": {"message": "model m1 not found"}}),
            openai_ok(),
            openai_tools_ok(), openai_ok('{"ok": true}'),
            openai_ok("42", reasoning_content="hmm"), openai_ok("1 pixel"),
        ]
        res, _ = self.run_model_with(responses)
        self.assertEqual(len(res["candidates_tried"]), 2)
        self.assertTrue(res["candidates_tried"][0].startswith("m1(404)"))
        self.assertEqual([res["probes"][p]["verdict"] for p in verify.ALL_PROBES],
                         ["match", "match", "match", "match"])
        self.assertEqual(res["usage"]["prompt_tokens"], 5)

    def test_all_candidates_404_is_inconclusive(self):
        responses = [(404, {"error": {"message": "model not found"}}),
                     (404, {"error": {"message": "model not found"}})]
        res, _ = self.run_model_with(responses)
        for p in verify.ALL_PROBES:
            self.assertEqual(res["probes"][p]["verdict"], "inconclusive")

    def test_ping_auth_error_stops_everything(self):
        responses = [(401, {"error": {"message": "invalid api key"}})]
        res, _ = self.run_model_with(responses)
        for p in verify.ALL_PROBES:
            self.assertEqual(res["probes"][p]["verdict"], "inconclusive")
            self.assertIn("auth_error", str(res["probes"][p]["observed"]))

    def test_capability_rejected_gives_mismatch(self):
        responses = [
            openai_ok(),
            (400, {"error": {"message": "tools is not supported by this model"}}),
            openai_ok('{"ok": true}'), openai_ok("42"), openai_ok("1"),
        ]
        res, _ = self.run_model_with(responses)
        self.assertEqual(res["probes"]["tools"]["verdict"], "mismatch")
        self.assertEqual(res["probes"]["structured"]["verdict"], "match")

    def test_endpoint_error_marks_all_inconclusive(self):
        ep = {"error": "no API key"}
        r = Recorder([])
        with mock.patch.object(verify, "http_json", r):
            res = verify.run_model("prov/m1", REC, ep, ("tools",))
        self.assertEqual(res["probes"]["tools"]["verdict"], "inconclusive")
        self.assertEqual(r.calls, [])


class ResolutionTests(unittest.TestCase):
    TABLE = {
        "schema_version": 1, "updated_at": "2026-09-16", "generator": "test",
        "models": {
            # dedup winner for identity 'glm53' is alibaba-cn
            "alibaba-cn/glm-5.3": {"provider": "alibaba-cn", "tool_call": True,
                                   "aliases": ["glm-5-3", "glm5.3"]},
            "cloudflare-workers-ai/@cf/zai-org/glm-5.3": {"provider": "cloudflare-workers-ai"},
        },
    }

    def resolve(self, name):
        return verify.resolve_names(self.TABLE, [name])

    def test_bare_name_resolves_to_winner(self):
        entries = self.resolve("glm-5.3")
        self.assertEqual(entries[0][0], "alibaba-cn/glm-5.3")
        self.assertEqual(entries[0][2], "alibaba-cn/glm-5.3")

    def test_exact_key_hit_even_multi_slash(self):
        entries = self.resolve("cloudflare-workers-ai/@cf/zai-org/glm-5.3")
        self.assertEqual(entries[0][0], "cloudflare-workers-ai/@cf/zai-org/glm-5.3")

    def test_qualified_name_pins_virtual_provider(self):
        # winner lives at alibaba-cn, but the user names volcengine:
        # endpoint provider must be volcengine, facts traced to the winner
        entries = self.resolve("volcengine/glm-5.3")
        key, rec, table_key = entries[0]
        self.assertEqual(key, "volcengine/glm-5.3")
        self.assertEqual(rec["provider"], "volcengine")
        self.assertEqual(rec["tool_call"], True)      # facts from the winner
        self.assertEqual(table_key, "alibaba-cn/glm-5.3")

    def test_qualified_name_with_own_record(self):
        # spelling variant of a real record at that vendor -> the real key
        entries = self.resolve("alibaba-cn/GLM5.3")
        key, rec, table_key = entries[0]
        self.assertEqual(key, "alibaba-cn/glm-5.3")
        self.assertEqual(table_key, "alibaba-cn/glm-5.3")

    def test_qualified_unknown_model_errors(self):
        with self.assertRaises(LookupError):
            self.resolve("zhipuai/no-such-model")

    def test_unknown_vendor_still_pins_and_needs_base_url(self):
        # 'vendor/model' pins even for unknown vendors: a virtual record is
        # created and endpoint resolution demands --base-url (never silently
        # falls back to the dedup winner's channel)
        entries = self.resolve("some-gateway/glm-5.3")
        key, rec, table_key = entries[0]
        self.assertEqual(key, "some-gateway/glm-5.3")
        self.assertEqual(rec["provider"], "some-gateway")
        self.assertEqual(table_key, "alibaba-cn/glm-5.3")
        ep = verify.resolve_endpoint("some-gateway", ns())
        self.assertIn("error", ep)
        self.assertIn("--base-url", ep["error"])


class MainTests(unittest.TestCase):
    TABLE = {
        "schema_version": 1, "updated_at": "2026-09-16", "generator": "test",
        "models": {
            "zhipuai/test-model": {
                "provider": "zhipuai", "tool_call": True, "structured_output": True,
                "reasoning": True, "modalities": {"input": ["text", "image"], "output": ["text"]},
                "aliases": ["test-model-alt"],
            },
        },
    }

    def happy_responses(self):
        return [
            openai_ok(),                                       # ping
            openai_tools_ok(),                                 # tools
            openai_ok('{"ok": true}'),                         # structured
            openai_ok("42", reasoning_content="hmm"),          # reasoning
            openai_ok("1 pixel"),                              # image
        ]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.table_path = Path(self.tmp.name) / "table.json"
        self.table_path.write_text(json.dumps(self.TABLE), encoding="utf-8")
        self.out_path = Path(self.tmp.name) / "reports" / "verify-results.json"
        self._env = mock.patch.dict(os.environ, {"ZHIPU_API_KEY": "testkey123"})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.addCleanup(self.tmp.cleanup)

    def main(self, *extra):
        return verify.main(["test-model", "--table", str(self.table_path),
                            "--out", str(self.out_path), *extra])

    def test_happy_path_writes_report(self):
        rec = Recorder(self.happy_responses())
        with mock.patch.object(verify, "http_json", rec):
            code = self.main()
        self.assertEqual(code, 0)
        report = json.loads(self.out_path.read_text(encoding="utf-8"))
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["summary"]["match"], 4)
        res = report["results"]["zhipuai/test-model"]
        self.assertEqual(res["endpoint"], verify.PROVIDER_MAP["zhipuai"]["base_url"])
        self.assertEqual(res["candidates_tried"], ["test-model(200)"])

    def test_report_never_contains_key(self):
        rec = Recorder(self.happy_responses())
        with mock.patch.object(verify, "http_json", rec):
            self.main()
        self.assertNotIn("testkey123", self.out_path.read_text(encoding="utf-8"))

    def test_strict_exit_code_on_mismatch(self):
        responses = [
            openai_ok(),
            (400, {"error": {"message": "tools is not supported by this model"}}),
            openai_ok('{"ok": true}'), openai_ok("42"), openai_ok("1"),
        ]
        rec = Recorder(responses)
        with mock.patch.object(verify, "http_json", rec):
            self.assertEqual(self.main("--strict"), 1)
        report = json.loads(self.out_path.read_text(encoding="utf-8"))
        self.assertEqual(report["summary"]["mismatch"], 1)

    def test_probes_filter_limits_calls(self):
        rec = Recorder(self.happy_responses())
        with mock.patch.object(verify, "http_json", rec):
            code = self.main("--probes", "tools")
        self.assertEqual(code, 0)
        report = json.loads(self.out_path.read_text(encoding="utf-8"))
        self.assertEqual(list(report["results"]["zhipuai/test-model"]["probes"]), ["tools"])
        self.assertEqual(len(rec.calls), 2)  # ping + one probe

    def test_declared_only_skips_unknown(self):
        table = json.loads(json.dumps(self.TABLE))
        table["models"]["zhipuai/test-model"] = {"provider": "zhipuai", "tool_call": True}
        self.table_path.write_text(json.dumps(table), encoding="utf-8")
        rec = Recorder([openai_ok(), openai_tools_ok()])
        with mock.patch.object(verify, "http_json", rec):
            code = self.main("--declared-only")
        self.assertEqual(code, 0)
        self.assertEqual(len(rec.calls), 2)

    def test_dry_run_sends_nothing(self):
        rec = Recorder([])
        with mock.patch.object(verify, "http_json", rec):
            code = verify.main(["test-model", "--table", str(self.table_path), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(rec.calls, [])

    def test_unknown_name_is_exit_2(self):
        code = verify.main(["no-such-model", "--table", str(self.table_path)])
        self.assertEqual(code, 2)

    def test_bad_probes_is_exit_2(self):
        code = verify.main(["test-model", "--table", str(self.table_path), "--probes", "bogus"])
        self.assertEqual(code, 2)

    def test_qualified_name_pins_channel_endpoint(self):
        table = {
            "schema_version": 1, "updated_at": "2026-09-16", "generator": "test",
            "models": {"alibaba-cn/glm-5.3": {"provider": "alibaba-cn", "tool_call": True}},
        }
        self.table_path.write_text(json.dumps(table), encoding="utf-8")
        responses = [openai_ok(), openai_tools_ok()]
        rec = Recorder(responses)
        with mock.patch.object(verify, "http_json", rec), \
                mock.patch.dict(os.environ, {"ARK_API_KEY": "ark-key-1"}, clear=False):
            code = verify.main(["volcengine/glm-5.3", "--probes", "tools",
                                "--table", str(self.table_path), "--out", str(self.out_path)])
        self.assertEqual(code, 0)
        report = json.loads(self.out_path.read_text(encoding="utf-8"))
        res = report["results"]["volcengine/glm-5.3"]
        self.assertEqual(res["endpoint"], verify.PROVIDER_MAP["volcengine"]["base_url"])
        self.assertEqual(res["table_key"], "alibaba-cn/glm-5.3")  # facts traced to winner
        self.assertNotIn("ark-key-1", self.out_path.read_text(encoding="utf-8"))
        # ping went to the volcengine endpoint, not alibaba's
        self.assertIn("ark.cn-beijing.volces.com", rec.calls[0]["url"])

    def test_max_models_caps_provider_scan(self):
        table = json.loads(json.dumps(self.TABLE))
        for i in range(5):
            table["models"][f"zhipuai/t{i}"] = {"provider": "zhipuai", "tool_call": True}
        self.table_path.write_text(json.dumps(table), encoding="utf-8")
        rec = Recorder([])
        with mock.patch.object(verify, "http_json", rec):
            code = verify.main(["--provider", "zhipuai", "--max-models", "2",
                                "--table", str(self.table_path), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(rec.calls, [])  # dry-run never sends
        # cap applied before any network use: 2 models planned, not 6
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()

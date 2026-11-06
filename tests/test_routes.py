"""Per-phase model configuration: parsing, validation and two-call routing."""
import copy
import json
import unittest
from unittest.mock import patch

from agent.plugin_llm import PluginLlmTrustError
from test_support import (
    ContextSteward, FakeLlm, ROOT, StewardCase, carrier_of, engine, history, plugin, steward, verdict,
)

GOOD_PROVIDER = patch("context_steward_test_plugin.steward.get_provider_profile",
                      return_value=object())


def handoff_json(**fields):
    handoff = {
        "goal": "Ship the fix",
        "completed": ["Read src/app.py"],
        "in_progress": [],
        "key_context": ["src/app.py"],
        "todo": [],
    }
    handoff.update(fields)
    return json.dumps(handoff, ensure_ascii=False)


class ParserTests(unittest.TestCase):
    def test_mode_tolerates_prose_and_fences(self):
        self.assertEqual(steward.parse_mode('{"mode": "fresh"}'), "fresh")
        self.assertEqual(steward.parse_mode('Result: {"mode": "checkpoint"} done'), "checkpoint")
        self.assertEqual(steward.parse_mode("checkpoint — work is still running"), "checkpoint")
        self.assertEqual(steward.parse_mode("```json\n{\"mode\":\"fresh\"}\n```"), "fresh")

    def test_mode_rejects_ambiguity(self):
        for raw in ("checkpoint or fresh?", '{"mode": "later"}', ""):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                steward.parse_mode(raw)

    def test_handoff_and_verdict_tolerate_prose(self):
        handoff = {"goal": "g", "completed": [], "in_progress": [], "key_context": [], "todo": []}
        self.assertEqual(steward.parse_handoff("here it is: " + json.dumps(handoff)), handoff)
        self.assertEqual(steward.parse_handoff(json.dumps({"handoff": handoff})), handoff)
        mode, parsed = steward.parse_verdict(
            "sure: " + json.dumps({"mode": "fresh", "handoff": handoff}))
        self.assertEqual((mode, parsed), ("fresh", handoff))

    def test_mode_ignores_braces_inside_strings(self):
        raw = 'note "{ not json" then {"mode": "fresh"}'
        self.assertEqual(steward.parse_mode(raw), "fresh")


class ParseTests(unittest.TestCase):
    def test_string_shorthand_and_empty(self):
        self.assertEqual(steward.parse_model_route("cheap", label="x").model, "cheap")
        self.assertFalse(steward.parse_model_route(None, label="x").configured)
        self.assertFalse(steward.parse_model_route({}, label="x").configured)

    def test_structural_errors_are_explicit(self):
        cases = {
            "unknown key": {"provider": "p", "model": "m", "temprature": 1},
            "provider without model": {"provider": "p"},
            "non-string": {"model": 5},
            "bad env name": {"model": "m", "api_key_env": "my-key"},
            "bad base_url": {"model": "m", "base_url": "ftp://x"},
        }
        for name, raw in cases.items():
            with self.subTest(name=name), self.assertRaises(ValueError) as caught:
                steward.parse_model_route(raw, label="context.steward.verdict")
            self.assertIn("context.steward.verdict", str(caught.exception))

    def test_base_url_and_api_key_env_are_logged_unsupported(self):
        with self.assertLogs("context_steward_test_plugin.steward", level="WARNING") as logs:
            route = steward.parse_model_route(
                {"model": "m", "base_url": "https://example.test/v1", "api_key_env": "MY_KEY"},
                label="context.steward.summary")
        self.assertEqual(route.model, "m")
        text = "\n".join(logs.output)
        self.assertIn("ignored", text)
        self.assertIn("custom provider", text)
        self.assertEqual(route.overrides(), {"model": "m"})  # endpoint never passed through

    def test_unknown_provider_is_dropped_loudly(self):
        with patch("context_steward_test_plugin.steward.get_provider_profile", return_value=None), \
                self.assertLogs("context_steward_test_plugin.steward", level="WARNING") as logs:
            route = steward.parse_model_route({"provider": "nope", "model": "m"},
                                              label="context.steward.verdict")
        self.assertFalse(route.configured)
        self.assertIn("unknown", "\n".join(logs.output))

    def test_known_provider_is_kept(self):
        with GOOD_PROVIDER:
            route = steward.parse_model_route({"provider": "magpie", "model": "m"},
                                              label="context.steward.verdict")
        self.assertEqual(route.overrides(), {"provider": "magpie", "model": "m"})


class SettingsTests(unittest.TestCase):
    def test_toplevel_keys(self):
        with GOOD_PROVIDER:
            e = ContextSteward.from_settings(
                {"verdict": {"provider": "magpie", "model": "small"},
                 "summary": {"provider": "magpie", "model": "big"}})
        self.assertEqual(e.verdict_route.model, "small")
        self.assertEqual(e.summary_route.model, "big")
        self.assertTrue(e._split())

    def test_legacy_verdict_model_and_conflict(self):
        e = ContextSteward.from_settings({"verdict_model": "cheap"})
        self.assertEqual(e.verdict_model, "cheap")
        with self.assertRaises(ValueError):
            ContextSteward.from_settings({"verdict_model": "cheap", "verdict": {"model": "x"}})

    def test_unknown_key_and_route_typos(self):
        with self.assertRaises(ValueError) as caught:
            ContextSteward.from_settings({"typo": 1})
        self.assertIn("typo", str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            ContextSteward.from_settings({"verdict": {"modle": "x"}})
        self.assertIn("modle", str(caught.exception))


class RoutingTests(StewardCase):
    def test_release_defaults_use_aux_compression_without_an_operator_route(self):
        llm = FakeLlm(verdict())
        e = ContextSteward.from_settings({}, compression={"threshold": 0.3}, llm=llm)
        e.update_model("main-model", 1_000_000)
        result = e.compress(self.messages, force=True)
        self.assertFalse(e.verdict_route.configured)
        self.assertFalse(e.summary_route.configured)
        self.assertEqual(e.threshold_tokens, 300_000)
        self.assertEqual(e.last_outcome, "compacted")
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(llm.calls[0]["task"], "compression")
        self.assertEqual(llm.calls[0]["purpose"], "context-steward.judgment")
        for key in ("provider", "model", "base_url", "api_key", "api_key_env"):
            self.assertNotIn(key, llm.calls[0])
        self.assertIn("Read src/app.py", carrier_of(result))

    def test_catalog_has_no_operator_specific_provider_or_endpoint(self):
        content = (ROOT / "catalog" / "context-steward.yaml").read_text(encoding="utf-8")
        for operator_value in ("magpie", "opendesign/", "127.0.0.1:3425", "1048000", "1,048,000"):
            self.assertNotIn(operator_value, content)

    def test_single_call_uses_the_configured_route(self):
        llm = FakeLlm(verdict())
        e = ContextSteward(model="m", llm=llm, verdict_route=steward.ModelRoute("magpie", "small"))
        e.update_model("m", 100_000)
        e.compress(self.messages, force=True)
        self.assertEqual(llm.calls[0]["provider"], "magpie")
        self.assertEqual(llm.calls[0]["model"], "small")
        self.assertNotIn("task", llm.calls[0])

    def test_single_call_falls_back_when_the_route_is_denied(self):
        llm = FakeLlm(verdict(), trust_errors=2)  # deny the route, then the aux task
        e = ContextSteward(model="m", llm=llm, verdict_route=steward.ModelRoute("magpie", "small"))
        e.update_model("m", 100_000)
        e.compress(self.messages, force=True)
        self.assertEqual(len(llm.calls), 3)
        self.assertEqual(llm.calls[0]["model"], "small")
        self.assertEqual(llm.calls[1]["task"], "compression")
        self.assertNotIn("task", llm.calls[2])
        self.assertNotIn("model", llm.calls[2])

    def test_split_uses_two_models_and_keeps_the_result(self):
        llm = FakeLlm(json.dumps({"mode": "checkpoint"}), handoff_json())
        e = ContextSteward(model="m", llm=llm,
                           verdict_route=steward.ModelRoute("magpie", "small"),
                           summary_route=steward.ModelRoute("magpie", "big"))
        e.update_model("m", 100_000)
        result = e.compress(self.messages, force=True)
        self.assertEqual(e.last_mode, "checkpoint")
        self.assertEqual(e.last_outcome, "compacted")
        self.assertEqual(len(llm.calls), 2)
        self.assertEqual((llm.calls[0]["model"], llm.calls[0]["purpose"]), ("small", "context-steward.verdict"))
        self.assertEqual((llm.calls[1]["model"], llm.calls[1]["purpose"]), ("big", "context-steward.summary"))
        self.assertIn("Read src/app.py", carrier_of(result))

    def test_split_fresh_is_downgraded_when_the_handoff_has_work(self):
        llm = FakeLlm(json.dumps({"mode": "fresh"}), handoff_json(in_progress=["tests"]))
        e = ContextSteward(model="m", llm=llm,
                           verdict_route=steward.ModelRoute("magpie", "small"),
                           summary_route=steward.ModelRoute("magpie", "big"))
        e.update_model("m", 100_000)
        e.compress(self.messages, force=True)
        self.assertEqual(e.last_mode, "checkpoint")

    def test_split_verdict_failure_degrades_mechanically(self):
        llm = FakeLlm("{not json")
        e = ContextSteward(model="m", llm=llm,
                           verdict_route=steward.ModelRoute("magpie", "small"),
                           summary_route=steward.ModelRoute("magpie", "big"))
        e.update_model("m", 100_000)
        result = e.compress(self.messages, force=True)
        self.assertEqual(e.last_outcome, "degraded")
        self.assertEqual(len(llm.calls), 1)
        self.assert_quotes(result)

    def test_equal_verdict_and_summary_routes_stay_a_single_call(self):
        llm = FakeLlm(verdict())
        e = ContextSteward(model="m", llm=llm,
                           verdict_route=steward.ModelRoute("magpie", "same"),
                           summary_route=steward.ModelRoute("magpie", "same"))
        e.update_model("m", 100_000)
        e.compress(self.messages, force=True)
        self.assertEqual(len(llm.calls), 1)

    def test_clone_keeps_the_routes(self):
        e = ContextSteward(model="m", llm=FakeLlm(),
                           verdict_route=steward.ModelRoute("magpie", "small"),
                           summary_route=steward.ModelRoute("magpie", "big"))
        clone = e.clone_for_agent()
        self.assertEqual(clone.verdict_route.model, "small")
        self.assertEqual(clone.summary_route.model, "big")
        self.assertIs(clone._facade(), e._facade())


if __name__ == "__main__":
    unittest.main()

"""Boundary and fault coverage. Real engine + real host classes; model calls faked."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.context_engine import ContextEngine
from test_support import (
    ContextSteward, FakeLlm, FOOTER, ROOT, StewardCase, carrier_of, engine, history, plugin,
    steward, tool_round, verdict,
)


def call(i, output_chars=8_000):
    return tool_round(i, output_chars)


class ThresholdTests(unittest.TestCase):
    def test_exact_boundaries_across_windows_and_ratios(self):
        for window in (2_000, 8_192, 32_000, 128_000, 1_000_000):
            for ratio in (0.1, 0.25, 0.3, 0.5, 0.8):
                with self.subTest(window=window, ratio=ratio):
                    e = ContextSteward(model="fixture", check_ratio=ratio, llm=FakeLlm(verdict()))
                    e.update_model("fixture", window)
                    threshold = int(window * ratio)
                    self.assertEqual(e.threshold_tokens, threshold)
                    self.assertFalse(e.should_compress(threshold - 1))
                    self.assertTrue(e.should_compress(threshold))
                    self.assertTrue(e.should_compress(threshold + 1))
                    e.update_model("other", window)
                    self.assertEqual(e.threshold_tokens, threshold)

    def test_profile_thresholds_inherited_from_compression_block(self):
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        from hermes_cli.config import load_config_readonly

        class Collector:
            llm = FakeLlm(verdict())

            def register_context_engine(self, e):
                self.engine = e

        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as tmp:
            for name, ratio in (("profile-a", 0.3), ("profile-b", 0.3), ("profile-c", 0.3)):
                home = Path(tmp) / name
                home.mkdir()
                (home / "config.yaml").write_text(
                    f"compression:\n  threshold: {ratio}\ncontext:\n  engine: context-steward\n",
                    encoding="utf-8")
                self.assertNotIn("check_ratio", (home / "config.yaml").read_text(encoding="utf-8"))
                token = set_hermes_home_override(home)
                try:
                    self.assertEqual(load_config_readonly()["compression"]["threshold"], ratio)
                    ctx = Collector()
                    plugin.register(ctx)
                    ctx.engine.update_model("fixture", 1_000_000)
                    self.assertEqual(ctx.engine.threshold_tokens, int(1_000_000 * ratio))
                finally:
                    reset_hermes_home_override(token)
        explicit = ContextSteward.from_settings({"check_ratio": 0.35},
                                                compression={"threshold": 0.3}, llm=FakeLlm())
        explicit.update_model("fixture", 100_000)
        self.assertEqual(explicit.threshold_tokens, 35_000)


class BoundaryTests(StewardCase):
    def test_empty_and_short_histories_are_lossless_without_calls(self):
        for n in range(4):
            messages = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"row {i}"}
                        for i in range(n)]
            original = copy.deepcopy(messages)
            llm = FakeLlm(verdict())
            e = engine(llm)
            self.assertIs(e.compress(messages), messages)
            self.assertEqual(messages, original)
            self.assertEqual(llm.calls, [])

    def test_single_message_larger_than_half_window_is_never_cut(self):
        e = engine(FakeLlm(verdict()), window=32_000)
        messages = history() + [{"role": "user", "content": "超长原文\n" * 20_000 + "END-OF-REQUEST"}]
        latest = messages[-1]["content"]
        original = copy.deepcopy(messages)
        result = e.compress(messages, force=True)
        self.assertTrue(any(m.get("content") == latest for m in result))
        self.assertEqual(messages, original)

    def test_two_hundred_large_tool_results_keep_request_and_tail_pairs(self):
        messages = history()
        messages[-2]["display_kind"] = "internal_notification"
        current = messages[-2]["content"]
        for i in range(200):
            messages.extend(call(i, 20_000))
        original = copy.deepcopy(messages)
        e = engine(FakeLlm(verdict()))
        result = e.compress(messages, force=True)
        self.assertEqual(e.last_outcome, "compacted")
        self.assertTrue(any(m.get("role") == "user" and m.get("content") == current for m in result))
        summary = carrier_of(result)
        self.assertNotIn("## Unfinished User Request Restatement", summary)
        for tail in original[-6:]:
            self.assertTrue(any(m.get("content") == tail.get("content")
                                and m.get("tool_call_id") == tail.get("tool_call_id")
                                and m.get("tool_calls") == tail.get("tool_calls") for m in result))
        calls = {c["id"] for m in result for c in m.get("tool_calls") or []}
        for message in result:
            if message.get("role") == "tool":
                self.assertIn(message["tool_call_id"], calls)
        self.assertEqual(messages, original)

    def test_multilingual_code_blocks_survive_in_both_modes(self):
        phrases = [
            "中文：保留  空格\n```python\nx = '你好'\n```\n",
            "日本語の依頼\n```json\n{\"金額\": 123}\n```",
            "العربية + français + emoji 🧪\n```sh\nprintf '%s\\n' café\n```",
        ]
        for mode in ("fresh", "checkpoint"):
            messages = history()
            for message, text in zip([m for m in messages if m["role"] == "user"][-3:], phrases):
                message["content"] = text
                message["display_kind"] = "internal_notification"
            original = copy.deepcopy(messages)
            e = engine(FakeLlm(verdict(mode)))
            result = e.compress(messages, force=True)
            self.assertEqual(e.last_mode, mode)
            self.assertEqual(e.last_outcome, "compacted")
            self.assertEqual(messages, original)
            for text in phrases:
                self.assertTrue(any(m.get("role") == "user" and m.get("content") == text for m in result))

    def test_three_successive_compactions_keep_requests_and_previous_facts(self):
        facts = "FACT: basalt-742; owner=Orion; deadline=2031-09-03"
        messages = history()
        llm = FakeLlm(verdict("checkpoint", goal=facts, unfinished=["still working"]))
        e = engine(llm)
        for generation in range(3):
            latest = f"Unfinished request generation={generation}; exact fact {facts}\n```py\nx = {generation}\n```"
            messages += history()[1:] + [{"role": "user", "display_kind": "internal_notification",
                                          "content": latest}]
            messages += call(1000 + generation)
            before = copy.deepcopy(messages)
            result = e.compress(messages, force=True)
            self.assertEqual(e.compression_count, generation + 1)
            self.assertEqual(e.last_mode, "checkpoint")
            self.assertTrue(any(m.get("role") == "user" and m.get("content") == latest for m in result))
            self.assertIn(facts, carrier_of(result))
            prompt = llm.calls[-1]["messages"][0]["content"]
            if generation:
                previous = prompt.split("PREVIOUS HANDOFF:\n", 1)[1].split("SAMPLED RECORDS", 1)[0]
                self.assertIn(facts, previous)
            self.assertEqual(messages, before)
            messages = result
        self.assertEqual(len(llm.calls), 3)

    def test_api_content_sidecars_do_not_false_negative(self):
        messages = history()
        for message in messages[-6:]:
            message["api_content"] = message.get("content")
        messages[-1]["reasoning_content"] = "stale reasoning"
        original = copy.deepcopy(messages)
        e = engine(FakeLlm(verdict()))
        result = e.compress(messages, force=True)
        self.assertEqual(e.last_outcome, "compacted")
        self.assertEqual(messages, original)
        self.assertNotIn("api_content", carrier_of(result))

    def test_resume_with_a_single_historical_user_keeps_it(self):
        only = "only request"
        sparse = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": only},
            {"role": "assistant", "content": "one"},
            {"role": "tool", "tool_call_id": "mid", "content": "raw tool body " * 100},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "pending", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]},
        ]
        result = engine(FakeLlm(verdict())).compress(sparse, force=True)
        self.assertTrue(any(m.get("role") == "user" and m.get("content") == only for m in result))


class FaultTests(StewardCase):
    def test_judgment_faults_degrade_without_losing_messages(self):
        faults = {
            "timeout": TimeoutError("injected timeout"),
            "network": ConnectionError("injected connection error"),
            "invalid_json": "{not json",
            "empty_response": "",
            "empty_handoff": json.dumps({"mode": "fresh", "handoff": {}}),
        }
        for name, fault in faults.items():
            with self.subTest(name=name):
                messages = history() + [{"role": "user", "display_kind": "internal_notification",
                                         "content": f"LATEST-{name}-EXACT\n正在进行"}]
                original = copy.deepcopy(messages)
                llm = FakeLlm(fault)
                e = engine(llm)
                result = e.compress(messages, force=True)
                self.assertEqual(e.last_outcome, "degraded")
                self.assertEqual(e.last_mode, "checkpoint")
                self.assertEqual(len(llm.calls), 1)
                for user in [m for m in messages if m["role"] == "user"][-3:]:
                    self.assertTrue(any(m.get("content") == user["content"] for m in result))
                for tail in messages[-6:]:
                    self.assertTrue(any(m.get("content") == tail.get("content")
                                        and m.get("tool_call_id") == tail.get("tool_call_id") for m in result))
                self.assertIn(FOOTER, carrier_of(result))
                self.assertEqual(messages, original)

    def test_degraded_checkpoint_still_shrinks_and_is_host_recognised(self):
        from agent.context_compressor import is_compaction_summary_message
        messages = history()
        e = engine(FakeLlm(ConnectionError("offline")))
        result = e.compress(messages, force=True)
        self.assertLess(steward.estimate_tokens(result), steward.estimate_tokens(messages))
        self.assertTrue(any(is_compaction_summary_message(m) for m in result))


if __name__ == "__main__":
    unittest.main()

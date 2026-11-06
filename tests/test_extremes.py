"""Executed boundary/fault tests; auxiliary/network results here are explicitly FAKE."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_steward import ROOT, ContextSteward, engine, history, plugin, response
from agent.context_compressor import ContextCompressor, is_compaction_summary_message
from agent.model_metadata import estimate_messages_tokens_rough


GOOD_PARENT = {"choices": [{"finish_reason": "stop", "message": {"content": (
    "## Historical Task Snapshot\nContinue the latest request\n"
    "## Goal\nPreserve the current task\n## Completed Actions\nInspected files\n"
    "## Active State\nTests are still running\n## Key Decisions\nKeep exact user text"
)}}]}


def call(i, output_chars=8_000):
    return [
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": f"call_{i}", "type": "function", "function": {
                "name": "terminal", "arguments": json.dumps({"command": f"check-{i}"}),
            },
        }]},
        {"role": "tool", "tool_call_id": f"call_{i}", "content": f"output-{i}\n" + "detail " * (output_chars // 7)},
    ]


class ExtremeTests(unittest.TestCase):
    def setUp(self):
        self.aux = patch("agent.context_compressor.call_llm", return_value=response()).start()
        self.addCleanup(patch.stopall)

    def test_exact_threshold_boundaries_and_small_windows(self):
        for window in (2_000, 8_192, 32_000, 128_000, 1_000_000):
            for ratio in (0.1, 0.25, 0.5, 0.8):
                with self.subTest(window=window, ratio=ratio):
                    e = ContextSteward(model="fixture", config_context_length=window, check_ratio=ratio)
                    threshold = int(window * ratio)
                    self.assertEqual(e.threshold_tokens, threshold)
                    self.assertFalse(e.should_compress(threshold - 1))
                    self.assertTrue(e.should_compress(threshold))
                    self.assertTrue(e.should_compress(threshold + 1))
                    e.update_model("other", window)
                    self.assertEqual(e.threshold_tokens, threshold)
        self.aux.assert_not_called()

    def test_profile_thresholds_from_actual_isolated_config_files(self):
        from hermes_constants import set_hermes_home_override, reset_hermes_home_override
        from hermes_cli.config import load_config_readonly
        class Collector:
            def register_context_engine(self, e):
                self.engine = e
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as tmp:
            for name, ratio in (("profile-a", 0.25), ("profile-b", 0.6), ("profile-c", 0.4)):
                home = Path(tmp) / name
                home.mkdir()
                (home / "config.yaml").write_text(
                    f"compression:\n  threshold: {ratio}\ncontext:\n  engine: context-steward\n",
                    encoding="utf-8",
                )
                token = set_hermes_home_override(home)
                try:
                    config = load_config_readonly()
                    self.assertEqual(config["compression"]["threshold"], ratio)
                    ctx = Collector()
                    plugin.register(ctx)
                    ctx.engine.update_model("fixture", 1_000_000)
                    self.assertEqual(ctx.engine.threshold_tokens, int(1_000_000 * ratio))
                finally:
                    reset_hermes_home_override(token)
        explicit = ContextSteward.from_settings({"check_ratio": 0.35}, compression={"threshold": 0.2})
        explicit.update_model("fixture", 100_000)
        self.assertEqual(explicit.threshold_tokens, 35_000)

    def test_api_content_and_reasoning_sidecars_do_not_false_negative(self):
        messages = history()
        for msg in messages[-6:]:
            msg["api_content"] = msg.get("content")
        messages[-1]["reasoning_content"] = "stale reasoning"
        original = copy.deepcopy(messages)
        e = engine()
        result = e.compress(messages)
        self.assertEqual(e.last_outcome, "compacted")
        self.assertEqual(messages, original)
        self.assertTrue(any(m.get("role") == "assistant" and m.get("content") == original[-1]["content"] for m in result))
        # Summary carrier never inherits raw api_content from an older turn
        carrier = next(m for m in result if is_compaction_summary_message(m))
        self.assertNotIn("api_content", carrier)

    def test_resume_with_only_one_historical_user_still_retains_it(self):
        messages = history()
        only = [m for m in messages if m["role"] == "user"][-1]["content"]
        sparse = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": only},
            {"role": "assistant", "content": "one"},
            {"role": "tool", "tool_call_id": "mid", "content": "raw tool body " * 100},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "pending", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}]},
        ]
        e = engine()
        self.assertEqual([sparse[i]["content"] for i in e._user_indices(sparse)], [only])
        result = e.compress(sparse)
        self.assertIn(e.last_outcome, ("compacted", "preserved"))
        self.assertTrue(any(m.get("role") == "user" and m.get("content") == only for m in result))

    def test_empty_and_short_histories_are_lossless_without_aux(self):
        for n in range(5):
            messages = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"row {i}"}
                        for i in range(n)]
            original = copy.deepcopy(messages)
            e = engine()
            self.assertIs(e.compress(messages), messages)
            self.assertEqual(messages, original)
            self.assertEqual(e.compression_count, 0)
        self.aux.assert_not_called()

    def test_single_latest_message_over_half_window_is_never_cut(self):
        e = ContextSteward(model="fixture", config_context_length=32_000)
        messages = history() + [{"role": "user", "content": "超长原文\n" * 20_000 + "END-OF-REQUEST"}]
        latest = messages[-1]["content"]
        self.assertGreater(estimate_messages_tokens_rough([messages[-1]]), e.context_length / 2)
        original = copy.deepcopy(messages)
        result = e.compress(messages)
        self.assertTrue(any(m.get("content") == latest for m in result))
        self.assertEqual(messages, original)
        self.assertLessEqual(estimate_messages_tokens_rough(result), estimate_messages_tokens_rough(messages))
        self.assertIn(e.last_outcome, ("compacted", "preserved"))

    def test_two_hundred_large_tool_results_preserve_current_request_and_tail(self):
        messages = history()
        messages[-2]["display_kind"] = "internal_notification"
        current = messages[-2]["content"]
        for i in range(200):
            messages.extend(call(i, 20_000))
        original = copy.deepcopy(messages)
        e = engine()
        result = e.compress(messages)
        self.assertEqual(e.last_outcome, "compacted")
        self.assertTrue(any(m.get("role") == "user" and m.get("content") == current for m in result))
        summary = next(m["content"] for m in result if is_compaction_summary_message(m))
        self.assertNotIn("## Unfinished User Request Restatement", summary)
        for tail in original[-6:]:
            self.assertTrue(any(m.get("content") == tail.get("content") and
                                m.get("tool_call_id") == tail.get("tool_call_id") and
                                m.get("tool_calls") == tail.get("tool_calls") for m in result))
        calls = {tc["id"] for m in result for tc in m.get("tool_calls", [])}
        for m in result:
            if m.get("role") == "tool":
                self.assertIn(m["tool_call_id"], calls)
        self.assertEqual(messages, original)

    def test_multilingual_code_fences_preserve_exact_bytes_in_both_modes(self):
        phrases = [
            "中文：保留  空格\n```python\nx = '你好'\n```\n",
            "日本語の依頼\n```json\n{\"金額\": 123}\n```",
            "العربية + français + emoji 🧪\n```sh\nprintf '%s\\n' café\n```",
        ]
        for mode in ("fresh", "checkpoint"):
            messages = history()
            for msg, text in zip([m for m in messages if m["role"] == "user"][-3:], phrases):
                msg["content"] = text
                msg["display_kind"] = "internal_notification"
            original = copy.deepcopy(messages)
            self.aux.return_value = response(mode)
            e = engine()
            result = e.compress(messages)
            self.assertEqual(e.last_mode, mode)
            self.assertEqual(e.last_outcome, "compacted")
            self.assertEqual(messages, original)
            for text in phrases:
                self.assertTrue(any(m.get("role") == "user" and m.get("content") == text for m in result))

    def test_three_successive_compactions_keep_requests_and_previous_facts(self):
        facts = "FACT: basalt-742; owner=Orion; deadline=2031-09-17"
        messages = history()
        e = engine()
        for generation in range(3):
            latest = f"Unfinished request generation={generation}; exact fact {facts}\n```py\nx = {generation}\n```"
            messages += history()[1:] + [{"role": "user", "display_kind": "internal_notification", "content": latest}]
            messages += call(1000 + generation)
            before = copy.deepcopy(messages)
            self.aux.return_value = response("checkpoint", goal=facts, unfinished=[latest])
            result = e.compress(messages, force=True)
            self.assertEqual(e.compression_count, generation + 1)
            self.assertEqual(e.last_mode, "checkpoint")
            self.assertTrue(any(m.get("role") == "user" and m.get("content") == latest for m in result))
            summary = next(m["content"] for m in result if is_compaction_summary_message(m))
            self.assertIn(facts, summary)
            self.assertNotIn("## Unfinished User Request Restatement", summary)
            prompt = self.aux.call_args.kwargs["messages"][0]["content"]
            if generation:
                previous = prompt.split("PREVIOUS HANDOFF:\n", 1)[1].split("SAMPLED RECORDS", 1)[0]
                self.assertIn(facts, previous)
            self.assertEqual(messages, before)
            messages = result
        self.assertEqual(self.aux.call_count, 3)

    def test_presentation_stripped_user_rows_survive_fresh_and_checkpoint(self):
        messages = history()
        for msg in [m for m in messages if m["role"] == "user"][-2:]:
            # Stripping display_metadata must not erase genuine "存在\n换行\u200b" bytes.
            msg["display_kind"] = "internal_notification"
            msg["display_metadata"] = {"ingress": "api_notification"}
        original = copy.deepcopy(messages)
        for mode in ("fresh", "checkpoint"):
            self.aux.return_value = response(mode)
            target = copy.deepcopy(messages)
            for msg in [m for m in target if m["role"] == "user"][-2:]:
                msg["display_kind"] = "internal_notification"
                msg["display_metadata"] = {"ingress": "api_notification"}
            e = engine()
            result = e.compress(target, force=True)
            self.assertIn(e.last_outcome, ("compacted", "preserved"))
            for msg in [m for m in original if m["role"] == "user"][-3:]:
                self.assertTrue(any(m.get("role") == "user" and m.get("content") == msg["content"] for m in result))
        self.assertEqual(messages, original)

    def test_known_synthetic_nudges_do_not_displace_recent_requests(self):
        from agent.context_compressor import COMPRESSION_CONTINUATION_USER_CONTENT
        messages = history()
        latest = messages[-2]["content"]
        messages[-2]["display_kind"] = "internal_notification"
        messages += [{"role": "user", "display_kind": "internal_notification",
                      "content": COMPRESSION_CONTINUATION_USER_CONTENT} for _ in range(10)]
        e = engine()
        selected = e._user_indices(messages)
        self.assertEqual(len(selected), 3)
        self.assertEqual(messages[selected[-1]]["content"], latest)

    def test_faults_execute_real_parent_and_preserve_all_required_messages(self):
        faults = {
            "timeout": TimeoutError("injected timeout"),
            "network": ConnectionError("injected connection error"),
            "json": {"choices": [{"message": {"content": "invalid JSON"}}]},
            "empty_response": {"choices": [{"message": {"content": ""}}]},
            "empty_handoff": {"choices": [{"message": {"content": '{"mode":"fresh","handoff":{}}'}}]},
        }
        real_parent = ContextCompressor.compress
        for name, fault in faults.items():
            with self.subTest(name=name):
                self.aux.reset_mock()
                self.aux.side_effect = [fault, GOOD_PARENT]
                messages = history() + [{"role": "user", "display_kind": "internal_notification",
                                         "content": f"LATEST-{name}-EXACT\n正在进行"}]
                original = copy.deepcopy(messages)
                e = engine()
                with patch.object(ContextCompressor, "compress", autospec=True, side_effect=real_parent) as parent:
                    result = e.compress(messages, force=True)
                parent.assert_called_once()
                self.assertGreaterEqual(self.aux.call_count, 2)
                self.assertEqual(e.last_mode, "checkpoint")
                self.assertEqual(e.last_outcome, "parent_fallback")
                for user in [m for m in messages if m["role"] == "user"][-3:]:
                    self.assertTrue(any(m.get("content") == user["content"] for m in result))
                self.assertEqual(messages, original)

    def test_real_parent_can_compact_diagnostic_ingress_without_stale_restatement(self):
        messages = history()
        users = [m for m in messages if m["role"] == "user"]
        for msg in users[3:]:
            msg["display_kind"] = "internal_notification"
        original = copy.deepcopy(messages)
        self.aux.side_effect = [ConnectionError("injected judge failure"), GOOD_PARENT]
        e = engine()
        result = e.compress(messages, force=True)
        self.assertEqual(e.last_outcome, "parent_fallback")
        self.assertLess(estimate_messages_tokens_rough(result), estimate_messages_tokens_rough(messages))
        for msg in users[-3:]:
            self.assertTrue(any(m.get("role") == "user" and m.get("content") == msg["content"] for m in result))
        carrier = next(m for m in result if is_compaction_summary_message(m))
        self.assertNotIn("## Unfinished User Request Restatement", carrier["content"])
        self.assertEqual(messages, original)

    def test_parent_fallback_cannot_rewrite_protected_tail(self):
        messages = history()
        damaged = copy.deepcopy(messages[-6:])
        damaged[-1]["content"] = "truncated assistant tail"
        self.aux.side_effect = ConnectionError("injected transport error")
        e = engine()
        with patch.object(ContextCompressor, "compress", return_value=damaged):
            result = e.compress(messages)
        self.assertIs(result, messages)
        self.assertEqual(e.last_outcome, "preserved")
        self.assertEqual(e.compression_count, 0)

    def test_finalization_cannot_rewrite_protected_tool_result(self):
        real_finalize = ContextCompressor._finalize_compressed
        messages = history() + call(1234, 20_000)
        original = copy.deepcopy(messages)
        def damage(instance, *args, **kwargs):
            result = real_finalize(instance, *args, **kwargs)
            result[-1]["content"] = "truncated tool tail"
            return result
        e = engine()
        with patch.object(ContextCompressor, "_finalize_compressed", autospec=True, side_effect=damage):
            result = e.compress(messages)
        self.assertIs(result, messages)
        self.assertEqual(messages, original)
        self.assertEqual(e.compression_count, 0)
        self.assertFalse(e._last_compression_made_progress)

    def test_real_parent_also_failing_keeps_every_original_row(self):
        for error in (TimeoutError("offline timeout"), ConnectionError("offline connection")):
            self.aux.side_effect = error
            messages = history() + [{"role": "user", "content": "LATEST-NEVER-DROP"}]
            original = copy.deepcopy(messages)
            e = engine()
            result = e.compress(messages, force=True)
            self.assertEqual(result, original)
            self.assertEqual(messages, original)
            self.assertEqual(e.last_outcome, "preserved")

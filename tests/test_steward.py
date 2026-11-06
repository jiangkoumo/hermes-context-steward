"""Real Hermes classes + fake auxiliary responses; no live model or user state."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent.auxiliary_client import AuxiliaryExplicitCancellation
from agent.context_compressor import ContextCompressor, is_compaction_summary_message
from agent.context_engine import ContextEngine
from agent.model_metadata import estimate_messages_tokens_rough

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "context_steward_test_plugin", ROOT / "__init__.py",
    submodule_search_locations=[str(ROOT)],
)
plugin = importlib.util.module_from_spec(spec)
# Normal package import for a directory plugin; no Hermes module is replaced.
import sys
sys.modules[spec.name] = plugin
spec.loader.exec_module(plugin)
ContextSteward = plugin.steward.ContextSteward


def response(mode="checkpoint", *, goal="Ship the fix", unfinished=None, **kwargs):
    handoff = {
        "goal": goal, "completed": ["Read src/app.py"],
        "in_progress": unfinished if unfinished is not None else [],
        "key_context": ["src/app.py; run python -m unittest; keep API stable"],
        "todo": [],
    }
    content = json.dumps({"mode": mode, "handoff": handoff})
    return {"choices": [{"finish_reason": "stop", "message": {"content": content}}], **kwargs}


def history():
    result = [{"role": "system", "content": "System instructions unchanged."}]
    for i in range(16):
        result.extend([
            {"role": "user", "content": f"request {i}: 请保留  空格\n原文", "message_uid": f"u{i}"},
            {"role": "assistant", "content": f"answer {i}: " + "old investigation details. " * 150,
             "message_uid": f"a{i}"},
        ])
    return result


def engine(**kwargs):
    return ContextSteward(model="test-model", config_context_length=100_000, **kwargs)


class StewardTests(unittest.TestCase):
    def setUp(self):
        self.messages = history()
        self.original = copy.deepcopy(self.messages)
        self.aux = patch("agent.context_compressor.call_llm", return_value=response()).start()
        self.addCleanup(patch.stopall)

    def assert_quotes(self, result, count=3):
        expected = [m["content"] for m in self.messages if m["role"] == "user"][-count:]
        actual = [m["content"] for m in result if m["role"] == "user"]
        for text in expected:
            self.assertIn(text, actual)
        self.assertEqual(self.messages, self.original)

    def test_diagnostic_latest_unfinished_request_survives_long_tool_loop(self):
        # The live api_server marks diagnostic ingress as internal_notification.
        # Only the first three historical requests lack that presentation tag.
        users = [m for m in self.messages if m["role"] == "user"]
        for msg in users[3:]:
            msg["display_kind"] = "internal_notification"
        latest = users[-1]["content"]
        for i in range(12):
            self.messages.extend([
                {"role": "assistant", "content": None, "tool_calls": [{
                    "id": f"loop_{i}", "type": "function",
                    "function": {"name": "terminal", "arguments": "{}"},
                }]},
                {"role": "tool", "tool_call_id": f"loop_{i}", "content": "tool details " * 100},
            ])
        self.original = copy.deepcopy(self.messages)
        self.aux.return_value = response("fresh", goal=f"Still processing {latest}")
        e = engine()
        result = e.compress(self.messages)
        self.assertEqual(e.last_outcome, "compacted")
        self.assertEqual(e.last_mode, "checkpoint")
        retained = [m.get("content") for m in result if m.get("role") == "user"]
        self.assertIn(latest, retained, "latest unfinished request must survive outside the summary")
        for msg in users[-3:]:
            self.assertIn(msg["content"], retained)
        carrier = next(m for m in result if is_compaction_summary_message(m))
        self.assertNotIn("## Unfinished User Request Restatement", carrier["content"])
        self.assertNotIn("request 2:", carrier["content"])
        for msg in self.messages[-6:]:
            self.assertTrue(any(m.get("content") == msg.get("content") and
                                m.get("tool_call_id") == msg.get("tool_call_id") for m in result))
        self.assertEqual(self.messages, self.original)

    def test_contract_and_exact_half_window(self):
        e = engine()
        self.assertIsInstance(e, ContextEngine)
        self.assertEqual(e.name, "context-steward")
        self.assertEqual(e.threshold_tokens, 50_000)
        self.assertFalse(e.should_compress(49_999))
        self.assertTrue(e.should_compress(50_000))
        e.model_thresholds = {"small": 0.9}
        e.update_model("small", 32_000)
        self.assertEqual(e.threshold_tokens, 16_000)
        e.update_model("large", 1_000_000)
        self.assertEqual(e.threshold_tokens, 500_000)

    def test_usage_reset_and_session_lifecycle(self):
        e = engine()
        e.on_session_start("test")
        e.update_from_response({"prompt_tokens": 51_000, "completion_tokens": 5})
        self.assertTrue(e.should_compress())
        self.assertEqual(e.get_status()["last_prompt_tokens"], 51_000)
        e.last_mode = "fresh"
        e.on_session_reset()
        self.assertEqual(e.last_prompt_tokens, 0)
        self.assertIsNone(e.last_mode)
        e.on_session_end("test", [])

    def test_checkpoint_one_call_and_verbatim_users(self):
        e = engine()
        result = e.compress(self.messages)
        self.assertEqual(e.last_mode, "checkpoint")
        self.assertEqual(e.last_outcome, "compacted")
        self.assertEqual(e.compression_count, 1)
        self.assertLess(estimate_messages_tokens_rough(result), estimate_messages_tokens_rough(self.messages))
        self.assertEqual(result[0], self.messages[0])
        self.assertTrue(any(is_compaction_summary_message(m) for m in result))
        self.assert_quotes(result)
        self.aux.assert_called_once()
        call = self.aux.call_args.kwargs
        self.assertEqual(call["task"], "compression")
        self.assertEqual(call["main_runtime"]["model"], "test-model")
        self.assertNotIn("model", call)
        self.assertNotIn("api_key", call)
        self.assertIn("request 15", call["messages"][0]["content"])

    def test_fresh_drops_old_answers_but_keeps_completed_reply(self):
        self.aux.return_value = response("fresh")
        e = engine()
        result = e.compress(self.messages)
        self.assertEqual(e.last_mode, "fresh")
        self.assertEqual(len(result), 6)  # system, handoff, 3 quotes, completed reply
        self.assertEqual(result[-1]["content"], self.messages[-1]["content"])
        self.assertFalse(any(m.get("content") == self.messages[2]["content"] for m in result))
        self.assert_quotes(result)
        self.aux.assert_called_once()

    def test_unfinished_handoff_forces_checkpoint(self):
        self.aux.return_value = response("fresh", unfinished=["Awaiting tests"])
        e = engine()
        e.compress(self.messages)
        self.assertEqual(e.last_mode, "checkpoint")
        self.aux.assert_called_once()

    def test_fresh_with_new_unanswered_user_is_checkpoint(self):
        self.messages.append({"role": "user", "content": "New task, not answered"})
        self.original = copy.deepcopy(self.messages)
        self.aux.return_value = response("fresh")
        e = engine()
        result = e.compress(self.messages)
        self.assertEqual(e.last_mode, "checkpoint")
        self.assert_quotes(result)

    def test_pending_tool_round_keeps_parallel_calls_and_results(self):
        self.messages.extend([
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_a", "type": "function", "function": {"name": "terminal", "arguments": "{}"}},
                {"id": "call_b", "type": "function", "function": {"name": "terminal", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "call_a", "content": "A done"},
            {"role": "tool", "tool_call_id": "call_b", "content": "B done"},
        ])
        self.original = copy.deepcopy(self.messages)
        self.aux.return_value = response("fresh")
        e = engine()
        result = e.compress(self.messages)
        self.assertEqual(e.last_mode, "checkpoint")
        self.assertEqual(result[-3:][0]["tool_calls"], self.messages[-3]["tool_calls"])
        self.assertEqual([m["tool_call_id"] for m in result if m["role"] == "tool"], ["call_a", "call_b"])
        self.assert_quotes(result)

    def test_invalid_mode_with_valid_handoff_becomes_checkpoint(self):
        self.aux.return_value = response("not-a-mode")
        e = engine()
        e.compress(self.messages)
        self.assertEqual(e.last_mode, "checkpoint")
        self.aux.assert_called_once()

    def test_malformed_json_calls_parent_checkpoint(self):
        self.aux.return_value = {"choices": [{"message": {"content": "{invalid"}}]}
        e = engine()
        with patch.object(ContextCompressor, "compress", return_value=self.messages[-6:]) as parent:
            result = e.compress(self.messages)
        self.assertEqual(e.last_mode, "checkpoint")
        self.assertEqual(e.last_outcome, "parent_fallback")
        self.assert_quotes(result)
        self.aux.assert_called_once()
        parent.assert_called_once()

    def test_aux_exception_calls_parent_and_preserves_if_parent_fails(self):
        self.aux.side_effect = RuntimeError("synthetic transport failure")
        e = engine()
        with patch.object(ContextCompressor, "compress", side_effect=RuntimeError("also failed")) as parent:
            result = e.compress(self.messages)
        self.assertIs(result, self.messages)
        self.assertEqual(e.last_mode, "checkpoint")
        self.assertEqual(e.last_outcome, "preserved")
        self.assertTrue(e.abort_on_summary_failure)
        parent.assert_called_once()
        self.assert_quotes(result)

    def test_real_parent_with_aux_offline_keeps_all_messages(self):
        # Executes the REAL parent pipeline too; all network dispatches are fake.
        self.aux.side_effect = RuntimeError("synthetic offline")
        e = engine()
        result = e.compress(self.messages, force=True)
        self.assertEqual(result, self.messages)
        self.assertEqual(self.messages, self.original)
        self.assertGreaterEqual(self.aux.call_count, 2)
        self.assertEqual(e.last_mode, "checkpoint")

    def test_fallback_rejects_loss_of_recent_quote(self):
        e = engine()
        self.aux.side_effect = RuntimeError("synthetic offline")
        with patch.object(ContextCompressor, "compress", return_value=[self.messages[-1]]):
            self.assertIs(e.compress(self.messages), self.messages)

    def test_truncated_response_is_never_committed(self):
        r = response("fresh")
        r["choices"][0]["finish_reason"] = "length"
        self.aux.return_value = r
        e = engine()
        with patch.object(ContextCompressor, "compress", return_value=self.messages):
            self.assertIs(e.compress(self.messages), self.messages)
        self.assertEqual(e.last_mode, "checkpoint")

    def test_noop_when_required_users_would_grow_context(self):
        self.messages = self.messages[-4:]
        self.messages.insert(0, {"role": "system", "content": "system"})
        self.original = copy.deepcopy(self.messages)
        e = engine(keep_user_messages=5)
        self.assertIs(e.compress(self.messages), self.messages)

    def test_nondefault_retention_and_model(self):
        e = engine(keep_user_messages=5, verdict_model="cheap-judge")
        result = e.compress(self.messages, focus_topic="Fix app.py")
        self.assert_quotes(result, 5)
        self.assertEqual(self.aux.call_args.kwargs["model"], "cheap-judge")
        self.assertEqual(e.summary_model, "")
        self.assertIn("Fix app.py", self.aux.call_args.kwargs["messages"][0]["content"])

    def test_summary_and_aux_input_are_redacted_without_changing_user_quotes(self):
        secret = "sk-" + "FAKE_TEST_ONLY_" * 4
        opaque = "q7X9" + "n4J2" + "m8L6"
        url_secret = "m7N2" + "q9K4"
        self.messages[2]["content"] += (
            f' API_KEY={secret}\npassword: {opaque}\n'
            f'https://example-user:{url_secret}@example.test?token=opaque_value'
        )
        self.original = copy.deepcopy(self.messages)
        self.aux.return_value = response(goal=f"Fix API_KEY={secret}\npassword: {opaque}")
        e = engine()
        result = e.compress(self.messages, memory_context=f"API_KEY={secret}")
        prompt = self.aux.call_args.kwargs["messages"][0]["content"]
        summary = next(m["content"] for m in result if is_compaction_summary_message(m))
        for text in (prompt, summary):
            self.assertNotIn(secret, text)
            self.assertNotIn(opaque, text)
            self.assertNotIn(url_secret, text)
            self.assertNotIn("token=opaque_value", text)
        self.assert_quotes(result)

    def test_prompt_is_bounded_and_contains_latest_messages(self):
        many = [{"role": "user", "content": f"row-{i}: " + "x" * 5000} for i in range(300)]
        e = engine()
        prompt = e._prompt(many, None, "")
        self.assertLess(len(prompt), 66_000)
        self.assertIn("row-299", prompt)
        self.assertIn("row-0", prompt)
        self.assertIn("omitted", prompt)

    def test_cancellation_does_not_fall_back(self):
        self.aux.side_effect = AuxiliaryExplicitCancellation()
        with patch.object(ContextCompressor, "compress") as parent:
            with self.assertRaises(AuxiliaryExplicitCancellation):
                engine().compress(self.messages)
        parent.assert_not_called()

    def test_repeated_identical_user_messages_require_distinct_copies(self):
        for msg in self.messages:
            if msg["role"] == "user":
                msg["content"] = "same request"
        self.original = copy.deepcopy(self.messages)
        e = engine()
        self.aux.side_effect = RuntimeError("synthetic offline")
        with patch.object(ContextCompressor, "compress", return_value=[self.messages[-2], self.messages[-1]]):
            self.assertIs(e.compress(self.messages), self.messages)
        self.assertEqual(e.last_outcome, "preserved")

    def test_invalid_handoff_schema_never_yields_fresh(self):
        from context_steward_test_plugin.steward import parse_verdict
        invalid = ["[]", '{"mode":"fresh","handoff":{}}',
                   '{"mode":"fresh","handoff":"untrusted free text"}']
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_verdict(raw)
        r = response("fresh", goal="x" * 2_001)
        mode, _ = parse_verdict(r["choices"][0]["message"]["content"])
        self.assertEqual(mode, "checkpoint")

    def test_partial_assistant_reply_cannot_start_fresh(self):
        self.messages[-1]["finish_reason"] = "length"
        self.original = copy.deepcopy(self.messages)
        self.aux.return_value = response("fresh")
        e = engine()
        result = e.compress(self.messages)
        self.assertEqual(e.last_mode, "checkpoint")
        self.assert_quotes(result)

    def test_rejected_finalization_restores_summary_and_count(self):
        e = engine()
        e._previous_summary = "previous checkpoint"
        with patch.object(ContextCompressor, "_finalize_compressed", return_value=[self.messages[-1]]):
            self.assertIs(e.compress(self.messages), self.messages)
        self.assertEqual(e._previous_summary, "previous checkpoint")
        self.assertEqual(e.compression_count, 0)
        self.assertFalse(e._last_compression_made_progress)
        self.assertFalse(e.should_compress(60_000))  # structural backoff

    def test_malformed_json_real_parent_summary_can_checkpoint(self):
        bad = {"choices": [{"message": {"content": "{invalid"}}]}
        good = {"choices": [{"finish_reason": "stop", "message": {"content": (
            "## Historical Task Snapshot\nrequest 15: 请保留  空格\\n原文\n"
            "## Goal\nShip the fix\n## Completed Actions\nRead src/app.py\n"
            "## Active State\nWorking on tests\n## Key Decisions\nKeep API stable"
        )}}]}
        self.aux.side_effect = [bad, good]
        e = engine(keep_user_messages=3)
        result = e.compress(self.messages, force=True)
        self.assertEqual(e.last_mode, "checkpoint")
        self.assertEqual(e.last_outcome, "parent_fallback")
        self.assertLess(estimate_messages_tokens_rough(result), estimate_messages_tokens_rough(self.messages))
        self.assert_quotes(result, 3)
        for protected in self.messages[-6:]:
            self.assertTrue(any(m.get("role") == protected.get("role") and
                                m.get("content") == protected.get("content") for m in result))
        self.assertEqual(self.aux.call_count, 2)

    def test_config_validation(self):
        for settings in ({"check_ratio": 0}, {"check_ratio": 1}, {"check_ratio": True},
                         {"check_ratio": float("nan")}, {"keep_user_messages": -1},
                         {"keep_user_messages": True}, {"verdict_model": 42}, {"typo": 1}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                ContextSteward.from_settings(settings)

    def test_fenced_json_accepts_crlf_and_optional_language(self):
        raw = response("fresh")["choices"][0]["message"]["content"]
        for wrapped in (f"```json\r\n{raw}\r\n```", f"```\n{raw}\n```", f"```JSON  \n{raw}\n``` "):
            with self.subTest(wrapped=wrapped):
                self.aux.return_value = {"choices": [{"message": {"content": wrapped}}]}
                e = engine()
                e.compress(self.messages)
                self.assertEqual(e.last_mode, "fresh")

    def test_resume_recovers_full_handoff_before_sampling(self):
        # Critical information is in the middle, beyond the 1200-char record cap.
        note = "old-note " * 250 + "RESUME_CRITICAL_DECISION" + " earlier-work" * 250
        self.aux.return_value = response(goal=note)
        first = engine().compress(self.messages)
        self.assertTrue(any(is_compaction_summary_message(m) for m in first))
        resumed = engine()
        self.assertIsNone(resumed._previous_summary)
        prompt = resumed._prompt(first + self.messages[1:], None, "")
        previous = prompt.split("PREVIOUS HANDOFF:\n", 1)[1].split("SAMPLED RECORDS", 1)[0]
        self.assertIn(note, previous)
        self.assertIn("RESUME_CRITICAL_DECISION", previous)
        self.assertIsNone(resumed._previous_summary)  # no state published by prompt preparation

    def test_empty_yaml_sections_default_but_invalid_config_does_not_load(self):
        from plugins.context_engine import _load_engine_from_dir
        for config in ({"context": None}, {"context": {"steward": None}}):
            with self.subTest(config=config), \
                 patch("hermes_cli.config.load_config_readonly", return_value=config), \
                 patch("hermes_cli.plugin_isolation.user_plugin_host", return_value=None):
                loaded = _load_engine_from_dir(ROOT)
                self.assertIsInstance(loaded, ContextEngine)
                self.assertEqual(loaded.check_ratio, 0.5)
        for config in ({"context": "bad"}, {"context": {"steward": {"check_ratio": 0}}}):
            with self.subTest(config=config), \
                 patch("hermes_cli.config.load_config_readonly", return_value=config), \
                 patch("hermes_cli.plugin_isolation.user_plugin_host", return_value=None):
                self.assertIsNone(_load_engine_from_dir(ROOT))

    def test_register_reads_context_section_without_writes(self):
        class Collector:
            def register_context_engine(self, obj):
                self.engine = obj
        ctx = Collector()
        with patch("hermes_cli.config.load_config_readonly", return_value={"context": {
            "steward": {"check_ratio": 0.4, "keep_user_messages": 2, "verdict_model": "cheap"}
        }}):
            plugin.register(ctx)
        self.assertEqual(ctx.engine.check_ratio, 0.4)
        self.assertEqual(ctx.engine.keep_user_messages, 2)
        self.assertEqual(ctx.engine.verdict_model, "cheap")

    def test_real_hermes_directory_loader(self):
        from plugins.context_engine import _load_engine_from_dir
        with patch("hermes_cli.config.load_config_readonly", return_value={}), \
             patch("hermes_cli.plugin_isolation.user_plugin_host", return_value=None):
            loaded = _load_engine_from_dir(ROOT)
        self.assertIsInstance(loaded, ContextEngine)
        self.assertEqual(loaded.name, "context-steward")
        loaded.update_model("test-model", 100_000)
        self.assertEqual(loaded.threshold_tokens, 50_000)

    def test_sqlite_archive_preserves_old_messages_and_search(self):
        # Actual SQLite/FTS and Hermes commit API, NOT a simulated database.
        from hermes_state import SessionDB
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as tmp:
            db = SessionDB(Path(tmp) / "state.db")
            try:
                db.create_session("steward-test", source="test")
                for msg in self.messages:
                    db.append_message("steward-test", role=msg["role"], content=msg["content"])
                held = db.get_messages_as_conversation("steward-test", include_row_ids=True)
                persisted_note = "a" * 850 + "SQLITE_RESUME_DECISION" + "b" * 650
                self.aux.return_value = response("fresh", goal=persisted_note)
                result = engine().compress(held)
                self.assertLess(len(result), len(held))
                db.archive_and_compact("steward-test", copy.deepcopy(result))
                live = db.get_messages("steward-test")
                all_rows = db.get_messages("steward-test", include_compacted=True)
                # Conversation projection strips trailing assistant whitespace;
                # compare the archive to the exact bytes originally persisted.
                old_content = self.messages[2]["content"]
                self.assertFalse(any(m["content"] == old_content for m in live))
                archived = [m for m in all_rows if m["content"] == old_content]
                self.assertEqual(len(archived), 1)
                self.assertEqual((archived[0]["active"], archived[0]["compacted"]), (0, 1))
                self.assertTrue(db.search_messages('"request 0"'))
                restored = db.get_messages_as_conversation("steward-test", include_row_ids=True)
                resumed = engine()
                self.assertIsNone(resumed._previous_summary)
                prompt = resumed._prompt(restored + self.messages[1:], None, "")
                previous = prompt.split("PREVIOUS HANDOFF:\n", 1)[1].split("SAMPLED RECORDS", 1)[0]
                self.assertIn(persisted_note, previous)
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()

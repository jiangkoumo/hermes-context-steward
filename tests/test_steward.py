"""Core behaviour, host-compatibility and host integration.

Runtime model calls are faked (``FakeLlm``); everything else — the Hermes
ContextEngine contract, the config loader, SQLite archival, FTS and the real
``session_search`` tool — executes for real.
"""
import ast
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.context_compressor import (
    LEGACY_SUMMARY_PREFIX, _SUMMARY_END_MARKER, is_compaction_summary_message,
)
from agent.context_engine import ContextEngine
from agent.plugin_llm import PluginLlmTrustError
from test_support import (
    ContextSteward, FakeLlm, FOOTER, ROOT, StewardCase, carrier_of, engine, history, plugin,
    steward, tool_round, verdict,
)

RUNTIME_PUBLIC_MODULES = {"agent.context_engine", "agent.plugin_llm", "hermes_cli.config"}


class ContractTests(unittest.TestCase):
    def test_runtime_imports_are_documented_modules_only(self):
        """The admission lint's rule, asserted statically for our own runtime files."""
        for name in ("steward.py", "__init__.py"):
            tree = ast.parse((ROOT / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and not node.level and node.module:
                    if node.module.split(".")[0] not in {"agent", "hermes_cli", "hermes_state"}:
                        continue
                    self.assertIn(node.module, RUNTIME_PUBLIC_MODULES, f"{name}:{node.lineno}")
                    for alias in node.names:
                        self.assertFalse(alias.name.startswith("_"), f"{name}:{node.lineno}")

    def test_name_and_contract(self):
        e = engine()
        self.assertIsInstance(e, ContextEngine)
        self.assertEqual(e.name, "context-steward")
        self.assertEqual(e.threshold_tokens, 50_000)
        self.assertFalse(e.should_compress(49_999))
        self.assertTrue(e.should_compress(50_000))
        self.assertEqual(e.get_status()["threshold_tokens"], 50_000)

    def test_carrier_is_recognised_by_the_host_and_constants_match(self):
        result = engine().compress(copy.deepcopy(history()), force=True)
        carrier = next(m for m in result if is_compaction_summary_message(m))
        self.assertTrue(carrier["content"].startswith(LEGACY_SUMMARY_PREFIX))
        self.assertEqual(steward.SUMMARY_PREFIX, LEGACY_SUMMARY_PREFIX)
        self.assertEqual(steward.SUMMARY_END_MARKER, _SUMMARY_END_MARKER)
        self.assertIn(FOOTER, carrier["content"])

    def test_usage_and_lifecycle(self):
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

    def test_judgment_is_skipped_below_the_check_line(self):
        llm = FakeLlm(verdict())
        e = engine(llm)
        e.update_from_response({"prompt_tokens": 10, "completion_tokens": 1})
        messages = copy.deepcopy(history())
        self.assertIs(e.compress(messages), messages)
        self.assertEqual(llm.calls, [])


class JudgmentTests(StewardCase):
    def test_checkpoint_keeps_users_and_tail_and_calls_llm_once(self):
        llm = FakeLlm(verdict("checkpoint"))
        e = engine(llm)
        result = e.compress(self.messages, force=True)
        self.assertEqual(e.last_mode, "checkpoint")
        self.assertEqual(e.last_outcome, "compacted")
        self.assertEqual(e.compression_count, 1)
        self.assertLess(steward.estimate_tokens(result), steward.estimate_tokens(self.messages))
        self.assertEqual(result[0], self.messages[0])
        self.assert_quotes(result)
        self.assertEqual(len(llm.calls), 1)
        self.assertEqual(llm.calls[0]["task"], "compression")
        self.assertEqual(llm.calls[0]["purpose"], "context-steward.judgment")
        self.assertNotIn("api_key", llm.calls[0])
        self.assertIn("request 15", llm.calls[0]["messages"][0]["content"])

    def test_fresh_keeps_completed_reply_and_drops_old_answers(self):
        llm = FakeLlm(verdict("fresh"))
        e = engine(llm)
        result = e.compress(self.messages, force=True)
        self.assertEqual(e.last_mode, "fresh")
        self.assertEqual(len(result), 6)
        self.assertEqual(result[-1]["content"], self.messages[-1]["content"])
        self.assertFalse(any(m.get("content") == self.messages[2]["content"] for m in result))
        self.assert_quotes(result)

    def test_unfinished_handoff_forces_checkpoint(self):
        e = engine(FakeLlm(verdict("fresh", unfinished=["Awaiting tests"])))
        e.compress(self.messages, force=True)
        self.assertEqual(e.last_mode, "checkpoint")

    def test_fresh_with_unanswered_user_is_checkpoint(self):
        self.messages.append({"role": "user", "content": "New task, not answered"})
        self.original = copy.deepcopy(self.messages)
        e = engine(FakeLlm(verdict("fresh")))
        result = e.compress(self.messages, force=True)
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
        e = engine(FakeLlm(verdict("fresh")))
        result = e.compress(self.messages, force=True)
        self.assertEqual(e.last_mode, "checkpoint")
        self.assertEqual([m["tool_call_id"] for m in result if m.get("role") == "tool"],
                         ["call_a", "call_b"])
        self.assert_quotes(result)

    def test_invalid_mode_with_valid_handoff_becomes_checkpoint(self):
        e = engine(FakeLlm(verdict("not-a-mode")))
        e.compress(self.messages, force=True)
        self.assertEqual(e.last_mode, "checkpoint")
        self.assertEqual(e.last_outcome, "compacted")

    def test_in_flight_tool_calls_survive_compaction(self):
        self.messages.extend([
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "pending", "type": "function",
                 "function": {"name": "terminal", "arguments": "{}"}}]},
        ])
        self.original = copy.deepcopy(self.messages)
        e = engine(FakeLlm(verdict("fresh")))
        result = e.compress(self.messages, force=True)
        self.assertEqual(e.last_mode, "checkpoint")
        self.assertEqual(result[-1].get("tool_calls"), self.messages[-1]["tool_calls"])

    def test_preflight_uses_a_rough_estimate(self):
        e = engine(window=1_000, check_ratio=0.5)
        small = [{"role": "user", "content": "hi" * 10}]
        large = [{"role": "user", "content": "x" * 4_000 * 3}]
        self.assertFalse(e.should_compress_preflight(small))
        self.assertTrue(e.should_compress_preflight(large))

    def test_diagnostic_latest_request_survives_long_tool_loop(self):
        users = [m for m in self.messages if m["role"] == "user"]
        for msg in users[3:]:
            msg["display_kind"] = "internal_notification"
        latest = users[-1]["content"]
        for i in range(12):
            self.messages.extend(tool_round(i, 400))
        self.original = copy.deepcopy(self.messages)
        e = engine(FakeLlm(verdict("fresh", goal=f"Still processing {latest}")))
        result = e.compress(self.messages, force=True)
        self.assertEqual(e.last_outcome, "compacted")
        retained = [m.get("content") for m in result if m.get("role") == "user"]
        self.assertIn(latest, retained)
        for msg in users[-3:]:
            self.assertIn(msg["content"], retained)
        for msg in self.messages[-6:]:
            self.assertTrue(any(m.get("content") == msg.get("content")
                                and m.get("tool_call_id") == msg.get("tool_call_id") for m in result))
        self.assertEqual(self.messages, self.original)


class FallbackTests(StewardCase):
    def test_trust_error_falls_back_to_the_plain_route(self):
        llm = FakeLlm(verdict("checkpoint"), trust_errors=1)
        e = engine(llm)
        e.compress(self.messages, force=True)
        self.assertEqual(e.last_outcome, "compacted")
        self.assertEqual(len(llm.calls), 2)
        self.assertEqual(llm.calls[0]["task"], "compression")
        self.assertNotIn("task", llm.calls[1])

    def test_malformed_json_degrades_to_a_mechanical_checkpoint(self):
        for bad in ("{invalid", json.dumps({"mode": "fresh", "handoff": {}}), ""):
            with self.subTest(bad=bad):
                e = engine(FakeLlm(bad))
                result = e.compress(copy.deepcopy(self.messages), force=True)
                self.assertEqual(e.last_outcome, "degraded")
                self.assertEqual(e.last_mode, "checkpoint")
                self.assert_quotes(result)
                carrier = next(m for m in result if is_compaction_summary_message(m))
                self.assertIn("Continue the latest user request", carrier["content"])
                self.assertIn(FOOTER, carrier["content"])
                self.assertLess(steward.estimate_tokens(result), steward.estimate_tokens(self.messages))

    def test_judgment_exception_degrades_and_keeps_users(self):
        for fault in (TimeoutError("timeout"), ConnectionError("offline"), RuntimeError("boom")):
            with self.subTest(fault=type(fault).__name__):
                e = engine(FakeLlm(fault))
                result = e.compress(copy.deepcopy(self.messages), force=True)
                self.assertEqual(e.last_outcome, "degraded")
                self.assert_quotes(result)
                self.assertTrue(all(m in result for m in [result[-1]]))

    def test_cannot_shrink_preserves_the_original(self):
        messages = history(pairs=1) + [{"role": "user", "content": "x" * 200_000}]
        e = engine(FakeLlm(verdict()), window=32_000)
        self.assertIs(e.compress(messages, force=True), messages)
        self.assertEqual(e.last_outcome, "preserved")
        self.assertEqual(e.compression_count, 0)
        self.assertFalse(e.should_compress(60_000))  # structural backoff

    def test_failed_judgment_sets_a_cooldown(self):
        e = engine(FakeLlm(ConnectionError("offline")))
        e.compress(self.messages, force=True)
        blocked, reason = e.should_compress_info(60_000)
        self.assertFalse(blocked)
        self.assertTrue(str(reason).startswith("cooldown"))


class RedactionTests(StewardCase):
    def test_prompt_and_handoff_are_redacted_but_user_quotes_are_verbatim(self):
        secret = "sk-" + "FAKE_TEST_ONLY_" * 4
        opaque = "q7X9" + "n4J2" + "m8L6"
        url_secret = "m7N2" + "q9K4"
        self.messages[2]["content"] += (
            f' API_KEY={secret}\npassword: {opaque}\n'
            f'https://example-user:{url_secret}@example.test?token=opaque_value'
        )
        self.original = copy.deepcopy(self.messages)
        llm = FakeLlm(verdict(goal=f"Fix API_KEY={secret}\npassword: {opaque}"))
        e = engine(llm)
        result = e.compress(self.messages, force=True, memory_context=f"API_KEY={secret}")
        prompt = llm.calls[0]["messages"][0]["content"]
        carrier = carrier_of(result)
        for text in (prompt, carrier):
            self.assertNotIn(secret, text)
            self.assertNotIn(opaque, text)
            self.assertNotIn(url_secret, text)
            self.assertNotIn("token=opaque_value", text)
        self.assert_quotes(result)

    def test_known_prefixes_and_bearer_tokens_are_masked(self):
        sample = ("ghp_" + "a" * 20 + " AKIAABCDEFGHIJKLMNOP "
                  "Bearer abcdefghijklmnopqrstuvwxyz hf_" + "b" * 20)
        cleaned = steward.scrub(sample)
        for leaked in ("ghp_", "AKIA", "Bearer abc", "hf_"):
            self.assertNotIn(leaked, cleaned)


class ResumeTests(unittest.TestCase):
    def test_previous_handoff_is_recovered_from_the_carrier(self):
        note = "old-note " * 250 + "RESUME_CRITICAL_DECISION" + " earlier-work" * 250
        first = engine(FakeLlm(verdict(goal=note))).compress(copy.deepcopy(history()), force=True)
        resumed = engine(FakeLlm(verdict()))
        self.assertIn(note, resumed._previous_handoff(first))
        prompt = resumed._prompt(first, None, "")
        previous = prompt.split("PREVIOUS HANDOFF:\n", 1)[1].split("SAMPLED RECORDS", 1)[0]
        self.assertIn("RESUME_CRITICAL_DECISION", previous)
        self.assertNotIn(FOOTER, previous)


class ConfigTests(unittest.TestCase):
    def test_config_validation(self):
        for settings in ({"check_ratio": 0}, {"check_ratio": 1}, {"check_ratio": True},
                         {"check_ratio": float("nan")}, {"keep_user_messages": -1},
                         {"keep_user_messages": True}, {"verdict_model": 42}, {"typo": 1}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                ContextSteward.from_settings(settings)

    def test_register_receives_the_host_llm_facade(self):
        class Collector:
            llm = FakeLlm(verdict())

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
        self.assertIs(ctx.engine._facade(), ctx.llm)

    def test_directory_loader_and_defaults(self):
        from plugins.context_engine import _load_engine_from_dir
        for config in ({}, {"context": None}, {"context": {"steward": None}}):
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


class HostIntegrationTests(StewardCase):
    def test_archive_commit_keeps_users_and_archive_stays_searchable(self):
        from hermes_state import SessionDB
        from tools.session_search_tool import session_search
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as tmp:
            db = SessionDB(Path(tmp) / "state.db")
            try:
                db.create_session("steward-test", source="test")
                marker = "vqmx" + "ztlr" + "dwkpha"
                early = self.messages[:10]
                for msg in early:
                    db.append_message("steward-test", role=msg["role"], content=msg["content"])
                db.append_message("steward-test", role="tool", content=f"L87\t{marker} abcxyz")
                for msg in self.messages[10:]:
                    db.append_message("steward-test", role=msg["role"], content=msg["content"])
                db.append_message("steward-test", role="user", content="latest request")
                db.append_message("steward-test", role="assistant", content="working")
                held = db.get_messages_as_conversation("steward-test", include_row_ids=True)
                result = engine(FakeLlm(verdict("checkpoint"))).compress(held, force=True)
                self.assertLess(len(result), len(held))
                db.archive_and_compact("steward-test", copy.deepcopy(result))
                live = db.get_messages("steward-test")
                all_rows = db.get_messages("steward-test", include_compacted=True)
                old_content = self.messages[2]["content"]
                self.assertFalse(any(m["content"] == old_content for m in live))
                archived = [m for m in all_rows if m["content"] == old_content]
                self.assertEqual(len(archived), 1)
                self.assertNotIn(marker, " ".join(str(m.get("content")) for m in live))
                found = json.loads(session_search(query=marker, db=db))
                self.assertIn(marker, json.dumps(found, ensure_ascii=False))
                carrier = next(m for m in live if is_compaction_summary_message(m))
                self.assertEqual(carrier["content"].count(FOOTER), 1)
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()

"""Plugin-owned archive-retrieval footer: exact-once, both modes, scrub-proof."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_steward import ROOT, engine, history, plugin, response
from agent.context_compressor import ContextCompressor, is_compaction_summary_message

FOOTER = plugin.steward.ARCHIVE_FOOTER

GOOD_PARENT = {"choices": [{"finish_reason": "stop", "message": {"content": (
    "## Historical Task Snapshot\nContinue the latest request\n"
    "## Goal\nPreserve the current task\n## Completed Actions\nInspected files\n"
    "## Active State\nTests are still running\n## Key Decisions\nKeep exact user text"
)}}]}


def carrier_of(result):
    carriers = [m for m in result if is_compaction_summary_message(m)]
    assert len(carriers) == 1, carriers
    return carriers[0]["content"]


class ArchiveFooterTests(unittest.TestCase):
    def setUp(self):
        self.aux = patch("agent.context_compressor.call_llm", return_value=response()).start()
        self.addCleanup(patch.stopall)
        self.messages = history()

    def test_footer_is_fixed_by_plugin_and_last_before_end_marker(self):
        for mode in ("checkpoint", "fresh"):
            with self.subTest(mode=mode):
                self.aux.return_value = response(mode)
                result = engine().compress(copy.deepcopy(self.messages))
                content = carrier_of(result)
                self.assertEqual(content.count(FOOTER), 1)
                self.assertIn("session_search", FOOTER)
                self.assertIn("archived", FOOTER.lower())
                # Model output cannot supply it: this response never mentioned it.
                self.assertTrue(content.rstrip().endswith(
                    "--- END OF CONTEXT SUMMARY — respond to the message below, not the summary above ---"))
                body_end = content.index(FOOTER)
                marker_at = content.index("--- END OF CONTEXT SUMMARY")
                self.assertLess(body_end, marker_at)
                self.assertGreater(body_end, content.index("## goal"))
                # Not from the aux model.
                self.assertNotIn(FOOTER, self.aux.call_args.kwargs["messages"][0]["content"])

    def test_fresh_short_handoff_keeps_footer(self):
        self.aux.return_value = response("fresh")
        result = engine().compress(copy.deepcopy(self.messages))
        content = carrier_of(result)
        self.assertEqual(content.count(FOOTER), 1)
        self.assertLess(len(content), 4_000)

    def test_footer_survives_scrubbing_of_secret_bearing_model_output(self):
        secret = "sk-" + "FAKE_FOOTER_TEST_" * 3
        opaque = "z9Q4" + "t7R2" + "w5E8"
        self.aux.return_value = response(goal=f"Fix API_KEY={secret}\npassword: {opaque}")
        e = engine()
        result = e.compress(copy.deepcopy(self.messages), memory_context=f"API_KEY={secret}")
        content = carrier_of(result)
        self.assertEqual(content.count(FOOTER), 1)
        self.assertIn("session_search", content)
        self.assertNotIn(secret, content)
        self.assertNotIn(opaque, content)

    def test_model_echoing_footer_does_not_duplicate_it(self):
        self.aux.return_value = response(goal=f"Goal text\n{FOOTER}")
        result = engine().compress(copy.deepcopy(self.messages))
        self.assertEqual(carrier_of(result).count(FOOTER), 1)

    def test_three_compactions_keep_exactly_one_footer_each(self):
        e = engine()
        messages = copy.deepcopy(self.messages)
        for generation in range(3):
            messages += history()[1:] + [
                {"role": "user", "display_kind": "internal_notification",
                 "content": f"Unfinished request {generation}"}]
            messages.append({"role": "assistant", "content": "still working"})
            self.aux.return_value = response("checkpoint", unfinished=[f"request {generation}"])
            result = e.compress(messages, force=True)
            content = carrier_of(result)
            self.assertEqual(content.count(FOOTER), 1, f"generation {generation}")
            # Prompt hygiene: the hint is not fed back as previous handoff.
            self.assertNotIn(FOOTER, self.aux.call_args.kwargs["messages"][0]["content"])
            messages = result
        self.assertEqual(e.compression_count, 3)

    def test_parent_fallback_carrier_gets_one_footer(self):
        self.aux.side_effect = [ConnectionError("injected judge failure"), GOOD_PARENT]
        result = engine().compress(copy.deepcopy(self.messages), force=True)
        self.assertEqual(carrier_of(result).count(FOOTER), 1)

    def test_committed_carrier_keeps_footer_and_archive_stays_searchable(self):
        # Real host commit API + real SQLite/FTS + the real session_search tool.
        from hermes_state import SessionDB
        from tools.session_search_tool import session_search
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as tmp:
            db = SessionDB(Path(tmp) / "state.db")
            try:
                db.create_session("footer-test", source="test")
                marker = "vqmx" + "ztlr" + "dwkpha"
                early = self.messages[:10]
                for msg in early:
                    db.append_message("footer-test", role=msg["role"], content=msg["content"])
                db.append_message("footer-test", role="tool", content=f"L87\t{marker} abcxyz")
                for msg in self.messages[10:]:
                    db.append_message("footer-test", role=msg["role"], content=msg["content"])
                db.append_message("footer-test", role="user", content="latest request")
                db.append_message("footer-test", role="assistant", content="working")
                held = db.get_messages_as_conversation("footer-test", include_row_ids=True)
                result = engine().compress(held, force=True)
                carrier = next(m["content"] for m in result if is_compaction_summary_message(m))
                self.assertNotIn(marker, carrier)
                self.assertEqual(carrier.count(FOOTER), 1)
                db.archive_and_compact("footer-test", copy.deepcopy(result))
                live = db.get_messages("footer-test")
                committed = next(m["content"] for m in live if is_compaction_summary_message(m))
                self.assertEqual(committed.count(FOOTER), 1)
                self.assertNotIn(marker, " ".join(str(m.get("content")) for m in live))
                found = json.loads(session_search(query=marker, db=db))
                self.assertIn(marker, json.dumps(found, ensure_ascii=False))
            finally:
                db.close()

    def test_preserved_context_adds_no_footer(self):
        self.aux.side_effect = RuntimeError("offline")
        result = engine().compress(copy.deepcopy(self.messages), force=True)
        self.assertFalse(any(FOOTER in str(m.get("content") or "") for m in result))


if __name__ == "__main__":
    unittest.main()

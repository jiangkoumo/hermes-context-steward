"""Plugin-owned archive-retrieval footer: exact-once, both modes, scrub-proof."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from agent.context_compressor import is_compaction_summary_message
from test_support import (
    FakeLlm, FOOTER, ROOT, StewardCase, carrier_of, engine, history, steward, verdict,
)


class ArchiveFooterTests(StewardCase):
    def test_footer_is_plugin_owned_last_and_exactly_once(self):
        for mode in ("checkpoint", "fresh"):
            with self.subTest(mode=mode):
                llm = FakeLlm(verdict(mode))
                result = engine(llm).compress(copy.deepcopy(self.messages), force=True)
                content = carrier_of(result)
                self.assertEqual(content.count(FOOTER), 1)
                self.assertIn("session_search", FOOTER)
                self.assertTrue(content.rstrip().endswith(steward.SUMMARY_END_MARKER))
                self.assertLess(content.index(FOOTER), content.index(steward.SUMMARY_END_MARKER))
                self.assertGreater(content.index(FOOTER), content.index("## goal"))
                # Never supplied by the model: this prompt/response pair lacks it.
                self.assertNotIn(FOOTER, llm.calls[0]["messages"][0]["content"])

    def test_fresh_short_handoff_keeps_footer(self):
        result = engine(FakeLlm(verdict("fresh"))).compress(copy.deepcopy(self.messages), force=True)
        content = carrier_of(result)
        self.assertEqual(content.count(FOOTER), 1)
        self.assertLess(len(content), 6_000)

    def test_footer_survives_scrubbing_of_secret_bearing_output(self):
        secret = "sk-" + "FAKE_FOOTER_TEST_" * 3
        opaque = "z9Q4" + "t7R2" + "w5E8"
        e = engine(FakeLlm(verdict(goal=f"Fix API_KEY={secret}\npassword: {opaque}")))
        result = e.compress(copy.deepcopy(self.messages), force=True,
                            memory_context=f"API_KEY={secret}")
        content = carrier_of(result)
        self.assertEqual(content.count(FOOTER), 1)
        self.assertIn("session_search", content)
        self.assertNotIn(secret, content)
        self.assertNotIn(opaque, content)

    def test_model_echoing_footer_does_not_duplicate_it(self):
        result = engine(FakeLlm(verdict(goal=f"Goal text\n{FOOTER}"))).compress(
            copy.deepcopy(self.messages), force=True)
        self.assertEqual(carrier_of(result).count(FOOTER), 1)

    def test_three_compactions_keep_exactly_one_footer_each(self):
        llm = FakeLlm(verdict("checkpoint", unfinished=["still going"]))
        e = engine(llm)
        messages = copy.deepcopy(self.messages)
        for generation in range(3):
            messages += history()[1:] + [{"role": "user", "display_kind": "internal_notification",
                                          "content": f"Unfinished request {generation}"}]
            messages.append({"role": "assistant", "content": "still working"})
            result = e.compress(messages, force=True)
            self.assertEqual(carrier_of(result).count(FOOTER), 1, f"generation {generation}")
            self.assertNotIn(FOOTER, llm.calls[-1]["messages"][0]["content"])
            messages = result
        self.assertEqual(e.compression_count, 3)

    def test_degraded_carrier_has_one_footer(self):
        result = engine(FakeLlm(ConnectionError("offline"))).compress(
            copy.deepcopy(self.messages), force=True)
        self.assertEqual(carrier_of(result).count(FOOTER), 1)

    def test_preserved_context_adds_no_footer(self):
        # Every row is retained (recent users + tail cover the whole transcript), so a
        # carrier would only add tokens: the original list must come back untouched.
        messages = history(pairs=2) + [{"role": "user", "content": "y" * 200_000}]
        result = engine(FakeLlm(verdict()), window=32_000).compress(messages, force=True)
        self.assertIs(result, messages)
        self.assertFalse(any(FOOTER in str(m.get("content") or "") for m in result))

    def test_committed_carrier_keeps_footer_and_archive_is_searchable(self):
        from hermes_state import SessionDB
        from tools.session_search_tool import session_search
        with tempfile.TemporaryDirectory(dir=ROOT / ".tmp") as tmp:
            db = SessionDB(Path(tmp) / "state.db")
            try:
                db.create_session("footer-test", source="test")
                marker = "vqmx" + "ztlr" + "dwkpha"
                early = self.messages[:10]
                for message in early:
                    db.append_message("footer-test", role=message["role"], content=message["content"])
                db.append_message("footer-test", role="tool", content=f"L87\t{marker} abcxyz")
                for message in self.messages[10:]:
                    db.append_message("footer-test", role=message["role"], content=message["content"])
                db.append_message("footer-test", role="user", content="latest request")
                db.append_message("footer-test", role="assistant", content="working")
                held = db.get_messages_as_conversation("footer-test", include_row_ids=True)
                result = engine(FakeLlm(verdict())).compress(held, force=True)
                self.assertEqual(carrier_of(result).count(FOOTER), 1)
                self.assertNotIn(marker, carrier_of(result))
                db.archive_and_compact("footer-test", copy.deepcopy(result))
                live = db.get_messages("footer-test")
                committed = next(m["content"] for m in live if is_compaction_summary_message(m))
                self.assertEqual(committed.count(FOOTER), 1)
                self.assertNotIn(marker, " ".join(str(m.get("content")) for m in live))
                found = json.loads(session_search(query=marker, db=db))
                self.assertIn(marker, json.dumps(found, ensure_ascii=False))
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()

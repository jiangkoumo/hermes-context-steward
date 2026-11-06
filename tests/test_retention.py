"""Recent-turn retention: whole turns verbatim, tool payloads trimmed, bounded by tokens."""
import copy
import json
import unittest

from test_support import (
    ContextSteward, FakeLlm, StewardCase, carrier_of, engine, steward, verdict,
)


def conversation(turns=20, reply_chars=600, tool_rounds=1, tool_chars=4_000):
    messages = [{"role": "system", "content": "System instructions unchanged."}]
    for index in range(turns):
        messages.append({"role": "user", "content": f"T{index}: please continue task {index}"})
        for round_index in range(tool_rounds):
            call_id = f"c{index}_{round_index}"
            messages.append({"role": "assistant", "content": None, "tool_calls": [{
                "id": call_id, "type": "function",
                "function": {"name": "terminal", "arguments": json.dumps({"command": f"step {index}"})}}]})
            messages.append({"role": "tool", "tool_call_id": call_id,
                             "content": f"OUT{index}\n" + "payload " * (tool_chars // 8)})
        messages.append({"role": "assistant", "content": f"A{index}: " + "detail " * (reply_chars // 7)})
    return messages


class RecentTurnTests(StewardCase):
    def setUp(self):
        self.messages = conversation()
        self.original = copy.deepcopy(self.messages)

    def test_default_keeps_the_newest_whole_turns(self):
        e = engine(FakeLlm(verdict()), window=1_000_000, check_ratio=0.3)
        result = e.compress(self.messages, force=True)
        self.assertEqual(e.last_outcome, "compacted")
        self.assertLess(steward.estimate_tokens(result), steward.estimate_tokens(self.messages))
        retained = [m.get("content") for m in result]
        for index in range(len(range(20)) - e.keep_recent_turns, 20):
            self.assertIn(f"T{index}: please continue task {index}", retained)
            self.assertTrue(any(str(c or "").startswith(f"A{index}:") for c in retained),
                            f"assistant reply for turn {index} must survive verbatim")
        self.assertNotIn("T0: please continue task 0", retained)
        self.assertIn(steward.ARCHIVE_FOOTER, carrier_of(result))
        self.assertEqual(self.messages, self.original)

    def test_keep_recent_turns_is_configurable(self):
        for turns in (1, 3, 12):
            with self.subTest(turns=turns):
                e = engine(FakeLlm(verdict()), window=1_000_000, keep_recent_turns=turns,
                           keep_recent_tokens=10_000_000)
                result = e.compress(copy.deepcopy(self.messages), force=True)
                users = [m["content"] for m in result if m.get("role") == "user"
                         and str(m.get("content")).startswith("T")]
                self.assertEqual(len(users), turns)
                self.assertEqual(users, [f"T{i}: please continue task {i}"
                                         for i in range(20 - turns, 20)])

    def test_zero_turns_falls_back_to_the_user_quote_floor(self):
        e = engine(FakeLlm(verdict()), window=1_000_000, keep_recent_turns=0,
                   keep_user_messages=3)
        result = e.compress(copy.deepcopy(self.messages), force=True)
        users = [m["content"] for m in result if m.get("role") == "user"
                 and str(m.get("content")).startswith("T")]
        self.assertEqual(len(users), 3)

    def test_token_budget_bounds_the_window(self):
        e = engine(FakeLlm(verdict()), window=1_000_000, keep_recent_turns=20,
                   keep_recent_tokens=3_000)
        result = e.compress(copy.deepcopy(self.messages), force=True)
        self.assertLess(e.keep_recent_token_budget(), 20 * 3_000)
        users = [m for m in result if m.get("role") == "user"
                 and str(m.get("content")).startswith("T")]
        self.assertGreaterEqual(len(users), 1)
        self.assertLess(len(users), 20)

    def test_derived_budget_formula(self):
        for window, ratio, expected in ((1_000_000, 0.3, 45_000), (8_192, 0.3, 12_000),
                                        (2_000_000, 0.5, 60_000)):
            with self.subTest(window=window, ratio=ratio):
                e = engine(window=window, check_ratio=ratio)
                self.assertEqual(e.keep_recent_token_budget(), expected)

    def test_tool_payloads_are_trimmed_but_structure_kept(self):
        e = engine(FakeLlm(verdict()), window=1_000_000, keep_recent_turns=6)
        result = e.compress(self.messages, force=True)
        tools = [m for m in result if m.get("role") == "tool"]
        self.assertGreater(len(tools), 3)
        trimmed = [m for m in tools if "trimmed by context steward" in str(m.get("content"))]
        self.assertTrue(trimmed, "older retained tool output should be stubbed")
        # The newest tool results and anything in the protected tail stay intact.
        for message in tools[-steward.KEEP_UNTRIMMED_TOOL_RESULTS:]:
            self.assertNotIn("trimmed by context steward", str(message.get("content")))
        calls = {c["id"] for m in result for c in m.get("tool_calls") or []}
        for message in result:
            if message.get("role") == "tool":
                self.assertIn(message["tool_call_id"], calls)
        self.assertLess(steward.estimate_tokens(result), steward.estimate_tokens(self.messages))

    def test_user_text_is_never_trimmed(self):
        e = engine(FakeLlm(verdict()), window=1_000_000)
        result = e.compress(self.messages, force=True)
        originals = {m["content"] for m in self.messages if m.get("role") == "user"}
        retained = [m for m in result if m.get("role") == "user" and m.get("content") in originals]
        self.assertEqual(len(retained), e.keep_recent_turns)
        for message in retained:
            self.assertIn(message["content"], originals)
            self.assertNotIn("trimmed by context steward", message["content"])


class RetentionConfigTests(unittest.TestCase):
    def test_defaults_and_validation(self):
        e = ContextSteward()
        self.assertEqual(e.keep_recent_turns, steward.KEEP_RECENT_TURNS)
        self.assertEqual(e.keep_recent_tokens, 0)
        for kwargs in ({"keep_recent_turns": -1}, {"keep_recent_turns": True},
                       {"keep_recent_turns": "3"}, {"keep_recent_tokens": -5},
                       {"keep_recent_tokens": 1.5}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                ContextSteward(**kwargs)

    def test_from_settings_accepts_the_keys(self):
        e = ContextSteward.from_settings(
            {"keep_recent_turns": 4, "keep_recent_tokens": 9000}, compression={"threshold": 0.3})
        self.assertEqual(e.keep_recent_turns, 4)
        self.assertEqual(e.keep_recent_tokens, 9000)
        e.update_model("m", 1_000_000)
        self.assertEqual(e.keep_recent_token_budget(), 9000)

    def test_clone_carries_the_settings(self):
        e = ContextSteward(keep_recent_turns=5, keep_recent_tokens=1234)
        clone = e.clone_for_agent()
        self.assertEqual((clone.keep_recent_turns, clone.keep_recent_tokens), (5, 1234))


if __name__ == "__main__":
    unittest.main()

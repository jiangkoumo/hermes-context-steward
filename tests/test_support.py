"""Shared fixtures for the plugin's tests (not a test module itself).

These helpers may import Hermes internals: the admission lint skips ``test_*.py``,
so tests can verify the plugin's host compatibility directly while the runtime
code stays on public surfaces only.
"""
import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agent.plugin_llm import PluginLlmTrustError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "context_steward_test_plugin", ROOT / "__init__.py",
    submodule_search_locations=[str(ROOT)],
)
plugin = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = plugin
_spec.loader.exec_module(plugin)
steward = plugin.steward
ContextSteward = steward.ContextSteward
FOOTER = steward.ARCHIVE_FOOTER


class FakeLlm:
    """Stands in for ctx.llm; records calls and replays scripted responses."""

    def __init__(self, *responses, trust_errors=0):
        self.responses = list(responses)
        self.calls = []
        self.trust_errors = trust_errors

    def complete(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        if self.trust_errors:
            self.trust_errors -= 1
            raise PluginLlmTrustError("override not granted")
        if not self.responses:
            raise RuntimeError("no scripted response")
        item = self.responses[0] if len(self.responses) == 1 else self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(text=item, provider="fake", model="fake", usage=None, audit={})


def verdict(mode="checkpoint", *, goal="Ship the fix", unfinished=None, todo=None, **fields):
    handoff = {
        "goal": goal,
        "completed": ["Read src/app.py"],
        "in_progress": list(unfinished or []),
        "key_context": ["src/app.py; run python -m unittest; keep API stable"],
        "todo": list(todo or []),
    }
    handoff.update(fields)
    return json.dumps({"mode": mode, "handoff": handoff}, ensure_ascii=False)


def history(pairs=16, filler=150):
    messages = [{"role": "system", "content": "System instructions unchanged."}]
    for i in range(pairs):
        messages.extend([
            {"role": "user", "content": f"request {i}: 请保留  空格\n原文", "message_uid": f"u{i}"},
            {"role": "assistant", "content": f"answer {i}: " + "old investigation details. " * filler,
             "message_uid": f"a{i}"},
        ])
    return messages


def engine(llm=None, *, window=100_000, **settings):
    e = ContextSteward(model="test-model", llm=llm or FakeLlm(verdict()), **settings)
    e.update_model("test-model", window)
    return e


def tool_round(index, output_chars=8_000):
    return [
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": f"call_{index}", "type": "function",
            "function": {"name": "terminal", "arguments": json.dumps({"command": f"check-{index}"})},
        }]},
        {"role": "tool", "tool_call_id": f"call_{index}",
         "content": f"output-{index}\n" + "detail " * (output_chars // 7)},
    ]


def carrier_of(result):
    carriers = [m for m in result if steward._is_summary(m)]
    assert len(carriers) == 1, carriers
    return carriers[0]["content"]


class StewardCase(unittest.TestCase):
    """Base class: fresh history, and no reliance on any live model."""

    def setUp(self):
        self.messages = history()
        self.original = copy.deepcopy(self.messages)

    def assert_quotes(self, result, count=3):
        expected = [m["content"] for m in self.messages if m["role"] == "user"][-count:]
        actual = [m["content"] for m in result if m["role"] == "user"]
        for text in expected:
            self.assertIn(text, actual)
        self.assertEqual(self.messages, self.original)


__all__ = ["ContextSteward", "FakeLlm", "FOOTER", "ROOT", "StewardCase", "carrier_of",
           "engine", "history", "plugin", "steward", "tool_round", "verdict"]

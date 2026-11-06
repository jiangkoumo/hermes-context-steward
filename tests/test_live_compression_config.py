"""Execute Hermes's real Desktop/TUI live-config updater, never a copied shim.

The lightweight agent is a fixture; neither the updater nor its threshold/window
resolution helpers are mocked. No model requests or production config writes.
"""
from types import SimpleNamespace
import unittest

from tui_gateway.session_compression import _apply_live_compression_config
from test_support import engine


class LiveCompressionConfigTests(unittest.TestCase):
    def setUp(self):
        self.engine = engine(window=1_000_000, check_ratio=0.3)
        self.agent = SimpleNamespace(
            context_compressor=self.engine, model="test-model", provider="custom",
            base_url="https://context-steward.invalid/v1", _config_context_length=None,
        )

    def config(self, ratio, window, **compression):
        return {
            "model": {"default": self.agent.model, "provider": self.agent.provider,
                      "base_url": self.agent.base_url, "context_length": window},
            "compression": {"threshold": ratio, "threshold_tokens": None, **compression},
        }

    def test_real_updater_changes_threshold_window_and_next_decision(self):
        # Read the original trigger first: the hot reload must invalidate it.
        self.assertEqual(self.engine.threshold_tokens, 300_000)
        _apply_live_compression_config(self.agent, self.config(0.4, 80_000))
        self.assertEqual(self.engine.context_length, 80_000)
        self.assertEqual(self.engine.threshold_percent, 0.4)
        self.assertEqual(self.engine.threshold_tokens, 32_000)
        self.assertFalse(self.engine.should_compress(31_999))
        self.assertTrue(self.engine.should_compress(32_000))
        self.assertEqual(self.engine.get_status()["threshold_tokens"], 32_000)
        self.engine.update_from_response({"prompt_tokens": 31_999})
        self.assertFalse(self.engine.should_compress())
        self.engine.update_from_response({"prompt_tokens": 32_000})
        self.assertTrue(self.engine.should_compress())

    def test_repeated_updates_adopt_lower_and_higher_thresholds(self):
        for ratio, window, expected in ((0.2, 32_000, 6_400), (0.6, 128_000, 76_800),
                                        (0.3, 1_000_000, 300_000)):
            with self.subTest(ratio=ratio, window=window):
                _apply_live_compression_config(self.agent, self.config(ratio, window))
                self.assertEqual(self.engine.threshold_tokens, expected)
                self.assertFalse(self.engine.should_compress(expected - 1))
                self.assertTrue(self.engine.should_compress(expected))

    def test_cap_applies_to_new_ratio_and_window_and_can_be_disabled(self):
        _apply_live_compression_config(self.agent, self.config(0.5, 100_000, threshold_tokens="12000"))
        self.assertEqual(self.engine.threshold_tokens_cap, 12_000)
        self.assertEqual(self.engine.threshold_tokens, 12_000)
        self.assertTrue(self.engine.should_compress(12_000))
        _apply_live_compression_config(self.agent, self.config(0.5, 100_000, threshold_tokens=None))
        self.assertIsNone(self.engine.threshold_tokens_cap)
        self.assertEqual(self.engine.threshold_tokens, 50_000)

    def test_cap_normalisation_matches_upstream(self):
        # Compare semantics, including int-convertible strings and non-positive caps.
        from agent.context_compressor import ContextCompressor
        for raw in (None, 0, -5, "0", "12000", "1_000", "invalid", 12000.8, True, False, {}):
            with self.subTest(raw=raw):
                expected = ContextCompressor._coerce_threshold_tokens_cap(raw)
                self.assertEqual(self.engine._coerce_threshold_tokens_cap(raw), expected)
                _apply_live_compression_config(self.agent, self.config(0.5, 100_000, threshold_tokens=raw))
                self.assertEqual(self.engine.threshold_tokens, min(50_000, expected) if expected else 50_000)

    def test_cap_above_window_never_binds_before_the_ratio(self):
        _apply_live_compression_config(self.agent, self.config(0.3, 8_192, threshold_tokens=2_000_000))
        self.assertEqual(self.engine.threshold_tokens, int(8_192 * 0.3))
        self.assertTrue(self.engine.should_compress(int(8_192 * 0.3)))

    def test_removing_cap_key_restores_the_upstream_default(self):
        from hermes_cli.config_defaults import DEFAULT_CONFIG
        _apply_live_compression_config(self.agent, self.config(0.3, 100_000, threshold_tokens=1_000))
        cfg = self.config(0.3, 100_000)
        del cfg["compression"]["threshold_tokens"]
        _apply_live_compression_config(self.agent, cfg)
        default = self.engine._coerce_threshold_tokens_cap(DEFAULT_CONFIG["compression"].get("threshold_tokens"))
        self.assertEqual(self.engine.threshold_tokens, min(30_000, default) if default else 30_000)

    def test_public_model_update_keeps_the_hot_reloaded_ratio_and_cap(self):
        _apply_live_compression_config(self.agent, self.config(0.4, 80_000, threshold_tokens=70_000))
        self.engine.update_model("other-model", 200_000)
        self.assertEqual(self.engine.check_ratio, 0.4)
        self.assertEqual(self.engine.threshold_tokens, 70_000)

    def test_clone_keeps_live_ratio_cap_and_window_without_sharing_state(self):
        _apply_live_compression_config(self.agent, self.config(0.4, 80_000, threshold_tokens=70_000))
        clone = self.engine.clone_for_agent()
        self.assertEqual(clone.context_length, 80_000)
        self.assertEqual(clone.check_ratio, 0.4)
        self.assertEqual(clone.threshold_tokens_cap, 70_000)
        self.assertEqual(clone.threshold_tokens, 32_000)
        clone.update_model("other-model", 200_000)
        self.assertEqual(clone.threshold_tokens, 70_000)
        self.assertEqual(self.engine.context_length, 80_000)
        self.assertEqual(self.engine.threshold_tokens, 32_000)

    def test_removing_threshold_key_restores_the_host_default(self):
        _apply_live_compression_config(self.agent, self.config(0.2, 100_000))
        self.assertEqual(self.engine.threshold_tokens, 20_000)
        cfg = self.config(0.2, 100_000)
        del cfg["compression"]["threshold"]
        _apply_live_compression_config(self.agent, cfg)
        self.assertEqual(self.engine.check_ratio, 0.5)
        self.assertEqual(self.engine.threshold_tokens, 50_000)

    def test_clearing_window_pin_restores_the_last_public_model_window(self):
        _apply_live_compression_config(self.agent, self.config(0.3, 32_000))
        self.assertEqual(self.engine.context_length, 32_000)
        cfg = self.config(0.3, 32_000)
        del cfg["model"]["context_length"]
        _apply_live_compression_config(self.agent, cfg)
        self.assertIsNone(self.agent._config_context_length)
        self.assertEqual(self.engine.context_length, 1_000_000)
        self.assertEqual(self.engine.threshold_tokens, 300_000)


if __name__ == "__main__":
    unittest.main()

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import usage_estimate as ue


class TestPricingResolution(unittest.TestCase):
    def test_exact_match(self):
        self.assertEqual(ue._resolve_pricing_key("claude-opus-4-8"), "claude-opus-4-8")

    def test_dated_model_id_matches_base_entry(self):
        self.assertEqual(
            ue._resolve_pricing_key("claude-haiku-4-5-20251001"), "claude-haiku-4-5"
        )

    def test_unknown_model_returns_none(self):
        self.assertIsNone(ue._resolve_pricing_key("gpt-codex-99"))
        self.assertIsNone(ue._resolve_pricing_key(None))
        self.assertIsNone(ue._resolve_pricing_key(""))

    def test_longest_prefix_wins(self):
        ue.PRICING["claude-opus-4"] = {"input": 1, "output": 1, "cache_create": 1, "cache_read": 1}
        try:
            self.assertEqual(ue._resolve_pricing_key("claude-opus-4-8"), "claude-opus-4-8")
        finally:
            del ue.PRICING["claude-opus-4"]


class TestEstimateCost(unittest.TestCase):
    USAGE = {
        "input_tokens": 1_000_000,
        "output_tokens": 2_000_000,
        "cache_create_tokens": 0,
        "cache_read_tokens": 0,
    }

    def test_known_model_math(self):
        cost, unknown = ue.estimate_cost("claude-haiku-4-5", self.USAGE)
        self.assertFalse(unknown)
        self.assertAlmostEqual(cost, 1.0 * 1 + 5.0 * 2)

    def test_unknown_model_priced_at_fallback(self):
        cost, unknown = ue.estimate_cost("mystery-model", self.USAGE)
        self.assertTrue(unknown)
        fb = ue.PRICING[ue.FALLBACK_PRICING_KEY]
        self.assertAlmostEqual(cost, fb["input"] * 1 + fb["output"] * 2)

    def test_cache_tokens_priced(self):
        usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_create_tokens": 1_000_000,
            "cache_read_tokens": 1_000_000,
        }
        cost, _ = ue.estimate_cost("claude-sonnet-4-6", usage)
        self.assertAlmostEqual(cost, 3.75 + 0.30)


class TestProjectLabel(unittest.TestCase):
    def test_home_prefix_stripped_keeps_hyphenated_name(self):
        orig = ue._HOME_PREFIX
        ue._HOME_PREFIX = "-Users-alice"
        try:
            self.assertEqual(ue.project_label(Path("-Users-alice-my-repo")), "my-repo")
            self.assertEqual(ue.project_label(Path("-Users-alice")), "home")
            self.assertEqual(ue.project_label(Path("-srv-ci-tool")), "srv-ci-tool")
        finally:
            ue._HOME_PREFIX = orig


class TestExtractUsage(unittest.TestCase):
    def test_extracts_model_and_tokens(self):
        model, usage = ue._extract_usage(
            {
                "model": "claude-opus-4-8",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 20,
                    "cache_creation_input_tokens": 30,
                    "cache_read_input_tokens": 40,
                },
            }
        )
        self.assertEqual(model, "claude-opus-4-8")
        self.assertEqual(usage["input_tokens"], 10)
        self.assertEqual(usage["cache_create_tokens"], 30)
        self.assertEqual(usage["cache_read_tokens"], 40)

    def test_missing_usage_returns_empty(self):
        model, usage = ue._extract_usage({"model": "x"})
        self.assertEqual(usage, {})


if __name__ == "__main__":
    unittest.main()

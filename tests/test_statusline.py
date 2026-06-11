import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import statusline as sl


class TestDeepMerge(unittest.TestCase):
    def test_nested_override_preserves_siblings(self):
        merged = sl._deep_merge(
            {"icons": {"prefix": "A", "dir": "B"}, "separator": "|"},
            {"icons": {"prefix": "X"}},
        )
        self.assertEqual(merged["icons"]["prefix"], "X")
        self.assertEqual(merged["icons"]["dir"], "B")
        self.assertEqual(merged["separator"], "|")


class TestSanitize(unittest.TestCase):
    def test_masks_configured_patterns_case_insensitive(self):
        orig = sl.CFG["mask_patterns"]
        sl.CFG["mask_patterns"] = ["Secret-Name"]
        try:
            out = sl.sanitize("hello secret-name and SECRET-NAME")
            self.assertNotIn("secret-name", out.lower())
        finally:
            sl.CFG["mask_patterns"] = orig

    def test_patterns_are_literal_not_regex(self):
        orig = sl.CFG["mask_patterns"]
        sl.CFG["mask_patterns"] = ["a.b"]
        try:
            self.assertIn("axb", sl.sanitize("axb"))  # '.' must not act as a wildcard
            self.assertNotIn("a.b", sl.sanitize("a.b"))
        finally:
            sl.CFG["mask_patterns"] = orig


class TestFormatters(unittest.TestCase):
    def test_fmt_tok_scales(self):
        self.assertEqual(sl.fmt_tok(None), "—tok")
        self.assertEqual(sl.fmt_tok(950), "950tok")
        self.assertEqual(sl.fmt_tok(12_000), "12ktok")
        self.assertEqual(sl.fmt_tok(3_400_000), "3.4Mtok")

    def test_fmt_limit_requires_percentage(self):
        self.assertIsNone(sl.fmt_limit("I", "5h", None))
        self.assertIsNone(sl.fmt_limit("I", "5h", {}))
        self.assertIn("42%", sl.fmt_limit("I", "5h", {"used_percentage": 42}))


class TestCodexParsers(unittest.TestCase):
    def test_snapshot_skips_null_primary_rows(self):
        lines = [
            json.dumps({"payload": {"rate_limits": {"primary": {"used_percent": 10}}}}),
            json.dumps({"payload": {"rate_limits": {"primary": None}}}),
        ]
        snap = sl.parse_codex_snapshot(lines)
        self.assertEqual(snap["primary"]["used_percent"], 10)

    def test_snapshot_none_when_absent(self):
        self.assertIsNone(sl.parse_codex_snapshot(['{"payload": {}}', "not json"]))

    def test_usage_takes_last_total(self):
        lines = [
            json.dumps({"payload": {"info": {"total_token_usage": {"input_tokens": 1}}}}),
            json.dumps({"payload": {"info": {"total_token_usage": {"input_tokens": 2}}}}),
        ]
        self.assertEqual(sl.parse_codex_usage(lines)["input_tokens"], 2)

    def test_cost_caps_cached_at_input(self):
        usage = {"input_tokens": 100, "cached_input_tokens": 500, "output_tokens": 0}
        p = sl.CFG["codex_pricing"]
        expected = (100 * p["cached_input"]) / 1e6  # all input treated as cached
        self.assertAlmostEqual(sl.codex_cost_usd(usage), expected)


if __name__ == "__main__":
    unittest.main()

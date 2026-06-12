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


class TestSessionCostAfterClear(unittest.TestCase):
    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._orig_cache = sl.CACHE_DIR
        sl.CACHE_DIR = self.tmp

    def tearDown(self):
        sl.CACHE_DIR = self._orig_cache
        self._tmp.cleanup()

    @staticmethod
    def _clear_line(uuid: str) -> str:
        return json.dumps({
            "type": "user", "uuid": uuid,
            "message": {"role": "user", "content": "<command-name>/clear</command-name>"},
        })

    def test_resets_after_clear_marker(self):
        transcript = self.tmp / "t1.jsonl"
        transcript.write_text(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
        data = {"session_id": "sid-1", "transcript_path": str(transcript)}

        self.assertEqual(sl.session_cost_after_clear(data, 1.0), 1.0)
        self.assertEqual(sl.session_cost_after_clear(data, 1.5), 1.5)

        with transcript.open("a") as fh:
            fh.write(self._clear_line("u-clear-1") + "\n")
        self.assertEqual(sl.session_cost_after_clear(data, 1.5), 0.0)
        self.assertAlmostEqual(sl.session_cost_after_clear(data, 1.8), 0.3)
        # same marker on later renders: no re-reset
        self.assertAlmostEqual(sl.session_cost_after_clear(data, 2.5), 1.0)

        with transcript.open("a") as fh:
            fh.write(self._clear_line("u-clear-2") + "\n")
        self.assertEqual(sl.session_cost_after_clear(data, 2.5), 0.0)

    def test_resets_on_transcript_path_change(self):
        t1 = self.tmp / "t1.jsonl"
        t2 = self.tmp / "t2.jsonl"
        for t in (t1, t2):
            t.write_text(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
        self.assertEqual(sl.session_cost_after_clear(
            {"session_id": "sid-2", "transcript_path": str(t1)}, 3.0), 3.0)
        self.assertAlmostEqual(sl.session_cost_after_clear(
            {"session_id": "sid-2", "transcript_path": str(t2)}, 3.2), 0.2)

    def test_passthrough_without_sid_or_cost(self):
        self.assertEqual(sl.session_cost_after_clear({}, 1.0), 1.0)
        self.assertIsNone(sl.session_cost_after_clear({"session_id": "x"}, None))


class TestQuotaForecast(unittest.TestCase):
    """Learned burn profile + depletion projection + quiet/always modes."""

    def setUp(self):
        import tempfile
        from datetime import datetime
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self._orig = (sl.CACHE_DIR, sl.QUOTA_SNAP_LOG, sl.CFG["forecast"])
        sl.CACHE_DIR = tmp / "cache"
        sl.QUOTA_SNAP_LOG = tmp / "snaps.jsonl"
        sl.CFG["forecast"] = dict(sl.DEFAULTS["forecast"])
        self.base = datetime(2026, 6, 12, 9, 0).timestamp()

    def tearDown(self):
        sl.CACHE_DIR, sl.QUOTA_SNAP_LOG, sl.CFG["forecast"] = self._orig
        self._tmp.cleanup()

    def _write_snaps(self, hours, rate_per_h, resets):
        lines, pct, ts = [], 10.0, self.base
        for _ in range(int(hours * 12)):
            lines.append(json.dumps({"ts": ts, "five_hour": {"pct": pct, "resets_at": resets}}))
            ts += 300
            pct = min(99.0, pct + rate_per_h / 12)
        sl.QUOTA_SNAP_LOG.parent.mkdir(parents=True, exist_ok=True)
        sl.QUOTA_SNAP_LOG.write_text("\n".join(lines) + "\n")

    def test_profile_learned_from_snapshots(self):
        self._write_snaps(3, 12.0, self.base + 5 * 3600)
        prof = sl.burn_profile("five_hour")
        self.assertIsNotNone(prof)
        self.assertGreaterEqual(prof["samples"], sl.MIN_PROFILE_SAMPLES)
        self.assertAlmostEqual(prof["overall"], 12.0, delta=0.7)

    def test_insufficient_data_returns_none(self):
        self._write_snaps(0.5, 12.0, self.base + 5 * 3600)
        self.assertIsNone(sl.burn_profile("five_hour"))

    def test_project_depletion_hourly_walk(self):
        from datetime import datetime
        profile = {"hour_rates": {"9": 30.0, "10": 6.0}, "overall": 18.0, "samples": 48}
        now = self.base  # 09:00
        dep = sl.project_depletion(80.0, now, now + 5 * 3600, profile)
        self.assertIsNotNone(dep)
        self.assertLess(abs(dep - (now + 40 * 60)), 60)  # 20% / 30%/h = 40min
        slow = {"hour_rates": {}, "overall": 2.0, "samples": 48}
        self.assertIsNone(sl.project_depletion(80.0, now, now + 3600, slow))

    def test_quiet_mode_thresholds(self):
        now = self.base + 3600
        # normal usage, no profile, linear projection stays inside? 31% @1h of 5h:
        # projects depletion but floor (50%) keeps it quiet
        block = {"used_percentage": 31, "resets_at": now + 4 * 3600}
        self.assertEqual(sl.quota_suffix(block, 5 * 3600, now=now), "")
        # >= 80% always warns
        block = {"used_percentage": 85, "resets_at": now + 4 * 3600}
        self.assertTrue(sl.quota_suffix(block, 5 * 3600, now=now).startswith(sl.ICON["warn"]))

    def test_always_mode_shows_eta_without_warn(self):
        sl.CFG["forecast"]["mode"] = "always"
        now = self.base + 3600
        block = {"used_percentage": 31, "resets_at": now + 4 * 3600}
        out = sl.quota_suffix(block, 5 * 3600, now=now)
        self.assertTrue(out.startswith(sl.ICON["forecast"]))
        self.assertIn("~", out)

    def test_off_mode_disables(self):
        sl.CFG["forecast"]["mode"] = "off"
        block = {"used_percentage": 95, "resets_at": self.base + 3600}
        self.assertEqual(sl.quota_suffix(block, 5 * 3600, now=self.base), "")


if __name__ == "__main__":
    unittest.main()

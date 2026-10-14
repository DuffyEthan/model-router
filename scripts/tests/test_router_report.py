"""Tests for scripts/router_report.py against small synthetic transcripts.

Fixtures (fixtures/projects, fixtures/routing-log.jsonl) follow Claude Code's real
transcript schema with fake content. Expected costs are worked by hand below, in
millionths of a dollar (token count x $/MTok), using plugin/hooks/prices.json:

            input  5m write  1h write  cache read  output
  Opus 5.5    4      5         8         0.20        20
  Sonnet 5.5  2      2.50      4         0.10        10
  Haiku 5.5   0.10   0.125     0.20      0.01        0.50   (prompt <= 100K tokens)
  Haiku 5.5   0.50   0.625     1         0.05        2.50   (prompt  > 100K tokens)
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from collections import defaultdict
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir))

import router_report as rr  # noqa: E402

FIX = os.path.join(HERE, "fixtures")
PROJECTS = os.path.join(FIX, "projects")
LOG = os.path.join(FIX, "routing-log.jsonl")
STORE = os.path.join(FIX, "store.json")
PRICES = rr.load_prices()
U = 1e-6  # one millionth of a dollar

# --- Session A (main thread, model switches) --------------------------------------
# r1 Opus   in 100, 5m write 2000, out 50 (two lines, one message id: counted once)
#   actual  100*4 + 2000*5 + 50*20                       = 400 + 10000 + 1000 = 11400
#   cf      same (already Opus)                                               = 11400
# r2 Haiku  in 10, 5m write 2150, out 20 -- first request after Opus -> Haiku.
#   The previous request held 100 + 0 + 2000 + 50 = 2150 tokens, so all 2150
#   written tokens are switch writes (cache reads under Opus-only).
#   actual  10*0.10 + 2150*0.125 + 20*0.5                = 1 + 268.75 + 10    = 279.75
#   penalty 2150*(0.125 - 0.01)                                               = 247.25
#   cf      10*4 + 2150*0.20 + 20*20                     = 40 + 430 + 400     = 870
# r3 Haiku  in 5, read 2150, 5m write 30, out 10 (no switch)
#   actual  5*0.10 + 30*0.125 + 2150*0.01 + 10*0.5       = 0.5+3.75+21.5+5    = 30.75
#   cf      5*4 + 30*5 + 2150*0.20 + 10*20               = 20+150+430+200     = 800
# r4 Opus   in 5, 5m write 2215, out 40 -- Haiku -> Opus. Previous held
#   5 + 2150 + 30 + 10 = 2195, so 2195 of the 2215 written are switch writes,
#   the other 20 are new content (a write either way).
#   actual  5*4 + 2215*5 + 40*20                         = 20 + 11075 + 800   = 11895
#   penalty 2195*(5 - 0.20)                                                   = 10536
#   cf      5*4 + 20*5 + 2195*0.20 + 40*20               = 20+100+439+800     = 1359
A_ACTUAL = 11400 + 279.75 + 30.75 + 11895  # 23605.5
A_PENALTY = 247.25 + 10536  # 10783.25
A_CF = 11400 + 870 + 800 + 1359  # 14429

# --- Session B (Haiku over 100K, an unknown model, malformed lines) ----------------
# b1 Haiku  in 1000, read 99000, 1h write 1000, out 200.
#   Prompt = 1000 + 99000 + 1000 = 101000 > 100000 -> long-context Haiku rates.
#   actual  1000*0.50 + 1000*1 + 99000*0.05 + 200*2.5    = 500+1000+4950+500  = 6950
#   cf      1000*4 + 1000*8 + 99000*0.20 + 200*20        = 4000+8000+19800+4000 = 35800
# b2 Haiku  in 100, read 50000, out 100. Prompt 50100 <= 100K -> standard rates.
#   actual  100*0.10 + 50000*0.01 + 100*0.5              = 10 + 500 + 50      = 560
#   cf      100*4 + 50000*0.20 + 100*20                  = 400 + 10000 + 2000 = 12400
# b3 claude-mystery-9: unpriced, left out of every cost line.
# b4 <synthetic>: not an API request, skipped.
# Subagent x1 (own chain, so no switch against the main thread): Sonnet 5.5
#   in 300, 5m write 1000, out 100
#   actual  300*2 + 1000*2.5 + 100*10                    = 600 + 2500 + 1000  = 4100
#   cf      300*4 + 1000*5 + 100*20                      = 1200 + 5000 + 2000 = 8200
B_ACTUAL = 6950 + 560 + 4100  # 11610
B_CF = 35800 + 12400 + 8200  # 56400

# --- Routing log (2026-10-01) -------------------------------------------------------
# Classifier calls: 2 ok with usage + 1 timeout without usage = 3 calls.
#   (1000*0.10 + 30*0.5) + (900*0.10 + 20*0.5) = 115 + 100 = 215 for 2 calls;
#   the third is assumed at the mean: 215 * 3/2 = 322.5
CLASSIFIER = 322.5
# User prompts 3 (two in A, one in B); overrides: /model 1, /router off 1 -> 2/3.


def run(**kw):
    stats = defaultdict(int)
    reqs = rr.read_requests(PROJECTS, stats)
    log = rr.read_log(kw.pop("logs", [LOG]), stats)
    lo, hi = rr.day_bounds(kw.pop("since", None), kw.pop("until", None))
    return rr.build_report(reqs, log, PRICES, lo, hi, **kw), stats


class FixtureReport(unittest.TestCase):
    def test_costs_counterfactual_and_savings(self):
        rep, _ = run(since="2026-10-01", until="2026-10-01")
        self.assertEqual(rep["scope"], "sessions in the routing log")
        self.assertAlmostEqual(rep["actual_usd"], (A_ACTUAL + B_ACTUAL) * U, places=12)
        self.assertAlmostEqual(rep["switch_penalty_usd"], A_PENALTY * U, places=12)
        self.assertAlmostEqual(rep["counterfactual_usd"], (A_CF + B_CF) * U, places=12)
        self.assertAlmostEqual(rep["classifier_usd"], CLASSIFIER * U, places=12)
        self.assertTrue(rep["classifier_estimated"])
        # 70829 - 35215.5 - 322.5 = 35291
        self.assertAlmostEqual(rep["net_savings_usd"], (A_CF + B_CF - A_ACTUAL - B_ACTUAL - CLASSIFIER) * U, places=12)
        self.assertAlmostEqual(rep["net_savings_usd"], 35291 * U, places=12)

    def test_switches_alone_can_cost_more_than_opus(self):
        # Session A alone: Opus -> Haiku -> Opus re-wrote the cache twice. Actual
        # 23605.5 exceeds the Opus-only 14429: routing lost money here.
        stats = defaultdict(int)
        reqs = [r for r in rr.read_requests(PROJECTS, stats) if r["session"] == "sess-a"]
        rep = rr.build_report(reqs, [], PRICES)
        self.assertAlmostEqual(rep["actual_usd"], A_ACTUAL * U, places=12)
        self.assertAlmostEqual(rep["counterfactual_usd"], A_CF * U, places=12)
        self.assertLess(rep["net_savings_usd"], 0)
        self.assertEqual(rep["by_model"]["claude-haiku-5-5"]["switch_requests"], 1)
        self.assertEqual(rep["by_model"]["claude-opus-5-5"]["switch_requests"], 1)

    def test_long_context_haiku_is_priced_per_request(self):
        r = {"input": 1000, "cw5m": 0, "cw1h": 1000, "cache_read": 99000, "output": 200}
        rates = rr.rate_card(PRICES, "claude-haiku-5-5", 101000)
        self.assertAlmostEqual(rr.price_tokens(rates, r), 6950 * U, places=12)
        # Exactly 100,000 is "up to 100K": standard rates.
        self.assertEqual(rr.rate_card(PRICES, "claude-haiku-5-5", 100000)["input"], 0.10)
        self.assertEqual(rr.rate_card(PRICES, "claude-haiku-5-5", 100001)["input"], 0.50)

    def test_shares_by_model(self):
        rep, _ = run(since="2026-10-01", until="2026-10-01")
        bm = rep["by_model"]
        self.assertEqual({k: v["requests"] for k, v in bm.items()},
                         {"claude-opus-5-5": 2, "claude-haiku-5-5": 4, "claude-sonnet-5-5": 1})
        self.assertEqual(rep["priced_requests"], 7)
        # opus 2150+2260 ; haiku 2180+2195+101200+50200 ; sonnet 1400
        self.assertEqual(rr.total(bm["claude-opus-5-5"]["tokens"]), 4410)
        self.assertEqual(rr.total(bm["claude-haiku-5-5"]["tokens"]), 155775)
        self.assertEqual(rep["priced_tokens"], 4410 + 155775 + 1400)
        self.assertEqual(rep["prompt_tiers"], {"opus": 1, "haiku": 1, "session": 3})

    def test_unknown_models_are_unpriced_not_zero(self):
        rep, _ = run()
        self.assertEqual(rep["unpriced"], {"claude-mystery-9": {"requests": 1, "tokens": 1000}})
        self.assertNotIn("claude-mystery-9", rep["by_model"])
        self.assertIsNone(rr.rate_card(PRICES, "claude-mystery-9", 10))
        text = rr.format_report(rep, defaultdict(int), PRICES, "x")
        self.assertIn("unpriced", text)

    def test_malformed_lines_are_skipped_and_counted(self):
        _, stats = run()
        self.assertEqual(stats["malformed_lines"], 3)
        self.assertEqual(stats["duplicate_lines"], 1)
        self.assertEqual(stats["synthetic"], 1)
        self.assertEqual(stats["log_malformed"], 1)
        self.assertEqual(stats["files"], 3)

    def test_overrides_and_origins(self):
        rep, _ = run(since="2026-10-01", until="2026-10-01")
        self.assertEqual(rep["user_prompts"], 3)
        self.assertEqual(rep["overrides_model"], 1)
        self.assertEqual(rep["overrides_router_off"], 1)
        self.assertAlmostEqual(rep["override_rate"], 2 / 3)
        self.assertEqual(rep["other_origin_kinds"], {"scheduled-trigger": 1})
        self.assertEqual(rep["mid_turn"], 1)
        self.assertEqual(rep["outcomes"], {"ok": 2, "skipped": 2, "timeout": 1})

    def test_date_range_filters_the_log(self):
        rep, _ = run()  # no range: includes the September record from sess-old
        self.assertEqual(rep["prompts"], 6)
        rep, _ = run(since="2026-10-01")
        self.assertEqual(rep["prompts"], 5)

    def test_empty_range(self):
        rep, _ = run(since="2027-01-01", until="2027-01-31")
        self.assertEqual(rep["requests"], 0)
        self.assertEqual(rep["actual_usd"], 0)
        self.assertEqual(rep["counterfactual_usd"], 0)
        self.assertIsNone(rep["override_rate"])
        text = rr.format_report(rep, defaultdict(int), PRICES, "empty")
        self.assertIn("no records in range", text)

    def test_without_a_log_all_sessions_are_counted(self):
        rep, _ = run(logs=[])
        self.assertEqual(rep["scope"], "all sessions")
        self.assertEqual(rep["prompts"], 0)
        self.assertIsNone(rep["classifier_usd"])
        self.assertAlmostEqual(rep["actual_usd"], (A_ACTUAL + B_ACTUAL) * U, places=12)

    def test_log_scope_drops_sessions_not_in_the_log(self):
        rep, _ = run(logs=[STORE])  # store.json holds only sess-a's two records
        self.assertEqual(rep["scope"], "sessions in the routing log")
        self.assertAlmostEqual(rep["actual_usd"], A_ACTUAL * U, places=12)
        rep, _ = run(logs=[STORE], all_sessions=True)
        self.assertAlmostEqual(rep["actual_usd"], (A_ACTUAL + B_ACTUAL) * U, places=12)

    def test_store_and_export_records_are_not_double_counted(self):
        rep, _ = run(logs=[STORE, LOG])
        self.assertEqual(rep["prompts"], 6)


class Units(unittest.TestCase):
    def test_canonical_model(self):
        for raw, want in [
            ("claude-opus-5-5", "claude-opus-5-5"),
            ("us.anthropic.claude-opus-5-5", "claude-opus-5-5"),
            ("claude-haiku-4-5-20251001", "claude-haiku-4-5"),
            ("claude-haiku-4-5@20251001", "claude-haiku-4-5"),
            ("claude-opus-4-6[1m]", "claude-opus-4-6"),
            ("anthropic.claude-sonnet-5-5-v1:0", "claude-sonnet-5-5"),
        ]:
            self.assertEqual(rr.canonical_model(raw), want, raw)

    def test_us_inference_is_1_1x(self):
        req = {"id": "x", "file": "f", "session": "s", "chain": "", "model": "claude-opus-5-5",
               "ts": rr.parse_ts("2026-10-01T00:00:00Z"), "geo": "us", "speed": "standard",
               "tokens": {"input": 1000, "cw5m": 0, "cw1h": 0, "cache_read": 0, "output": 0}}
        rep = rr.build_report([req], [], PRICES)
        self.assertAlmostEqual(rep["actual_usd"], 1000 * 4 * 1.1 * U, places=12)  # 4400

    def test_fast_mode_is_unpriced(self):
        req = {"id": "x", "file": "f", "session": "s", "chain": "", "model": "claude-opus-5-5",
               "ts": rr.parse_ts("2026-10-01T00:00:00Z"), "geo": None, "speed": "fast",
               "tokens": {"input": 10, "cw5m": 0, "cw1h": 0, "cache_read": 0, "output": 0}}
        rep = rr.build_report([req], [], PRICES)
        self.assertEqual(rep["actual_usd"], 0)
        self.assertIn("claude-opus-5-5", rep["unpriced"])

    def test_switch_after_cache_expiry_is_not_reclassified(self):
        def req(i, model, ts, cw):
            return {"id": i, "file": "f", "session": "s", "chain": "", "model": model,
                    "ts": rr.parse_ts(ts), "geo": None, "speed": "standard",
                    "tokens": {"input": 0, "cw5m": cw, "cw1h": 0, "cache_read": 0, "output": 0}}
        reqs = [req("1", "claude-opus-5-5", "2026-10-01T10:00:00Z", 1000),
                req("2", "claude-haiku-5-5", "2026-10-01T10:06:00Z", 1000)]  # 6 min > 5m TTL
        rr.mark_switches(reqs)
        self.assertEqual(reqs[1]["switch_reads"], 0)

    def test_no_prompt_text_reaches_the_report(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rr.main(["--projects-dir", PROJECTS, "--log", LOG, "--json"])
        self.assertNotIn("Fake prompt", out.getvalue())
        self.assertNotIn("fake reply", out.getvalue())
        data = json.loads(out.getvalue())
        self.assertIn("actual_usd", data)

    def test_cli_text_and_missing_projects_dir(self):
        tmp = tempfile.mkdtemp()
        try:
            out = io.StringIO()
            with redirect_stdout(out):
                code = rr.main(["--projects-dir", os.path.join(tmp, "nope"), "--log", os.path.join(tmp, "none.jsonl")])
            self.assertEqual(code, 0)
            self.assertIn("API-equivalent", out.getvalue())
        finally:
            shutil.rmtree(tmp)

    def test_default_log_paths(self):
        tmp = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(tmp, "plugins", "store"))
            os.makedirs(os.path.join(tmp, "model-router"))
            a = os.path.join(tmp, "plugins", "store", "model-router_model-router-abc123.json")
            b = os.path.join(tmp, "model-router", "routing-log.jsonl")
            for p in (a, b):
                open(p, "w").close()
            open(os.path.join(tmp, "plugins", "store", "other-plugin_x.json"), "w").close()
            self.assertEqual(rr.default_log_paths(tmp), [a, b])
        finally:
            shutil.rmtree(tmp)

    def test_price_table_has_source_and_date(self):
        self.assertTrue(PRICES["source"].startswith("https://platform.claude.com/"))
        self.assertEqual(PRICES["verified"], "2026-10-14")
        opus = PRICES["models"]["claude-opus-5-5"]
        self.assertEqual([opus[k] for k in ("input", "cache_write_5m", "cache_write_1h", "cache_read", "output")],
                         [4, 5, 8, 0.20, 20])


if __name__ == "__main__":
    unittest.main()

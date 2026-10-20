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

    def test_spend_by_source_and_route(self):
        # sess-a: a1 under the 10:00 opus prompt; a2, a3 under the 10:01 haiku prompt;
        # a4 under the 10:03 slash command. sess-b: b1, b2 under the 11:00 prompt whose
        # classifier timed out; subagent x1 starts at 11:00:20, under the same prompt.
        rep, _ = run(since="2026-10-01", until="2026-10-01")
        cost = {src: {b: round(m["cost"] / U, 6) for b, m in rows.items()} for src, rows in rep["by_source"].items()}
        self.assertEqual(cost, {
            "main": {"opus": 11400, "haiku": 279.75 + 30.75, "unrouted: slash": 11895,
                     "unrouted: classifier timeout": 6950 + 560},
            "subagent": {"unrouted: classifier timeout": 4100},
        })
        self.assertAlmostEqual(sum(m["cost"] for rows in rep["by_source"].values() for m in rows.values()),
                               rep["actual_usd"], places=12)
        self.assertEqual(rep["before_log"], {"requests": 0, "cost": 0.0})
        text = rr.format_report(rep, defaultdict(int), PRICES, "x")
        self.assertIn("Spend by source and route", text)
        self.assertIn("unrouted: classifier timeout", text)

    def test_simulation_calibrates_on_the_fixtures(self):
        rep, _ = run(since="2026-10-01", until="2026-10-01", simulate_policies=list(rr.POLICIES))
        sims = rep["simulations"]
        self.assertEqual(set(sims), set(rr.POLICIES))
        self.assertEqual(sims["observed"]["requests"], rep["priced_requests"])
        # Observed models: a3 and a4 read what a2 and a3 held, including their input and
        # output, which the transcripts show as writes (a3: 30, a4: 20 of 2215), so the
        # simulation is slightly cheaper than actual.
        #   a3  5*0.10 + 2180*0.01 + 10*0.5            = 0.5 + 21.8 + 5   = 27.3   (30.75)
        #   a4  5*4 + 2215*0.20 + 40*20 -> but Opus last ran at a1 (2150 held, 3 min ago):
        #       reads 2150, writes 65: 20 + 430 + 325 + 800              = 1575   (11895)
        # a2 switched to Haiku, which this chain had not run: its 2150 written tokens are
        # written again (the chain's first request read nothing, so no shared prefix).
        self.assertAlmostEqual(sims["observed"]["usd"], (A_ACTUAL - 30.75 + 27.3 - 11895 + 1575 + B_ACTUAL) * U, places=12)
        self.assertEqual(sims["observed"]["switches"], 2)
        self.assertEqual(sims["opus-only"]["switches"], 0)
        self.assertLess(sims["sonnet-only"]["usd"], sims["opus-only"]["usd"])


class Attribution(unittest.TestCase):
    T0 = "2026-10-01T10:00:00Z"

    @staticmethod
    def req(i, ts, model="claude-opus-5-5", chain="", session="s", file="f", tokens=None):
        return {"id": i, "file": file, "session": session, "chain": chain, "model": model,
                "ts": rr.parse_ts(ts), "geo": None, "speed": "standard",
                "tokens": tokens or {"input": 1000, "cw5m": 0, "cw1h": 0, "cache_read": 0, "output": 0}}

    @staticmethod
    def prompt(ts, tier="session", session="s", **kw):
        return {"kind": "prompt", "ts": ts, "session": session, "tier": tier, **kw}

    def test_requests_belong_to_the_latest_prompt_at_or_before_them(self):
        log = [self.prompt("2026-10-01T10:00:00Z", "haiku", origin="user"),
               self.prompt("2026-10-01T10:05:00Z", "opus", origin="user")]
        reqs = [self.req("m1", "2026-10-01T10:00:00Z"),  # same instant: belongs to it
                self.req("m2", "2026-10-01T10:04:59Z"),
                self.req("m3", "2026-10-01T10:05:01Z"),
                # A subagent started under the haiku prompt stays with it after 10:05.
                self.req("s1", "2026-10-01T10:04:00Z", chain="a1", file="g"),
                self.req("s2", "2026-10-01T10:06:00Z", chain="a1", file="g")]
        rr.attribute(reqs, log)
        self.assertEqual([r["prompt"]["tier"] for r in reqs], ["haiku", "haiku", "opus", "haiku", "haiku"])
        self.assertFalse(any(r["before_log"] for r in reqs))

    def test_route_buckets(self):
        log = [{"kind": "override", "ts": "2026-10-01T09:59:00Z", "session": "s", "action": "router-off"},
               self.prompt("2026-10-01T10:01:00Z", "sonnet", origin="user"),
               self.prompt("2026-10-01T10:02:00Z", "sonnet", origin="other", backgroundTier="sonnet"),
               self.prompt("2026-10-01T10:03:00Z", skip="non-user", origin="other", originKind="peer"),
               self.prompt("2026-10-01T10:04:00Z", skip="off", origin="user"),
               self.prompt("2026-10-01T10:05:00Z", outcome="failed", origin="user")]
        reqs = [self.req(str(i), f"2026-10-01T10:0{i}:30Z") for i in range(6)]
        reqs.append(self.req("x", "2026-10-01T10:00:00Z", session="other", file="h"))
        rr.attribute(reqs, log)
        self.assertEqual([rr.route_bucket(r) for r in reqs], [
            rr.BEFORE_FIRST_PROMPT, "sonnet", "background → sonnet", "unrouted: peer",
            "unrouted: off", "unrouted: classifier failed", rr.NOT_LOGGED])

    def test_requests_before_the_routing_log_are_counted_apart(self):
        log = [self.prompt("2026-10-01T10:00:00Z", "haiku", origin="user")]
        reqs = [self.req("early", "2026-10-01T09:00:00Z"), self.req("late", "2026-10-01T10:00:10Z")]
        rep = rr.build_report(reqs, log, PRICES)
        self.assertEqual(rep["requests"], 1)
        self.assertEqual(rep["before_log"]["requests"], 1)
        self.assertAlmostEqual(rep["before_log"]["cost"], 4000 * U, places=12)
        self.assertAlmostEqual(rep["actual_usd"], 4000 * U, places=12)
        self.assertAlmostEqual(rep["counterfactual_usd"], 4000 * U, places=12)
        text = rr.format_report(rep, defaultdict(int), PRICES, "x")
        self.assertIn("1 before routing log ($0.0040)", text)
        # A log record before `lo` still marks the session's start and finds the prompt.
        rep = rr.build_report(reqs, log, PRICES, *rr.day_bounds("2026-10-02", None))
        self.assertEqual(rep["requests"], 0)

    def test_policy_picks(self):
        user_haiku = {"prompt": self.prompt(self.T0, "haiku", origin="user"), "chain": ""}
        user_opus = {"prompt": self.prompt(self.T0, "opus", origin="user"), "chain": ""}
        unrouted = {"prompt": self.prompt(self.T0, skip="off", origin="user"), "chain": ""}
        peer = {"prompt": self.prompt(self.T0, skip="non-user", origin="other", originKind="peer"), "chain": ""}
        sub = {"prompt": self.prompt(self.T0, "haiku", origin="user"), "chain": "a1"}
        none = {"prompt": None, "chain": ""}
        haiku, sonnet, opus = (rr.TIER_MODELS[t] for t in ("haiku", "sonnet", "opus"))
        want = {
            "observed": [None, None, None, None, None, None],
            "opus-only": [opus] * 6,
            "sonnet-only": [sonnet] * 6,
            "sonnet-first": [haiku, opus, sonnet, sonnet, sonnet, sonnet],
            "sonnet-first-floor": [sonnet, opus, sonnet, sonnet, sonnet, sonnet],
            "background-sonnet": [None, None, None, sonnet, sonnet, None],
        }
        for name, models in want.items():
            got = [rr.POLICIES[name](r) for r in (user_haiku, user_opus, unrouted, peer, sub, none)]
            self.assertEqual(got, models, name)


class Simulation(unittest.TestCase):
    """One main-thread chain, worked by hand (5-minute cache):

      q1  t=0     Opus  in 100, read 500,  5m write 1000, out 100  -> holds 1700
      q2  t=60s   Opus  in 10,  read 1700, 5m write 50,   out 10   -> holds 1770
      q3  t=120s  Opus  in 5,   read 1770, 5m write 30,   out 5    -> holds 1810
      q4  t=520s  Opus  in 5,   read 0,    5m write 1830, out 5    (cache expired)

    q1 read 500 from outside the chain: the shared prefix, warm on any model.
    """

    def chain(self):
        def req(i, s, cr, cw, inp, out):
            return {"id": i, "file": "f", "session": "s", "chain": "", "model": "claude-opus-5-5",
                    "ts": rr.parse_ts("2026-10-01T10:00:00Z") + rr.dt.timedelta(seconds=s), "geo": None,
                    "speed": "standard", "tokens": {"input": inp, "cw5m": cw, "cw1h": 0, "cache_read": cr, "output": out}}
        return [req("q1", 0, 500, 1000, 100, 100), req("q2", 60, 1700, 50, 10, 10),
                req("q3", 120, 1770, 30, 5, 5), req("q4", 520, 0, 1830, 5, 5)]

    def test_observed_models(self):
        # q1-q3 keep their split; q4's Opus cache went cold, but the shared 500 is read:
        #   q1 100*4 + 500*0.2 + 1000*5 + 100*20 = 400 + 100 + 5000 + 2000  = 7500
        #   q2 10*4 + 1700*0.2 + 50*5 + 10*20    = 40 + 340 + 250 + 200     = 830
        #   q3 5*4 + 1770*0.2 + 30*5 + 5*20      = 20 + 354 + 150 + 100     = 624
        #   q4 5*4 + 500*0.2 + 1330*5 + 5*20     = 20 + 100 + 6650 + 100    = 6870
        s = rr.simulate(self.chain(), PRICES, rr.POLICIES["observed"], lambda r: True)
        self.assertAlmostEqual(s["usd"], (7500 + 830 + 624 + 6870) * U, places=12)
        self.assertEqual((s["requests"], s["switches"]), (4, 0))

    def test_a_switch_writes_the_cache_and_switching_back_reads_it(self):
        # q2 on Sonnet: cold, reads only the shared 500 and writes 1250.
        #   q2 10*2 + 500*0.1 + 1250*2.5 + 10*10 = 20 + 50 + 3125 + 100     = 3295
        # q3 back on Opus within 5 minutes of q1: reads q1's 1700, writes 100.
        #   q3 5*4 + 1700*0.2 + 100*5 + 5*20     = 20 + 340 + 500 + 100     = 960
        # q4 cold as before: 6870.
        pick = lambda r: rr.TIER_MODELS["sonnet"] if r["id"] == "q2" else None  # noqa: E731
        s = rr.simulate(self.chain(), PRICES, pick, lambda r: True)
        self.assertAlmostEqual(s["usd"], (7500 + 3295 + 960 + 6870) * U, places=12)
        self.assertEqual(s["switches"], 2)
        # Only counted requests add cost and switches; the rest still move the cache.
        s = rr.simulate(self.chain(), PRICES, pick, lambda r: r["id"] == "q3")
        self.assertAlmostEqual(s["usd"], 960 * U, places=12)
        self.assertEqual((s["requests"], s["switches"]), (1, 1))

    def test_one_hour_cache_stays_warm_past_five_minutes(self):
        reqs = self.chain()
        for r in reqs:  # the same chain on 1-hour cache
            t = r["tokens"]
            t["cw1h"], t["cw5m"] = t["cw5m"], 0
        s = rr.simulate(reqs, PRICES, rr.POLICIES["observed"], lambda r: r["id"] == "q4")
        # q4 is 400s after q3, inside the hour: reads q3's 1810 and writes 20 (1-hour).
        #   5*4 + 1810*0.2 + 20*8 + 5*20 = 20 + 362 + 160 + 100 = 642
        self.assertAlmostEqual(s["usd"], 642 * U, places=12)

    def test_cli_simulate_adds_the_baselines(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rr.main(["--projects-dir", PROJECTS, "--log", LOG, "--simulate", "sonnet-first", "--json"])
        self.assertEqual(set(json.loads(out.getvalue())["simulations"]), {"observed", "opus-only", "sonnet-first"})
        out = io.StringIO()
        with redirect_stdout(out):
            rr.main(["--projects-dir", PROJECTS, "--log", LOG, "--simulate", "all"])
        text = out.getvalue()
        self.assertIn("Simulated policies", text)
        for name in rr.POLICIES:
            self.assertIn(name, text)
        self.assertIn("calibration: observed simulates to", text)


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

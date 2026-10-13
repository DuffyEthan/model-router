#!/usr/bin/env python3
"""Routing-quality eval: does the real Haiku classifier pick the right tier?

Uses the plugin's actual router prompt (read from plugin/hooks/register.ts) and
builds the classifier input the same way the plugin does, then calls Haiku at
low effort through the `claude` CLI. Costs roughly $0.0003 per classification.

    python3 evals/routing_eval.py              # 3 runs per case
    python3 evals/routing_eval.py --runs 1 --only h1,o1
"""

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASES = Path(__file__).with_name("cases.jsonl")
RESULTS = Path(__file__).with_name("results.json")
TIERS = ["haiku", "sonnet", "opus"]


def plugin_system_prompt() -> str:
    src = (ROOT / "plugin/hooks/register.ts").read_text()
    m = re.search(r"const SYSTEM = `(.*?)`\n", src, re.S)
    if not m:
        sys.exit("could not find SYSTEM in plugin/hooks/register.ts")
    return m.group(1)


def classifier_input(case: dict) -> str:
    # Mirrors classify() in register.ts.
    tail = "\n".join(f"{role}: {text[:600]}" for role, text in case.get("context", [])[-4:] if text)
    return (f"<conversation_tail>\n{tail}\n</conversation_tail>\n\n" if tail else "") + \
        f"<new_message>\n{case['prompt'][:8000]}\n</new_message>"


def classify(system: str, case: dict) -> dict:
    cmd = [
        "claude", "-p", "--model", "haiku", "--effort", "low",
        "--system-prompt", system, "--tools", "", "--setting-sources", "",
        "--strict-mcp-config", "--no-session-persistence", "--output-format", "json",
        classifier_input(case),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=90, stdin=subprocess.DEVNULL)
        body = json.loads(out.stdout)
        text = body.get("result", "")
        m = re.search(r"\{[\s\S]*\}", text)  # same parsing as the plugin
        d = json.loads(m.group(0)) if m else {}
        model = d.get("model") if d.get("model") in TIERS else None
        return {"model": model, "effort": d.get("effort"), "reason": d.get("reason", ""),
                "cost": body.get("total_cost_usd") or 0.0}
    except Exception as e:
        return {"model": None, "effort": None, "reason": f"error: {e}", "cost": 0.0}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--only", help="comma-separated case ids")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    cases = [json.loads(l) for l in CASES.read_text().splitlines() if l.strip()]
    if args.only:
        keep = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in keep]
    system = plugin_system_prompt()

    jobs = [(c, r) for c in cases for r in range(args.runs)]
    with ThreadPoolExecutor(args.workers) as pool:
        outs = list(pool.map(lambda j: classify(system, j[0]), jobs))

    by_case: dict[str, list[dict]] = {}
    for (c, _), o in zip(jobs, outs):
        by_case.setdefault(c["id"], []).append(o)

    totals = Counter()
    rows = []
    for c in cases:
        runs = by_case[c["id"]]
        expect = c["expect"]
        primary = TIERS.index(expect[0])
        for o in runs:
            if o["model"] is None:
                totals["failed"] += 1
            elif o["model"] in expect:
                totals["ok"] += 1
            elif TIERS.index(o["model"]) < primary:
                totals["under"] += 1  # too cheap: risk of a weak answer
            else:
                totals["over"] += 1   # too expensive: wasted money
        picks = Counter(f"{o['model']}/{o['effort']}" for o in runs)
        ok = sum(o["model"] in expect for o in runs)
        flag = "" if ok == len(runs) else ("  <-- MISS" if ok == 0 else "  <-- unstable")
        rows.append({"id": c["id"], "expect": expect, "picks": dict(picks),
                     "reasons": [o["reason"] for o in runs]})
        print(f"{c['id']:>4}  expect {'/'.join(expect):<13} got {', '.join(f'{k} x{v}' for k, v in picks.items())}{flag}")

    n = len(outs)
    cost = sum(o["cost"] for o in outs)
    print(f"\n{n} classifications, ${cost:.4f} total (${cost / max(n, 1):.5f} each)")
    print(f"correct tier: {totals['ok']}/{n} ({100 * totals['ok'] / n:.0f}%)")
    print(f"under-routed (too cheap): {totals['under']}   over-routed (too expensive): {totals['over']}   failed: {totals['failed']}")
    RESULTS.write_text(json.dumps({"totals": totals, "cost_usd": cost, "cases": rows}, indent=2))
    print(f"details: {RESULTS.relative_to(ROOT)}")


if __name__ == "__main__":
    main()

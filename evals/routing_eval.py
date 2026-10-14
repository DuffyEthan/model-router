#!/usr/bin/env python3
"""Routing-quality eval: does the real Haiku classifier pick the right tier?

Uses the plugin's actual router prompt (read from plugin/hooks/register.ts) and
builds the classifier input the same way the plugin does, then calls Haiku at
low effort through the `claude` CLI. Costs roughly $0.0002 per classification;
see evals/README.md for what the numbers mean and what a full run costs.

    python3 evals/routing_eval.py              # all cases, 3 runs each
    python3 evals/routing_eval.py --runs 1 --only h1,ho1
    python3 evals/routing_eval.py --set heldout

Each run writes evals/results/<YYYY-MM-DD>.json (or -2, -3, ... for later
runs on the same day).
"""

import argparse
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVALS = Path(__file__).resolve().parent
CASE_FILES = {"original": EVALS / "cases.jsonl", "heldout": EVALS / "cases_heldout.jsonl"}
RESULTS_DIR = EVALS / "results"
TIERS = ["haiku", "sonnet", "opus"]
CLASSIFIER_MODEL = "haiku"  # alias passed to the CLI; resolved IDs come from the CLI's usage report
CLASSIFIER_EFFORT = "low"


def plugin_system_prompt() -> str:
    src = (ROOT / "plugin/hooks/register.ts").read_text()
    m = re.search(r"const SYSTEM = `(.*?)`\n", src, re.S)
    if not m:
        sys.exit("could not find SYSTEM in plugin/hooks/register.ts")
    return m.group(1)


def load_cases(sets=("original", "heldout")) -> list[dict]:
    cases = []
    for name in sets:
        for line in CASE_FILES[name].read_text().splitlines():
            if line.strip():
                cases.append({**json.loads(line), "set": name})
    return cases


def classifier_input(case: dict) -> str:
    # Mirrors classify() in register.ts.
    tail = "\n".join(f"{role}: {text[:600]}" for role, text in case.get("context", [])[-4:] if text)
    return (f"<conversation_tail>\n{tail}\n</conversation_tail>\n\n" if tail else "") + \
        f"<new_message>\n{case['prompt'][:8000]}\n</new_message>"


def parse_reply(text: str) -> dict:
    """Same parsing as the plugin: first {...} span, model must be a known tier."""
    m = re.search(r"\{[\s\S]*\}", text)
    try:
        d = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        d = {}
    if not isinstance(d, dict):
        d = {}
    model = d.get("model") if d.get("model") in TIERS else None
    return {"model": model, "effort": d.get("effort"), "reason": d.get("reason", "")}


def classify(system: str, case: dict) -> dict:
    cmd = [
        "claude", "-p", "--model", CLASSIFIER_MODEL, "--effort", CLASSIFIER_EFFORT,
        "--system-prompt", system, "--tools", "", "--setting-sources", "",
        "--strict-mcp-config", "--no-session-persistence", "--output-format", "json",
        classifier_input(case),
    ]
    raw = ""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=90, stdin=subprocess.DEVNULL)
        body = json.loads(out.stdout)
        raw = body.get("result", "")
        return {"raw": raw, "parsed": parse_reply(raw), "error": None,
                "model_ids": sorted(body.get("modelUsage", {}) or {}),
                "usage": slim_usage(body.get("usage")), "cost": body.get("total_cost_usd") or 0.0}
    except Exception as e:
        return {"raw": raw, "parsed": {"model": None, "effort": None, "reason": ""},
                "error": str(e), "model_ids": [], "usage": None, "cost": 0.0}


def grade(model: str | None, expect: list[str]) -> str:
    """ok / under (too cheap) / over (too expensive) / failed (no usable answer)."""
    if model is None:
        return "failed"
    if model in expect:
        return "ok"
    return "under" if TIERS.index(model) < min(map(TIERS.index, expect)) else "over"


def summarize(case_rows: list[dict]) -> dict:
    """Totals over every run of the given cases, plus per-case stability."""
    totals = Counter({"ok": 0, "under": 0, "over": 0, "failed": 0})
    stable = 0
    for row in case_rows:
        for r in row["runs"]:
            totals[r["grade"]] += 1
        stable += len({r["parsed"]["model"] for r in row["runs"]}) == 1
    n = sum(totals.values())
    return {"cases": len(case_rows), "classifications": n, **totals,
            "accuracy": totals["ok"] / n if n else None,
            "stable_cases": stable,
            "stability": stable / len(case_rows) if case_rows else None}


USAGE_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


def slim_usage(usage):
    """Keep only the token counts; the CLI's full usage block is mostly noise."""
    if not isinstance(usage, dict):
        return None
    return {k: usage[k] for k in USAGE_KEYS if k in usage}


def dumps_results(data: dict) -> str:
    """JSON with the header indented and one line per case, so a run is ~65 lines, not ~10,000."""
    head = json.dumps({k: v for k, v in data.items() if k != "cases"}, indent=2)
    lines = ",\n".join("    " + json.dumps(c, separators=(", ", ": ")) for c in data["cases"])
    return head[:-2] + ',\n  "cases": [\n' + lines + "\n  ]\n}\n"


def results_path(day: str, directory: Path = RESULTS_DIR) -> Path:
    """evals/results/<day>.json, or <day>-2.json, -3, ... if that day already has a run."""
    p = directory / f"{day}.json"
    i = 2
    while p.exists():
        p = directory / f"{day}-{i}.json"
        i += 1
    return p


def claude_version() -> str | None:
    try:  # local, no API call
        return subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=30).stdout.strip() or None
    except Exception:
        return None


def fmt(s: dict) -> str:
    if not s["classifications"]:
        return "no cases"
    return (f"{s['ok']}/{s['classifications']} ({100 * s['accuracy']:.0f}%) correct tier, "
            f"{s['under']} under, {s['over']} over, {s['failed']} failed; "
            f"{s['stable_cases']}/{s['cases']} cases gave the same tier every run")


def main(argv: list[str] | None = None, classify_fn=classify) -> Path:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--only", help="comma-separated case ids")
    ap.add_argument("--set", choices=["all", "original", "heldout"], default="all")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--out-dir", type=Path, default=RESULTS_DIR)
    args = ap.parse_args(argv)

    cases = load_cases(("original", "heldout") if args.set == "all" else (args.set,))
    if args.only:
        keep = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in keep]
    system = plugin_system_prompt()
    started = datetime.now(timezone.utc)

    jobs = [(c, r) for c in cases for r in range(args.runs)]
    with ThreadPoolExecutor(args.workers) as pool:
        outs = list(pool.map(lambda j: classify_fn(system, j[0]), jobs))

    by_case: dict[str, list[dict]] = {}
    for (c, _), o in zip(jobs, outs):
        by_case.setdefault(c["id"], []).append(o)

    rows = []
    for c in cases:
        runs = [{**o, "grade": grade(o["parsed"]["model"], c["expect"])} for o in by_case[c["id"]]]
        ok = sum(r["grade"] == "ok" for r in runs)
        rows.append({"id": c["id"], "set": c["set"], "input": classifier_input(c), "prompt": c["prompt"],
                     "context": c.get("context"), "expect": c["expect"], "runs": runs,
                     "passed": ok == len(runs), "runs_passed": ok})
        picks = Counter(f"{r['parsed']['model']}/{r['parsed']['effort']}" for r in runs)
        flag = "" if ok == len(runs) else ("  <-- MISS" if ok == 0 else "  <-- unstable")
        print(f"{c['id']:>5}  expect {'/'.join(c['expect']):<13} got {', '.join(f'{k} x{v}' for k, v in picks.items())}{flag}")

    summary = {name: summarize([r for r in rows if r["set"] == name]) for name in ("original", "heldout")}
    summary["overall"] = summarize(rows)
    cost = sum(o["cost"] for o in outs)
    n = len(outs)

    print(f"\n{n} classifications, ${cost:.4f} total (${cost / max(n, 1):.5f} each)")
    for name in ("original", "heldout", "overall"):
        print(f"{name:>9}: {fmt(summary[name])}")

    args.out_dir.mkdir(parents=True, exist_ok=True)
    path = results_path(started.strftime("%Y-%m-%d"), args.out_dir)
    path.write_text(dumps_results({
        "timestamp": started.isoformat(),
        "finished": datetime.now(timezone.utc).isoformat(),
        "models": {
            "classifier_requested": CLASSIFIER_MODEL,
            "classifier_effort": CLASSIFIER_EFFORT,
            "classifier_resolved": sorted({m for o in outs for m in o["model_ids"]}),
        },
        "claude_cli_version": claude_version(),
        "router_prompt_sha256": hashlib.sha256(system.encode()).hexdigest(),
        "router_prompt_source": "plugin/hooks/register.ts (const SYSTEM)",
        "runs_per_case": args.runs,
        "filters": {"set": args.set, "only": args.only},
        "cost_usd": cost,
        "summary": summary,
        "cases": rows,
    }))
    print(f"details: {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")
    return path


if __name__ == "__main__":
    main()

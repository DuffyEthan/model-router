#!/usr/bin/env python3
"""End-to-end: load the real plugin into a headless Claude Code session started
on --model opus, send a prompt, and check which model actually answered.

Costs real answer tokens (roughly $0.05-0.15 for the whole default set).

    python3 evals/e2e.py
    python3 evals/e2e.py --only h1,o1
"""
import argparse
import json
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASE_FILES = [Path(__file__).with_name("cases.jsonl"), Path(__file__).with_name("cases_heldout.jsonl")]
DEFAULT = ["h1", "h5", "s1", "s4", "o1", "o3"]
FAMILY = {"haiku": "claude-haiku", "sonnet": "claude-sonnet", "opus": "claude-opus"}


def run(case: dict) -> tuple[dict, set[str], str]:
    with tempfile.TemporaryDirectory() as cwd:  # empty project: no CLAUDE.md, no files
        out = subprocess.run(
            ["claude", "-p", "--plugin-dir", str(ROOT / "plugin"), "--model", "opus",
             "--tools", "", "--no-session-persistence", "--output-format", "json", case["prompt"]],
            capture_output=True, text=True, timeout=600, cwd=cwd, stdin=subprocess.DEVNULL,
        )
    raw = out.stdout
    body = json.loads(raw[raw.index('{"'):])
    return case, set(body.get("modelUsage", {})), f"${body.get('total_cost_usd') or 0:.4f}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only")
    args = ap.parse_args()
    ids = args.only.split(",") if args.only else DEFAULT
    lines = [l for f in CASE_FILES for l in f.read_text().splitlines() if l.strip()]
    cases = [c for c in map(json.loads, lines) if c["id"] in ids and "context" not in c]
    passed = 0
    with ThreadPoolExecutor(4) as pool:
        for case, models, cost in pool.map(run, cases):
            ok = any(m.startswith(FAMILY[t]) for m in models for t in case["expect"])
            passed += ok
            print(f"{case['id']:>4}  expect {'/'.join(case['expect']):<13} answered by {', '.join(sorted(models)) or 'nothing'}  {cost}  {'ok' if ok else 'FAIL'}")
    print(f"\n{passed}/{len(cases)} answered by an expected model (session started on opus)")


if __name__ == "__main__":
    main()

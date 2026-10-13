#!/usr/bin/env python3
"""Model router for Claude Code.

Haiku (low effort) reads your prompt and picks the cheapest model + effort that
can handle it, then launches Claude Code with that choice.

Usage:
    route "your prompt"            # classify, then open interactive Claude Code
    route -p "your prompt"         # classify, then run headless (print mode)
    route --dry-run "your prompt"  # only show the routing decision
    route "your prompt" -- --permission-mode plan   # flags after -- go to claude
    echo "prompt" | route -p       # prompt from stdin
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

CLASSIFIER_MODEL = "haiku"
CLASSIFIER_EFFORT = "low"
FALLBACK = {"model": "sonnet", "effort": "medium", "reason": "classifier failed; using safe default"}
LOG_PATH = Path(os.environ.get("MODEL_ROUTER_LOG", Path.home() / ".model-router" / "log.jsonl"))

SYSTEM_PROMPT = """You are a router. You never answer the user's prompt. You only decide which Claude model and effort level should answer it, choosing the CHEAPEST option likely to produce a fully correct answer.

Models (cheapest to most expensive):
- haiku: simple Q&A, definitions, quick lookups, formatting, renaming, one-line or trivial code edits, shell one-liners, short summaries.
- sonnet: everyday coding, writing a function or small feature, debugging a clear error, explaining code, moderate refactors, writing tests, most docs.
- opus: hard multi-step reasoning, architecture/design decisions, large or cross-cutting refactors, subtle bugs, security review, ambiguous open-ended tasks, long agentic work across many files.

Effort: low (fast, little reasoning), medium (typical), high (careful reasoning), xhigh (hard agentic/coding work).

Rules: Prefer the cheaper model when in doubt between two adjacent tiers, unless the task is high-stakes (production data, security, irreversible actions). Judge the task's difficulty, not its length."""

SCHEMA = {
    "type": "object",
    "properties": {
        "model": {"type": "string", "enum": ["haiku", "sonnet", "opus"]},
        "effort": {"type": "string", "enum": ["low", "medium", "high", "xhigh"]},
        "reason": {"type": "string", "description": "One short sentence."},
    },
    "required": ["model", "effort", "reason"],
    "additionalProperties": False,
}


def classify(prompt: str) -> dict:
    cmd = [
        "claude", "-p",
        "--model", CLASSIFIER_MODEL,
        "--effort", CLASSIFIER_EFFORT,
        "--system-prompt", SYSTEM_PROMPT,  # replaces Claude Code's large default prompt
        "--tools", "",                     # no tool definitions in context
        "--setting-sources", "",           # skip user/project settings and hooks
        "--strict-mcp-config",             # no MCP servers
        "--no-session-persistence",
        "--output-format", "json",
        "--json-schema", json.dumps(SCHEMA),
        f"Route this prompt:\n<prompt>\n{prompt}\n</prompt>",
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        result = json.loads(out.stdout)
        decision = result.get("structured_output") or json.loads(result["result"])
        if decision.get("model") not in ("haiku", "sonnet", "opus"):
            raise ValueError(f"bad model: {decision!r}")
        decision["router_cost_usd"] = result.get("total_cost_usd")
        return decision
    except Exception as e:  # never block the user on a routing failure
        print(f"[router] classification failed ({e}); falling back", file=sys.stderr)
        return dict(FALLBACK)


def log(prompt: str, decision: dict) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a") as f:
            f.write(json.dumps({"ts": time.time(), "prompt": prompt[:200], **decision}) + "\n")
    except OSError:
        pass


def main() -> None:
    ap = argparse.ArgumentParser(description="Route a prompt to the cheapest capable Claude model.")
    ap.add_argument("prompt", nargs="*", help="prompt text (or pipe via stdin)")
    ap.add_argument("-p", "--print", action="store_true", help="run headless and print the answer")
    ap.add_argument("--dry-run", action="store_true", help="only show the routing decision")
    ap.add_argument("--model", help="skip routing and force a model")
    ap.add_argument("--effort", help="override the routed effort level")
    argv = sys.argv[1:]
    # Everything after "--" goes to claude untouched (needed for flags that take
    # a value, e.g. `-- --permission-mode plan`, which we can't tell from a prompt).
    passthrough: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, passthrough = argv[:i], argv[i + 1:]
    args, extra = ap.parse_known_args(argv)  # other unknown flags (e.g. --flag=value) too
    passthrough = extra + passthrough

    prompt = " ".join(args.prompt).strip()
    if not prompt and not sys.stdin.isatty():
        prompt = sys.stdin.read().strip()
    if not prompt:
        ap.error("no prompt given")

    if args.model:
        decision = {"model": args.model, "effort": args.effort or "medium", "reason": "forced"}
    else:
        decision = classify(prompt)
        if args.effort:
            decision["effort"] = args.effort
    log(prompt, decision)

    cost = decision.get("router_cost_usd")
    cost_str = f" (router cost ${cost:.5f})" if isinstance(cost, (int, float)) else ""
    print(f"[router] → {decision['model']} / {decision['effort']}: {decision['reason']}{cost_str}", file=sys.stderr)

    if args.dry_run:
        return

    cmd = ["claude", "--model", decision["model"], "--effort", decision["effort"], *passthrough]
    if args.print:
        cmd.append("-p")
    cmd.append(prompt)
    os.execvp("claude", cmd)  # hand the terminal over to Claude Code


if __name__ == "__main__":
    main()

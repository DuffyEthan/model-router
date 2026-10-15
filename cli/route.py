#!/usr/bin/env python3
"""Model router for Claude Code.

Haiku (low effort) reads your prompt and picks the cheapest model + effort that
can handle it, then launches Claude Code with that choice.

Usage:
    route "your prompt"            # classify, then open interactive Claude Code
    route -p "your prompt"         # classify, then run headless (print mode)
    route --dry-run "your prompt"  # only show the routing decision
    route --model sonnet "prompt"  # force model (sonnet is recommended baseline)
    route --floor sonnet "prompt"  # never route below sonnet
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
MODELS = ("haiku", "sonnet", "opus")
EFFORTS = ("low", "medium", "high", "xhigh")
DEFAULT_FLOOR = "haiku"  # no floor: every tier can be routed to
# Like the plugin: if routing fails, don't pick a model; claude uses the session default.
SESSION_DEFAULT = {"model": None, "effort": None, "reason": "classifier failed; using the session model"}
LOG_PATH = Path(os.environ.get("MODEL_ROUTER_LOG", Path.home() / ".model-router" / "log.jsonl"))

# Must match SYSTEM in plugin/hooks/register.ts word for word; tests/test_prompt_parity.py checks.
SYSTEM_PROMPT = """You are a router. You never answer the user's message. You only decide which Claude model and effort level should answer it, choosing the CHEAPEST option likely to produce a fully correct answer.

Models (cheapest to most expensive):
- haiku: simple Q&A, definitions, quick lookups, formatting, renaming, one-line or trivial code edits, shell one-liners, short summaries.
- sonnet: everyday coding, writing a function or small feature, debugging a clear error, explaining code, moderate refactors, writing tests, most docs.
- opus: hard multi-step reasoning, architecture/design decisions, large or cross-cutting refactors, subtle bugs, security review, ambiguous open-ended tasks, long agentic work across many files.

Effort: low (fast, little reasoning), medium (typical), high (careful reasoning), xhigh (hard agentic/coding work).

Rules:
- Prefer the cheaper model when in doubt between two adjacent tiers, unless the task is high-stakes (production data, security, irreversible actions).
- Judge the task's difficulty, not its length.
- You may see the end of the conversation for context. Route the NEW message. A short follow-up that approves or continues earlier work ("yes", "do it", "go ahead", "continue", "try again") gets the same model as that work, never a cheaper one.

Reply with only a JSON object, no prose: {"model": "haiku"|"sonnet"|"opus", "effort": "low"|"medium"|"high"|"xhigh", "reason": "<one short sentence>"}"""

SCHEMA = {
    "type": "object",
    "properties": {
        "model": {"type": "string", "enum": list(MODELS)},
        "effort": {"type": "string", "enum": list(EFFORTS)},
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
        # Same framing as the plugin; the CLI has no conversation tail to send.
        f"<new_message>\n{prompt}\n</new_message>",
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        result = json.loads(out.stdout)
        decision = result.get("structured_output") or json.loads(result["result"])
        if not isinstance(decision, dict) or decision.get("model") not in MODELS:
            raise ValueError(f"unknown model in reply: {decision!r}")
        effort = decision.get("effort")
        return {
            "model": decision["model"],
            # Same as the plugin: a known model with a bad effort runs at medium.
            "effort": effort if effort in EFFORTS else "medium",
            "reason": str(decision.get("reason") or ""),
            "router_cost_usd": result.get("total_cost_usd"),
        }
    except Exception as e:  # never block the user on a routing failure
        print(f"[router] classifier failed ({e}); using the session model", file=sys.stderr)
        return dict(SESSION_DEFAULT)


def log(prompt: str, decision: dict) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a") as f:
            f.write(json.dumps({"ts": time.time(), "prompt": prompt[:200], **decision}) + "\n")
    except OSError:
        pass


def apply_floor(decision: dict, floor: str) -> dict:
    """Raise a routed model below `floor` to `floor`, like the plugin's /router floor.

    A failed classification (model None) is left alone: claude keeps its session model.
    The effort is kept when valid (every tier accepts every routed effort), else medium.
    """
    model = decision.get("model")
    if model not in MODELS or MODELS.index(model) >= MODELS.index(floor):
        return decision
    effort = decision.get("effort")
    return {**decision, "model": floor, "effort": effort if effort in EFFORTS else "medium", "floored": True}


def main() -> None:
    ap = argparse.ArgumentParser(description="Route a prompt to the cheapest capable Claude model.")
    ap.add_argument("prompt", nargs="*", help="prompt text (or pipe via stdin)")
    ap.add_argument("-p", "--print", action="store_true", help="run headless and print the answer")
    ap.add_argument("--dry-run", action="store_true", help="only show the routing decision")
    ap.add_argument("--model",
                    help=f"skip routing and force a model ({', '.join(MODELS)}; sonnet recommended as baseline)")
    ap.add_argument("--effort", help=f"override the routed effort level ({', '.join(EFFORTS)})")
    ap.add_argument("--floor", default=DEFAULT_FLOOR,
                    help=f"never route below this model ({', '.join(MODELS)}; default {DEFAULT_FLOOR})")
    argv = sys.argv[1:]
    # Everything after "--" goes to claude untouched (needed for flags that take
    # a value, e.g. `-- --permission-mode plan`, which we can't tell from a prompt).
    passthrough: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, passthrough = argv[:i], argv[i + 1:]
    args, extra = ap.parse_known_args(argv)  # other unknown flags (e.g. --flag=value) too
    passthrough = extra + passthrough
    validate(args.model, args.effort, args.floor)  # reject bad overrides before reading stdin or classifying

    prompt = " ".join(args.prompt).strip()
    if not prompt and not sys.stdin.isatty():
        prompt = sys.stdin.read().strip()
    if not prompt:
        ap.error("no prompt given")

    if args.model is not None:
        decision = {"model": args.model, "effort": args.effort or "medium", "reason": "forced"}
    else:
        decision = apply_floor(classify(prompt), args.floor)
        if args.effort:
            decision["effort"] = args.effort
    log(prompt, decision)

    cost = decision.get("router_cost_usd")
    cost_str = f" (router cost ${cost:.5f})" if isinstance(cost, (int, float)) else ""
    choice = " / ".join(v for v in (decision["model"] or "session model", decision["effort"]) if v)
    print(f"[router] → {choice}: {decision['reason']}{cost_str}", file=sys.stderr)

    if args.dry_run:
        return

    os.execvp("claude", build_command(decision, passthrough, args.print, prompt))  # hand the terminal over


def validate(model, effort, floor=None) -> None:
    """Exit non-zero unless model/effort/floor are allowed values. None means "flag omitted"."""
    for kind, value, allowed in (("model", model, MODELS), ("effort", effort, EFFORTS), ("floor", floor, MODELS)):
        if value is not None and value not in allowed:
            sys.exit(f"route: invalid {kind} {value!r}; allowed: {', '.join(allowed)}")


def build_command(decision: dict, passthrough: list[str], headless: bool, prompt: str) -> list[str]:
    # Last check before exec: never hand claude a model or effort we don't know.
    validate(decision["model"], decision["effort"])
    cmd = ["claude"]
    # A None model/effort means "don't pass the flag", so claude keeps its session default.
    if decision["model"] is not None:
        cmd += ["--model", decision["model"]]
    if decision["effort"] is not None:
        cmd += ["--effort", decision["effort"]]
    cmd += passthrough
    if headless:
        cmd.append("-p")
    cmd.append(prompt)
    return cmd


if __name__ == "__main__":
    main()

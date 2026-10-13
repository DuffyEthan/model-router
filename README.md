# model-router

A tiny router for [Claude Code](https://claude.com/claude-code). Before your prompt goes to an expensive model, **Claude Haiku reads it on low effort and picks the cheapest model + effort level that can handle it**, then launches Claude Code with that choice.

```
$ route --dry-run "what does ls -la do"
[router] → haiku / low: Simple shell command definition lookup; trivial Q&A. (router cost $0.00039)

$ route --dry-run "write a python function that parses ISO dates and add pytest tests"
[router] → sonnet / low: Small, well-defined function plus tests is everyday coding. (router cost $0.00024)

$ route --dry-run "design the sharding strategy and zero-downtime migration for our monolith postgres db"
[router] → opus / xhigh: Architecture-level, high-stakes, irreversible data risks. (router cost $0.00029)
```

Routing costs roughly **$0.0003 per prompt** and a couple of seconds of latency.

## How it works

1. `route.py` calls `claude -p --model haiku --effort low` with:
   - a short router system prompt (replacing Claude Code's large default one),
   - no tools, no MCP servers, no settings/hooks, no session persistence — so the classification call stays tiny,
   - a JSON schema, so Haiku must return `{model, effort, reason}`.
2. It then `exec`s `claude --model <model> --effort <effort> "<your prompt>"`, handing your terminal over to Claude Code.
3. If classification fails for any reason, it falls back to `sonnet / medium` instead of blocking you.
4. Every decision is appended to `~/.model-router/log.jsonl` so you can audit routing quality.

It uses your existing Claude Code login, so it works with a Pro/Max subscription (saving usage limits) or an API key (saving dollars). No extra dependencies — Python 3 stdlib only.

## Install

Requires Python 3.8+ and the `claude` CLI on your `PATH`.

```bash
git clone https://github.com/DuffyEthan/model-router.git
cd model-router
chmod +x route.py
ln -s "$PWD/route.py" ~/.local/bin/route   # or anywhere on your PATH
```

## Usage

| Command | What it does |
|---|---|
| `route "prompt"` | Route, then open interactive Claude Code |
| `route -p "prompt"` | Route, then run headless and print the answer |
| `route --dry-run "prompt"` | Only show the routing decision |
| `route --model opus "prompt"` | Skip routing, force a model |
| `route --effort high "prompt"` | Route the model, override the effort |
| `echo "prompt" \| route -p` | Read the prompt from stdin |

Any other flags (e.g. `--permission-mode`, `--add-dir`) are passed straight through to `claude`.

## Tuning

Edit `SYSTEM_PROMPT` in [`route.py`](route.py) to change what counts as a haiku / sonnet / opus task. The defaults lean toward the cheaper model when two tiers are close, except for high-stakes work. Check `~/.model-router/log.jsonl` to see where prompts are landing, and set `MODEL_ROUTER_LOG` to change the log location.

## Caveats

- **Routes once per session.** Claude Code can't switch models per message from outside; use `/model` inside the session if a task turns out harder than expected.
- **Savings depend on your mix.** If most prompts are genuinely hard, they'll go to Opus anyway and you pay a small routing overhead.
- **Prompt caches are per model**, so routing each *new task* is cheap, but switching models mid-conversation would forfeit cached context.

## License

MIT

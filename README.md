# model-router

Cut your [Claude Code](https://claude.com/claude-code) costs and usage by sending each prompt to the **cheapest model that can handle it**. Before your prompt is answered, Claude Haiku reads it on low effort and picks haiku, sonnet or opus plus an effort level. Trivial questions go to Haiku. Hard architecture work goes to Opus.

It comes in two forms:

| | **Plugin** (recommended) | **`route` CLI** |
|---|---|---|
| Where it runs | Inside Claude Code: terminal, and the desktop app's Code tab | Your terminal, as a wrapper around `claude` |
| Routing | **Every prompt**, mid-session | Once, on the first prompt of a session |
| Install | `/plugin install` or `install.sh` | `install.sh` |

## Install

**One line**, which installs both the plugin and the `route` command:

```bash
curl -fsSL https://raw.githubusercontent.com/DuffyEthan/model-router/main/install.sh | sh
```

Use `--plugin-only` or `--cli-only` to install just one: `... | sh -s -- --plugin-only`.

**Or install just the plugin from inside Claude Code** (in a terminal session):

```
/plugin install model-router --marketplace DuffyEthan/model-router
```

Answer `y` to add the marketplace, then pick a scope. *User* scope enables the plugin in every session.

Requirements: Claude Code 2.1.275 or later for the plugin, and Python 3.8+ for the CLI. The plugin uses your normal Claude Code login, so it works on a Pro/Max subscription (saving usage limits) or with an API key (saving money).

## The plugin

Once it's installed, routing just happens. Each time you send a prompt:

1. **`prompt.submit`**: Haiku (low effort) classifies the new message. It also gets the last few messages of the conversation, so a follow-up like "yes, do it" inherits the difficulty of the task it refers to.
2. **`turn.step`**: every model request in that turn is rewritten to the routed model and effort. Subagents keep their own models.
3. The status line shows the decision, for example `router → sonnet · medium`.

If classification fails or times out, the turn runs on whatever model the session was already using.

Commands:

| Command | What it does |
|---|---|
| `/router` or `/router status` | Show on/off, the last decision and why, and the routing counts for this session |
| `/router off` | Stop routing and use the session's model (remembered across sessions) |
| `/router on` | Turn routing back on |

A tested example, with sessions started with `--model opus`:

```
"What is 2+2?"                                   → answered by claude-haiku-5-5  ($0.0007)
"Write is_palindrome(s) in Python"               → answered by claude-haiku-5-5  ($0.0003)
"Zero-downtime plan to shard a 5TB Postgres..."  → answered by claude-opus-5-5   ($0.0445)
```

## The `route` CLI

```bash
route "prompt"                 # route, then open interactive Claude Code on that model
route -p "prompt"              # route, then run headless and print the answer
route --dry-run "prompt"       # only show the decision
route --model opus "prompt"    # skip routing
echo "prompt" | route -p       # prompt from stdin
```

Other flags (`--permission-mode`, `--add-dir`, …) pass straight through to `claude`. Decisions are logged to `~/.model-router/log.jsonl`. Each routing call costs about $0.0003.

## Tuning

- **Routing rules:** edit `SYSTEM` in [`plugin/hooks/register.ts`](plugin/hooks/register.ts) (plugin) or `SYSTEM_PROMPT` in [`cli/route.py`](cli/route.py) (CLI). The defaults lean toward the cheaper model when two tiers are close, except for high-stakes work.
- **Model IDs:** the plugin needs full model IDs. When new models ship, update `MODEL_IDS` in `register.ts`.

## Caveats

- **Switching models costs cache.** Prompt caches are per model, so the first turn on a new model re-reads the whole conversation at the full input price. Switching *down* to Haiku is still cheaper, but in a long conversation a one-off drop from Opus to Sonnet can cost more than staying on Opus. If a long session keeps hopping between models, run `/router off`.
- **Savings depend on your mix.** If most of your prompts really are hard, they go to Opus anyway and you pay a small routing overhead.
- **The plugin API is early access.** The `turn.step` / `prompt.submit` hooks may change between Claude Code releases.

## Layout

```
.claude-plugin/marketplace.json   # makes this repo a plugin marketplace
plugin/                           # the Claude Code plugin
  .claude-plugin/plugin.json
  hooks/hooks.json
  hooks/register.ts
cli/route.py                      # the standalone `route` wrapper
install.sh                        # one-line installer
```

## License

MIT

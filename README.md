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

## Benefits and drawbacks

### Benefits

- **Big savings on easy prompts.** Haiku costs a small fraction of Opus per token: $0.10 vs $4 per million input tokens, and $0.50 vs $20 per million output tokens. Quick questions, renames, one-liners and small edits make up a lot of a typical session, and they get answered for a fraction of a cent.
- **Effort is routed too.** Even when a prompt needs a strong model, a simple one runs at `low` or `medium` effort instead of `xhigh`, which cuts thinking tokens. Changing effort never costs cache.
- **The context stays the same.** Only the model and effort on each request change. Whichever model answers sees the full conversation, CLAUDE.md, memory, tool results and file contents. Every current model has a 1M-token context window, so nothing gets cut off.
- **Routing is cheap and fails safe.** Each classification costs about $0.0003 and runs as a separate call that adds nothing to your conversation. If it fails or times out, the turn runs on the session's normal model.
- **Hard work still gets Opus.** The router leans toward the cheaper tier only when two tiers are close. High-stakes work (production data, security, irreversible actions) is pushed up, not down.
- **It works on subscriptions.** On Pro/Max, cheaper models use up less of your usage limits, so you hit them later.

### Drawbacks

- **Switching models can cost cache.** Prompt caches are per model. The first request on a model that hasn't seen the conversation re-reads it at full input price instead of the ~90% cheaper cached price. How much that matters depends on where you switch:

  | Switch (100K-token conversation, ~2K output) | First step | Later steps | Worth it? |
  |---|---|---|---|
  | Stay on Opus (cached) | ~$0.06 | ~$0.06 | n/a |
  | Opus → **Haiku** (from scratch) | ~$0.015 | ~$0.01 | **Yes, always.** Haiku's full price is below Opus's cached price. |
  | Opus → **Sonnet** (from scratch) | ~$0.27 | ~$0.04 | **Usually not** in long sessions. It only pays off after ~10+ steps on Sonnet. |

  Two things soften this. Caches survive switching: each model keeps its own for a few minutes (up to an hour in some setups), so hopping back to Opus soon afterwards is cheap. And early in a session the context is small, so switching is nearly free. The bad case is a **long conversation that drops from Opus to Sonnet for a turn or two**. If a long session keeps hopping between models, run `/router off`.
- **Hidden reasoning doesn't carry over.** On current models, a model's private reasoning ("thinking") from earlier turns can only be read by that same model. After a switch, the new model sees everything the previous one *said and did*, but not how it reasoned. That rarely matters, because conclusions end up in the visible text, but it can in long, delicate debugging sessions.
- **Haiku can misjudge a prompt.** A hard problem phrased simply can get routed too low, and the answer may be weaker. Check `/router status` to see the reasoning, re-ask with more detail, or run `/router off` for that stretch of work. It also adds about 1–3 seconds of latency per prompt.
- **It overrides your `/model` and `/effort` choices** while it's on. `/router off` hands control back.
- **Savings depend on your mix.** If most of your prompts really are hard, they go to Opus anyway and you pay the small routing overhead for nothing.
- **Subagents aren't routed.** They keep their own configured models and effort.
- **The plugin API is early access.** The `turn.step` / `prompt.submit` hooks may change between Claude Code releases, and model IDs in `register.ts` need updating when new models ship.
- **The CLI routes only once.** `route` picks a model for the whole session based on its first prompt. Use the plugin for per-prompt routing.

Prices are Anthropic API list prices as of October 2026. Cached reads are billed at ~0.1× input price, and cache writes at 1.25×–2× depending on TTL. On a subscription, usage limits follow roughly the same proportions.

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

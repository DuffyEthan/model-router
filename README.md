# model-router

Cut your [Claude Code](https://claude.com/claude-code) costs and usage by sending each prompt to the **cheapest model that can handle it**. Before your prompt is answered, Claude Haiku reads it on low effort and picks haiku, sonnet or opus plus an effort level. Trivial questions go to Haiku. Hard architecture work goes to Opus.

![Demo: a real Claude Code session started on Opus. The status line shows each prompt routed to haiku, sonnet or opus, and /router shows the tally.](docs/demo.gif)

*A real recording (made with [vhs](https://github.com/charmbracelet/vhs); script in [`docs/demo.tape`](docs/demo.tape)): a session started on Opus, with each prompt routed to the cheapest capable model. The last two prompts were interrupted after the routing decision to keep the demo short.*

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

**Or install just the plugin** from a terminal:

```bash
claude plugin install model-router --marketplace DuffyEthan/model-router
```

or from inside an interactive Claude Code terminal session:

```
/plugin install model-router --marketplace DuffyEthan/model-router
```

Answer `y` to add the marketplace, then pick a scope. *User* scope enables the plugin in every session.

**Claude desktop app:** the Code tab can't run `/plugin`, but it loads plugins installed at User scope. Install with either command above from a terminal, then start a new Code session and type `/router` to check it's on.

Requirements: Claude Code 2.1.275 or later for the plugin, and Python 3.8+ for the CLI. The plugin uses your normal Claude Code login, so it works on a Pro/Max subscription (saving usage limits) or with an API key (saving money).

## The plugin

Once it's installed, routing just happens.

### How it works, step by step

When you send a prompt, Haiku makes a quick decision first, then that turn's requests are rewritten to use the model and effort it picked. The plugin is one file, [`plugin/hooks/register.ts`](plugin/hooks/register.ts), which hooks into four points in Claude Code:

```
You type a prompt
   │
   ▼
① prompt.submit hook ── Haiku (low effort) ──► {"model":"sonnet","effort":"medium","reason":"..."}
   │                      reads: your new message + last ~4 messages (trimmed)
   │                      status line: "router → sonnet · medium"
   ▼
Claude Code builds the request as normal (full conversation, CLAUDE.md, tools…)
   │
   ▼
② turn.step hook ── rewrites ONLY model + effort ──► claude-sonnet-5-5 @ medium
   │                 (fires on every request in the turn: each step of the tool-call loop)
   ▼
Sonnet answers, edits files, runs tools… until the turn ends
   │
   ▼
Next prompt → back to ①, fresh decision
```

**① Classifying (`prompt.submit`).** This runs before your message enters the conversation.
- The plugin sends Haiku a short router prompt describing the three tiers: Haiku for trivial things, Sonnet for everyday coding, Opus for hard or high-stakes work.
- Haiku also gets the last few messages, so a follow-up like "yes, do it" is judged by the task it refers to.
- It's a separate one-off call (`$.model.complete`) with a 20-second timeout and a 300-token answer cap. It has no tools and no history, and nothing from it goes into your conversation.
- Haiku replies with JSON, which the plugin checks before using. Anything invalid means "no route", and the turn uses your session's normal model.
- The decision appears in the status line, for example `router → sonnet · medium`.

**② Rewriting (`turn.step`).** This runs just before each request Claude Code sends to the API.
- The plugin swaps in the chosen model's full name (e.g. `claude-sonnet-5-5`) and the chosen effort level.
- Everything else is locked by Claude Code: the conversation, the instructions, the tools. That's why context is never affected.
- One turn can make many requests, because the model calls a tool, reads the result and continues. All of them use the routed model.
- Requests from subagents are left alone.

**③ Startup (`session.start`).** This loads your on/off setting, which is saved across sessions, and registers the `/router` command.

**④ `/router` (`command.run`).** `on` and `off` turn routing on and off. `status` shows the last decision with Haiku's reason and how many prompts went to each model this session.

**Where the savings come from:** the model doing the work in that turn is what you pay for. Each Haiku check costs about $0.0003. If it sends a quick question to Haiku instead of Opus, that answer costs a small fraction of what it would have, and effort set to `low` cuts thinking tokens further. See [Benefits and drawbacks](#benefits-and-drawbacks) for when a switch gets expensive.

### Commands

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

How it works: it runs `claude -p --model haiku --effort low` once to get the same JSON decision as the plugin, then starts a normal `claude --model X --effort Y "prompt"` session. It's simpler than the plugin, but it only decides once per session.

To pass other flags to `claude`, put them after `--`: `route "prompt" -- --permission-mode plan`. Flags written as `--flag=value` also pass straight through. Decisions are logged to `~/.model-router/log.jsonl`. Each routing call costs about $0.0003.

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
cli/tests/                        # CLI unit tests
plugin/tests/                     # plugin tests (claude plugin test)
evals/                            # routing-quality and end-to-end evals (real model calls)
install.sh                        # one-line installer
```

## Tests

```bash
make test          # everything
make test-cli      # Python unit tests for the route CLI (stdlib only)
make test-plugin   # validates and tests the plugin with `claude plugin test` (no API calls)
```

- `plugin/tests/router.test.ts`: 15 tests that run the real plugin against a mocked engine. They cover routing to each tier, full model IDs (including provider prefixes), fail-safe behaviour (garbage replies, bad JSON, unknown models, a classifier that fails or throws), subagents being left alone, slash commands not being classified, and `/router on|off|status` including the saved off setting.
- `cli/tests/test_route.py`: 18 tests for the CLI's classification, fallbacks, flag handling, stdin, and logging.

CI runs the CLI tests and basic checks on every push. The plugin tests need the `claude` CLI, so run them locally.

These tests fake Haiku's replies, so they check the *plumbing*, not whether Haiku picks well. Routing quality is checked by the evals below.

## Evals: does it route well?

The evals make real model calls and cost a little.

```bash
python3 evals/routing_eval.py   # ~100 real Haiku classifications, ~$0.02
python3 evals/e2e.py            # real plugin in real sessions; checks which model answered
```

- **`evals/routing_eval.py`** runs the plugin's actual router prompt (read from `register.ts`) on 33 labelled prompts in [`evals/cases.jsonl`](evals/cases.jsonl): 10 easy, 10 everyday coding, 10 hard, and 3 follow-ups with conversation context. Each runs 3 times to check consistency. It reports correct-tier rate, **under-routing** (too cheap, so a risk of weak answers) and **over-routing** (too expensive, so wasted money). Some borderline cases accept two tiers.
- **`evals/e2e.py`** starts headless Claude Code sessions on `--model opus` with the plugin loaded, sends real prompts, and checks from the session's usage report which model actually answered.

Latest results (Claude Code 2.1.296, Haiku 5.5, October 2026):

| Eval | Result |
|---|---|
| Routing quality | **96/99 (97%)** correct tier; **0 over-routed**; 3 under-routed, all one borderline case (a SQL "top 3 orders per customer" query sent to Haiku) |
| Follow-ups ("yes, do it" after an Opus plan) | Stays on Opus 6/6, after the follow-up rule was tightened. Before that, it dropped to Sonnet 1/3 of the time. |
| Cost per classification | ~$0.00017 |
| End-to-end (6 prompts, sessions started on Opus) | **6/6** answered by the expected model: Haiku for both easy prompts, Sonnet for both coding prompts, Opus for both hard ones. Cost $0.01–0.05 per prompt, including the answer. |

To tune the router, change `SYSTEM` in `register.ts`, rerun `routing_eval.py`, and compare. Add your own prompts to `cases.jsonl` so the eval matches how you actually work.

## License

MIT

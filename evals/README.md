# Evals

Two scripts make real model calls and cost money; one test file is offline.

| File | What it does | Calls the API? |
|---|---|---|
| `routing_eval.py` | Sends each labelled prompt to the real Haiku classifier with the plugin's real router prompt and checks the tier it picks | Yes (Haiku only) |
| `e2e.py` | Starts headless Claude Code sessions on `--model opus` with the plugin loaded and checks which model actually answered | Yes (Haiku, plus Sonnet/Opus answers) |
| `test_routing_eval.py` | Tests the eval's own plumbing with faked classifier replies | No |

## Cases

- **`cases.jsonl`** (original set, ids `h*`, `s*`, `o*`, ...). Written alongside the router prompt by the same person, so the prompt was in effect tuned against them.
- **`cases_heldout.jsonl`** (held-out set, ids `ho*`). 32 prompts written *before* looking at the router prompt, the README or the original cases (only the first three lines of `cases.jsonl` were read, to learn the format). They lean towards ambiguous prompts; 13 of them accept two tiers.

Each line is `{"id": ..., "prompt": ..., "expect": [tier, ...]}`, with an optional `context` list of `[role, text]` pairs for follow-ups. A run counts as correct if the classifier's tier is anywhere in `expect`. Only the tier is checked; the effort level is recorded but not graded.

Do not tune the router prompt against the held-out cases. Once you have looked at held-out misses and changed the prompt because of them, they are no longer held out; write a fresh batch.

## Running

```sh
python3 evals/routing_eval.py                        # all 65 cases, 3 runs each
python3 evals/routing_eval.py --set heldout          # just the held-out set
python3 evals/routing_eval.py --runs 1 --only h1,ho1 # quick spot check
python3 evals/e2e.py                                 # 6 default cases, real sessions
python3 -m unittest discover -s evals -p 'test_*.py' # offline, free
```

Both paid scripts shell out to the `claude` CLI, so they use whatever account it is logged in to.

## Where results go

`routing_eval.py` writes `evals/results/<YYYY-MM-DD>.json` (UTC date). A second run on the same day writes `<date>-2.json`, then `-3`, and so on, so nothing is overwritten. These files are meant to be committed, so changes in routing can be compared across router-prompt and model versions.

Each file contains:

- `timestamp` / `finished`
- `models`: the classifier alias requested (`haiku`), its effort, and the resolved model IDs the CLI reported (`classifier_resolved`)
- `claude_cli_version`
- `router_prompt_sha256`: hash of the exact `SYSTEM` text read from `plugin/hooks/register.ts`. Two result files are only directly comparable if this matches (and the models match).
- `runs_per_case`, `filters`, `cost_usd`
- `summary`: `original`, `heldout` and `overall`, each with counts of ok / under / over / failed, `accuracy` and `stability`
- `cases`: for every case its id, set, exact classifier input, expected tiers, and every run's raw reply, parsed `{model, effort, reason}`, resolved model IDs, token usage, cost and grade, plus whether all runs passed

`e2e.py` only prints to the console; it does not write a results file.

## What each metric means

**Correct-tier rate (original set).** How often the classifier's tier is one the label accepts.
- It measures agreement with labels written by the person who wrote the router prompt, i.e. whether the router does what its author intended. It is a routing-consistency check.
- It does *not* measure answer quality. A "correct" route to Haiku can still give a bad answer, and an "over-routed" Opus answer is not wrong, just more expensive.
- It is optimistic. The prompt was developed with these cases in view, so they are effectively training data.

**Correct-tier rate (held-out set).** The same measure on cases written without seeing the prompt.
- It is less biased than the original set and a better guess at how the router does on prompts nobody tuned for.
- It is still one person's labels. Tier choice is a judgement call, which is why many held-out cases accept two tiers. 32 cases is also a small sample: one case is about 3 points.
- It is weighted towards ambiguous prompts on purpose, so expect it to come out lower than the original set. That gap is not, by itself, a regression.

**Overall.** Both sets pooled. Useful as a single number to track, but read the two halves separately.

**Under-routed / over-routed.** Of the misses, whether the pick was cheaper than every accepted tier (risk of a weak answer) or dearer than every accepted tier (wasted money). Under-routing is usually the worse failure. **Failed** means no usable answer: CLI error, timeout, or a reply the plugin's parser would reject. In the plugin, that falls back to not routing.

**Stability.** The share of cases where every run picked the same tier. It measures how deterministic the classifier is on a given prompt, not whether that tier is right: a case can be stably wrong. Instability marks a prompt near a decision boundary, where routing in real use will be a coin flip. With the default 3 runs it is a coarse signal.

**e2e pass rate.** Whether the plugin, loaded into a real Claude Code session, actually switched the answering model to an expected tier. It checks the hook wiring end to end (registration, the classifier call, the model switch), not classifier quality; it runs only six cases.

## Cost

Prices from claude.com/pricing (fetched 2026-10-14). Haiku 5.5: $0.10 per million input tokens, $0.50 per million output tokens (prompts under 100K tokens).

**`routing_eval.py`, full default run:** 65 cases × 3 runs = 195 classifications.

- Per call, by token count: the router prompt is about 1,440 characters (roughly 360 tokens), the classifier input averages about 120 characters (roughly 35 tokens), so about 400 input tokens; the JSON reply is about 40 to 100 output tokens. That is about $0.00004 input + $0.00002 to $0.00005 output, so roughly **$0.00007 per call, about $0.015 for the run**.
- Earlier runs measured about $0.0002 to $0.0003 per call as reported by the CLI's `total_cost_usd`, which includes whatever the CLI adds on top of the bare prompt. On that basis the run is **about $0.04 to $0.06**.
- Plan on **under $0.10**. The script prints the actual cost and stores it in the results file.

**`e2e.py`, default six cases:** about $0.05 to $0.15. Most of the cost is the real Sonnet/Opus answers (Sonnet 5.5 $2/$10, Opus 5.5 $4/$20 per million input/output tokens), not the classifier.

**Both together: roughly $0.10 to $0.25.**

# Savings reports

Each file here is a short, approximate write-up of what model routing cost and saved over one date range, produced by `scripts/router_report.py`. Files are named `START_to_END.md` (UTC, inclusive). Only aggregate figures are committed: no project names, paths, session IDs or prompt text.

## Reproduce

From the repo root, against your own data:

```
python3 scripts/router_report.py --since 2026-10-19 --until 2026-10-21 --simulate all
```

It reads transcripts from `~/.claude/projects` and the routing log from `~/.claude/plugins/store/model-router_*.json`, so you get your own numbers, not the ones in these files. Add `--json` for machine-readable output. `python3 scripts/router_report.py --help` lists the other options.

## Caveats

- One user's usage over a few days. Treat the figures as an example, not a benchmark.
- Costs are API-equivalent at list prices. Pro and Max subscribers do not pay per token, so these are not what a subscriber is billed.
- The Opus-only baseline assumes Opus would have produced the same output length. It says nothing about answer quality.
- The override rate (`/model` changes and `/router off` per user prompt) is a rough quality proxy only.
- Simulated policies re-price the same requests with a modelled cache, so they are approximate. Each report prints a calibration line showing how far the model drifts from the observed figures.
- Time spent with the router turned off counts as unrouted Opus in the actual cost, which lowers the measured savings.

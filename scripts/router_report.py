#!/usr/bin/env python3
"""Measure what model routing actually costs and saves.

Joins the model-router plugin's routing log with Claude Code's session transcripts
(JSON Lines under ~/.claude/projects/) and prints, for a date range:

  * share of prompts (routing log) and of API requests and tokens (transcripts) by model;
  * actual cost at API list prices, labelled "API-equivalent" (Pro/Max plans do not pay
    per token);
  * an Opus-only counterfactual: the same tokens priced at Opus, with the cache writes
    caused by a model switch counted as cache reads (staying on Opus would have hit the
    cache);
  * net savings, with the switch cache penalty and the classifier overhead on lines of
    their own;
  * the override rate (/model changes and /router off per user prompt), a rough quality
    proxy only.

Stdlib only, Python 3.9+. Prices come from plugin/hooks/prices.json, the one price table
the plugin's /router stats reads too. Nothing here reads or prints message content.
"""

import argparse
import datetime as dt
import glob
import json
import os
import re
import sys
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PRICES = os.path.join(HERE, os.pardir, "plugin", "hooks", "prices.json")
MTOK = 1_000_000.0
TTL_5M = 5 * 60
TTL_1H = 60 * 60


# ---------------------------------------------------------------------------
# Prices


def load_prices(path: str = DEFAULT_PRICES) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


_PREFIX = re.compile(r"^(?:(?:us|us-gov|eu|apac|jp|au|global)\.)?anthropic\.")
_DATED = re.compile(r"-\d{8}$")


def canonical_model(model: str) -> str:
    """claude-haiku-4-5-20251001 -> claude-haiku-4-5; us.anthropic.claude-opus-5-5 -> claude-opus-5-5."""
    m = _PREFIX.sub("", model or "")
    m = m.split("@", 1)[0]
    m = re.sub(r"\[[^\]]*\]$", "", m)
    m = re.sub(r"-v\d+(?::\d+)?$", "", m)
    return _DATED.sub("", m)


def rate_card(prices: Dict[str, Any], model: str, prompt_tokens: int) -> Optional[Dict[str, float]]:
    """The per-MTok rates that apply to one request, or None when the model is unpriced."""
    row = prices["models"].get(canonical_model(model))
    if row is None:
        return None
    lc = row.get("long_context")
    if lc and prompt_tokens > lc["over_prompt_tokens"]:
        return lc
    return row


def price_tokens(rates: Dict[str, float], t: Dict[str, float], mult: float = 1.0) -> float:
    """USD for a token bundle {input, cw5m, cw1h, cache_read, output}."""
    return (
        t["input"] * rates["input"]
        + t["cw5m"] * rates["cache_write_5m"]
        + t["cw1h"] * rates["cache_write_1h"]
        + t["cache_read"] * rates["cache_read"]
        + t["output"] * rates["output"]
    ) * mult / MTOK


# ---------------------------------------------------------------------------
# Time


def parse_ts(s: Any) -> Optional[dt.datetime]:
    if not isinstance(s, str) or not s:
        return None
    try:
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t


def day_bounds(since: Optional[str], until: Optional[str]) -> Tuple[Optional[dt.datetime], Optional[dt.datetime]]:
    """[since 00:00 UTC, until+1 00:00 UTC): both dates inclusive."""
    lo = hi = None
    if since:
        lo = dt.datetime.fromisoformat(since).replace(tzinfo=dt.timezone.utc)
    if until:
        hi = dt.datetime.fromisoformat(until).replace(tzinfo=dt.timezone.utc) + dt.timedelta(days=1)
    return lo, hi


def in_range(t: Optional[dt.datetime], lo: Optional[dt.datetime], hi: Optional[dt.datetime]) -> bool:
    if t is None:
        return False
    return (lo is None or t >= lo) and (hi is None or t < hi)


# ---------------------------------------------------------------------------
# Transcripts


def _int(d: Any, k: str) -> int:
    v = d.get(k) if isinstance(d, dict) else None
    return v if isinstance(v, int) and v >= 0 else 0


def usage_tokens(u: Dict[str, Any]) -> Dict[str, int]:
    cw = _int(u, "cache_creation_input_tokens")
    split = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else None
    if split is not None:
        cw5 = _int(split, "ephemeral_5m_input_tokens")
        cw1 = _int(split, "ephemeral_1h_input_tokens")
        if cw5 + cw1 != cw:  # inconsistent split: trust the total, unsplit part as 5m
            cw5 = max(0, cw - cw1)
    else:
        cw5, cw1 = cw, 0  # no split recorded: the default 5-minute cache
    return {
        "input": _int(u, "input_tokens"),
        "cw5m": cw5,
        "cw1h": cw1,
        "cache_read": _int(u, "cache_read_input_tokens"),
        "output": _int(u, "output_tokens"),
    }


def transcript_files(projects_dir: str) -> List[str]:
    return sorted(glob.glob(os.path.join(projects_dir, "**", "*.jsonl"), recursive=True))


def read_requests(projects_dir: str, stats: Dict[str, int]) -> List[Dict[str, Any]]:
    """One dict per API request (assistant message), deduplicated by message id.

    Claude Code writes one line per content block of a response, each repeating the
    message id and usage; a resumed or forked session repeats earlier lines too. The
    last line of a message within a file wins (its usage is final); a message id seen
    in an earlier file is not counted again.
    """
    seen_ids = set()
    out: List[Dict[str, Any]] = []
    for path in transcript_files(projects_dir):
        stats["files"] += 1
        stem = os.path.splitext(os.path.basename(path))[0]
        in_file: Dict[str, Dict[str, Any]] = {}
        order: List[str] = []
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            stats["unreadable_files"] += 1
            continue
        with fh:
            for n, line in enumerate(fh):
                line = line.strip()
                if not line:
                    continue
                stats["lines"] += 1
                try:
                    d = json.loads(line)
                except ValueError:
                    stats["malformed_lines"] += 1
                    continue
                if not isinstance(d, dict):
                    stats["malformed_lines"] += 1
                    continue
                if d.get("type") != "assistant":
                    continue
                msg = d.get("message")
                if not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict):
                    stats["malformed_lines"] += 1
                    continue
                model = msg.get("model")
                if not isinstance(model, str) or not model:
                    stats["malformed_lines"] += 1
                    continue
                if model == "<synthetic>":  # an error placeholder Claude Code wrote; no API call
                    stats["synthetic"] += 1
                    continue
                ts = parse_ts(d.get("timestamp"))
                if ts is None:
                    stats["malformed_lines"] += 1
                    continue
                mid = msg.get("id") or d.get("requestId") or d.get("uuid") or f"{path}:{n}"
                u = msg["usage"]
                rec = {
                    "id": mid,
                    "file": path,
                    "session": d.get("sessionId") or stem,
                    "chain": d.get("agentId") or "",
                    "model": model,
                    "ts": ts,
                    "tokens": usage_tokens(u),
                    "geo": u.get("inference_geo"),
                    "speed": u.get("speed"),
                }
                if mid in in_file:
                    stats["duplicate_lines"] += 1
                    rec["ts"] = in_file[mid]["ts"]
                    in_file[mid] = rec
                    continue
                if mid in seen_ids:
                    stats["duplicate_lines"] += 1
                    continue
                in_file[mid] = rec
                order.append(mid)
        for mid in order:
            seen_ids.add(mid)
            out.append(in_file[mid])
    return out


def mark_switches(requests: List[Dict[str, Any]]) -> None:
    """Sets r["switch_reads"]: cache-write tokens of r that an unbroken Opus session would
    have read from the cache instead.

    Within one request chain (a transcript file's main thread, or one subagent), the
    first request after the model changed paid to write the cache again. Up to the
    previous request's whole context (input + cache reads + cache writes + output, the
    prefix the cache held) counts as a read in the counterfactual, provided the cache
    would still have been warm: the gap since that request is within the TTL the write
    used (5 minutes, or 1 hour when it wrote 1-hour cache). The rest of the write is new
    content, a write either way.
    """
    chains: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for r in requests:
        chains[(r["file"], r["chain"])].append(r)
    for chain in chains.values():
        chain.sort(key=lambda r: r["ts"])
        prev = None
        for r in chain:
            r["switch_reads"] = 0
            t = r["tokens"]
            cw = t["cw5m"] + t["cw1h"]
            if prev is not None and canonical_model(prev["model"]) != canonical_model(r["model"]) and cw:
                ttl = TTL_1H if t["cw1h"] else TTL_5M
                gap = (r["ts"] - prev["ts"]).total_seconds()
                if gap <= ttl:
                    p = prev["tokens"]
                    held = p["input"] + p["cache_read"] + p["cw5m"] + p["cw1h"] + p["output"]
                    r["switch_reads"] = min(cw, held)
            prev = r


# ---------------------------------------------------------------------------
# Routing log


def default_config_dir() -> str:
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")


def default_log_paths(config_dir: str) -> List[str]:
    """The plugin's $.store file(s) (holding the log under "log"), and /router export's default file."""
    paths = sorted(glob.glob(os.path.join(config_dir, "plugins", "store", "model-router_*.json")))
    export = os.path.join(config_dir, "model-router", "routing-log.jsonl")
    if os.path.exists(export):
        paths.append(export)
    return paths


def read_log(paths: Iterable[str], stats: Dict[str, int]) -> List[Dict[str, Any]]:
    """Routing-log records from a $.store file ({"log": [...]}), a JSON array, or JSON Lines."""
    seen = set()
    out: List[Dict[str, Any]] = []
    for path in paths:
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            stats["log_unreadable"] += 1
            continue
        items: List[Any] = []
        try:
            whole = json.loads(text)
            if isinstance(whole, dict):
                items = whole.get("log") if isinstance(whole.get("log"), list) else []
            elif isinstance(whole, list):
                items = whole
            else:
                items = [whole]
        except ValueError:
            for line in text.splitlines():
                if not line.strip():
                    continue
                try:
                    items.append(json.loads(line))
                except ValueError:
                    stats["log_malformed"] += 1
        for it in items:
            if not isinstance(it, dict) or it.get("kind") not in ("prompt", "override"):
                stats["log_malformed"] += 1
                continue
            key = json.dumps(it, sort_keys=True)
            if key in seen:  # the same record in the store and in an export
                continue
            seen.add(key)
            out.append(it)
    return out


# ---------------------------------------------------------------------------
# Report


def empty_bundle() -> Dict[str, float]:
    return {"input": 0, "cw5m": 0, "cw1h": 0, "cache_read": 0, "output": 0}


def total(t: Dict[str, float]) -> float:
    return t["input"] + t["cw5m"] + t["cw1h"] + t["cache_read"] + t["output"]


def build_report(
    requests: List[Dict[str, Any]],
    log: List[Dict[str, Any]],
    prices: Dict[str, Any],
    lo: Optional[dt.datetime] = None,
    hi: Optional[dt.datetime] = None,
    all_sessions: bool = False,
) -> Dict[str, Any]:
    log = [r for r in log if in_range(parse_ts(r.get("ts")), lo, hi)]
    prompts = [r for r in log if r.get("kind") == "prompt"]
    overrides = [r for r in log if r.get("kind") == "override"]
    logged_sessions = {r.get("session") for r in prompts if r.get("session")}

    mark_switches(requests)  # over every request, so a switch just before `lo` still counts
    reqs = [r for r in requests if in_range(r["ts"], lo, hi)]
    scope = "all sessions"
    if logged_sessions and not all_sessions:
        reqs = [r for r in reqs if r["session"] in logged_sessions]
        scope = "sessions in the routing log"

    cf_model = prices["counterfactual_model"]
    us_mult = prices.get("us_inference_multiplier", 1.0)
    by_model: Dict[str, Dict[str, Any]] = {}
    unpriced: Dict[str, Dict[str, Any]] = {}
    actual = counterfactual = penalty = 0.0

    for r in reqs:
        t = r["tokens"]
        prompt_tokens = t["input"] + t["cache_read"] + t["cw5m"] + t["cw1h"]
        mult = us_mult if r.get("geo") == "us" else 1.0
        rates = rate_card(prices, r["model"], prompt_tokens)
        if rates is not None and r.get("speed") not in (None, "standard"):
            rates = None  # fast mode is not in the price table
        name = canonical_model(r["model"])
        if rates is None:
            row = unpriced.setdefault(name, {"requests": 0, "tokens": 0})
            row["requests"] += 1
            row["tokens"] += total(t)
            continue
        cost = price_tokens(rates, t, mult)
        actual += cost

        # Switch penalty: what re-writing the cache cost over reading it, on this model.
        s = r.get("switch_reads", 0)
        cw = t["cw5m"] + t["cw1h"]
        if s:
            s5 = s * t["cw5m"] / cw
            s1 = s - s5
            penalty += (s5 * rates["cache_write_5m"] + s1 * rates["cache_write_1h"] - s * rates["cache_read"]) * mult / MTOK

        # Counterfactual: the same tokens at Opus, switch writes read from the cache.
        cf = dict(t)
        if s:
            cf["cw5m"] = t["cw5m"] - s * t["cw5m"] / cw
            cf["cw1h"] = t["cw1h"] - s * t["cw1h"] / cw
            cf["cache_read"] = t["cache_read"] + s
        cf_rates = rate_card(prices, cf_model, prompt_tokens)
        counterfactual += price_tokens(cf_rates, cf, mult)

        row = by_model.setdefault(name, {"requests": 0, "tokens": empty_bundle(), "cost": 0.0, "switch_requests": 0})
        row["requests"] += 1
        row["cost"] += cost
        row["switch_requests"] += 1 if s else 0
        for k in t:
            row["tokens"][k] += t[k]

    # Classifier overhead: the log's own usage per call, priced at the classifier model.
    calls = [r for r in prompts if r.get("outcome") in ("ok", "failed", "timeout")]
    with_usage = [r for r in calls if isinstance(r.get("usage"), dict)]
    clf_cost = 0.0
    for r in with_usage:
        u = r["usage"]
        t = {"input": u.get("in", 0), "cw5m": u.get("cw", 0), "cw1h": 0, "cache_read": u.get("cr", 0), "output": u.get("out", 0)}
        rates = rate_card(prices, prices["classifier_model"], t["input"] + t["cw5m"] + t["cache_read"])
        clf_cost += price_tokens(rates, t)
    clf_estimated = False
    if with_usage and len(with_usage) < len(calls):
        clf_cost *= len(calls) / len(with_usage)  # calls that reported no usage: assume the mean
        clf_estimated = True

    user_prompts = [r for r in prompts if r.get("origin") == "user"]
    n_model = sum(1 for r in overrides if r.get("action") == "model")
    n_off = sum(1 for r in overrides if r.get("action") == "router-off")
    tier_counts: Dict[str, int] = defaultdict(int)
    origin_counts: Dict[str, int] = defaultdict(int)
    other_kinds: Dict[str, int] = defaultdict(int)
    outcome_counts: Dict[str, int] = defaultdict(int)
    for r in prompts:
        tier_counts[str(r.get("tier"))] += 1
        origin_counts[str(r.get("origin"))] += 1
        outcome_counts[str(r.get("outcome"))] += 1
        if r.get("origin") == "other":
            other_kinds[str(r.get("originKind"))] += 1

    priced_requests = sum(m["requests"] for m in by_model.values())
    priced_tokens = sum(total(m["tokens"]) for m in by_model.values())
    return {
        "scope": scope,
        "requests": len(reqs),
        "priced_requests": priced_requests,
        "priced_tokens": priced_tokens,
        "by_model": by_model,
        "unpriced": unpriced,
        "actual_usd": actual,
        "switch_penalty_usd": penalty,
        "classifier_calls": len(calls),
        "classifier_usd": clf_cost if with_usage else None,
        "classifier_estimated": clf_estimated,
        "counterfactual_usd": counterfactual,
        "net_savings_usd": counterfactual - actual - (clf_cost if with_usage else 0.0),
        "prompts": len(prompts),
        "prompt_tiers": dict(tier_counts),
        "prompt_origins": dict(origin_counts),
        "other_origin_kinds": dict(other_kinds),
        "outcomes": dict(outcome_counts),
        "mid_turn": sum(1 for r in prompts if r.get("midTurn")),
        "user_prompts": len(user_prompts),
        "overrides_model": n_model,
        "overrides_router_off": n_off,
        "override_rate": (n_model + n_off) / len(user_prompts) if user_prompts else None,
    }


def _pct(n: float, d: float) -> str:
    return f"{100.0 * n / d:5.1f}%" if d else "    —"


def _usd(x: Optional[float]) -> str:
    return "unknown" if x is None else f"${x:,.4f}"


def format_report(rep: Dict[str, Any], stats: Dict[str, int], prices: Dict[str, Any], label: str) -> str:
    L: List[str] = []
    L.append(f"Model router report — {label}")
    L.append(
        f"Transcripts: {stats['files']} files, {stats['lines']} lines, "
        f"{stats['malformed_lines']} malformed lines skipped, {stats['duplicate_lines']} duplicate lines merged, "
        f"{stats['synthetic']} synthetic messages skipped"
    )
    L.append(f"Scope: {rep['scope']}; {rep['requests']} API requests in range")
    if not rep["prompts"]:
        L.append("Routing log: no records in range (prompt shares, classifier overhead and overrides unavailable).")
        L.append(
            "  Without it every session is counted, including models picked by hand, so the savings line"
        )
        L.append("  compares against Opus-only, not routing against no routing.")
    else:
        L.append(f"Routing log: {rep['prompts']} prompts")
    L.append("")

    if rep["prompts"]:
        L.append("Prompts by route (routing log):")
        for tier in ("haiku", "sonnet", "opus", "session"):
            n = rep["prompt_tiers"].get(tier, 0)
            name = "session model (not routed)" if tier == "session" else tier
            L.append(f"  {name:<28}{n:>7}  {_pct(n, rep['prompts'])}")
        o = rep["prompt_origins"]
        kinds = ", ".join(f"{k} {v}" for k, v in sorted(rep["other_origin_kinds"].items()))
        L.append(
            f"  origins: user {o.get('user', 0)}, slash {o.get('slash', 0)}, other {o.get('other', 0)}"
            + (f" ({kinds})" if kinds else "")
            + f"; mid-turn {rep['mid_turn']}"
        )
        oc = rep["outcomes"]
        L.append(
            "  classifier: " + ", ".join(f"{k} {oc.get(k, 0)}" for k in ("ok", "failed", "timeout", "skipped"))
        )
        L.append("")

    L.append("API requests and tokens by model (transcripts):")
    L.append(f"  {'model':<22}{'requests':>9}{'share':>8}{'tokens':>15}{'share':>8}{'API-equiv.':>13}")
    for name, m in sorted(rep["by_model"].items(), key=lambda kv: -kv[1]["cost"]):
        label_ = prices["models"].get(name, {}).get("label", name)
        tok = total(m["tokens"])
        L.append(
            f"  {label_:<22}{m['requests']:>9}{_pct(m['requests'], rep['priced_requests']):>8}"
            f"{int(tok):>15,}{_pct(tok, rep['priced_tokens']):>8}{_usd(m['cost']):>13}"
        )
    for name, m in sorted(rep["unpriced"].items()):
        L.append(f"  {name:<22}{m['requests']:>9}{'':>8}{int(m['tokens']):>15,}{'':>8}{'unpriced':>13}")
    if rep["unpriced"]:
        L.append("  (unpriced models are left out of every cost line below)")
    L.append("")

    L.append("Cost at API list prices (API-equivalent: Pro/Max plans do not pay per token):")
    cf_label = prices["models"][prices["counterfactual_model"]]["label"]
    est = " (partly estimated)" if rep["classifier_estimated"] else ""
    net = rep["net_savings_usd"]
    rows = [
        ("Actual", _usd(rep["actual_usd"]), ""),
        ("  of which switch cache penalty", _usd(rep["switch_penalty_usd"]), ""),
        (f"Classifier overhead ({rep['classifier_calls']} calls)", _usd(rep["classifier_usd"]), est),
        (f"Counterfactual: {cf_label} only", _usd(rep["counterfactual_usd"]), ""),
        ("Net savings", _usd(net), f"  ({_pct(net, rep['counterfactual_usd']).strip()} of counterfactual)"),
    ]
    for name, value, note in rows:
        L.append(f"  {name:<36}{value:>14}{note}")
    L.append("  (net savings = counterfactual - actual - classifier overhead)")
    if rep["classifier_usd"] is None and rep["classifier_calls"]:
        L.append("  (classifier overhead unknown: the log has no usage for its calls; net savings exclude it)")
    L.append("")

    if rep["prompts"]:
        rate = rep["override_rate"]
        L.append(
            f"Overrides: /model {rep['overrides_model']}, /router off {rep['overrides_router_off']} — "
            f"override rate {'—' if rate is None else f'{100 * rate:.1f}%'} of user prompts "
            "(a rough quality proxy only)"
        )
    L.append(
        "Limits: the counterfactual assumes Opus would have produced the same output length and "
        "does not measure answer quality."
    )
    L.append(f"Prices: {prices['source']} (verified {prices['verified']})")
    return "\n".join(L)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--since", help="first day, YYYY-MM-DD (UTC, inclusive)")
    ap.add_argument("--until", help="last day, YYYY-MM-DD (UTC, inclusive)")
    ap.add_argument("--projects-dir", help="transcripts root (default: $CLAUDE_CONFIG_DIR/projects or ~/.claude/projects)")
    ap.add_argument(
        "--log",
        action="append",
        help="routing log: the plugin's store file or a /router export file (repeatable; "
        "default: ~/.claude/plugins/store/model-router_*.json and ~/.claude/model-router/routing-log.jsonl)",
    )
    ap.add_argument("--prices", default=DEFAULT_PRICES, help="price table (default: plugin/hooks/prices.json)")
    ap.add_argument("--all-sessions", action="store_true", help="include sessions with no routing-log records")
    ap.add_argument("--json", action="store_true", help="print the figures as JSON")
    a = ap.parse_args(argv)

    try:
        lo, hi = day_bounds(a.since, a.until)
    except ValueError as e:
        ap.error(f"bad date: {e}")
    config = default_config_dir()
    projects = a.projects_dir or os.path.join(config, "projects")
    prices = load_prices(a.prices)
    stats: Dict[str, int] = defaultdict(int)
    requests = read_requests(projects, stats) if os.path.isdir(projects) else []
    log = read_log(a.log if a.log else default_log_paths(config), stats)
    rep = build_report(requests, log, prices, lo, hi, a.all_sessions)
    if a.json:
        print(json.dumps({**rep, "stats": dict(stats)}, indent=2, sort_keys=True, default=str))
    else:
        label = f"{a.since or 'start'} to {a.until or 'now'}"
        print(format_report(rep, stats, prices, label))
    return 0


if __name__ == "__main__":
    sys.exit(main())

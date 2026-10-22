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
  * spend by source (main thread or subagent) and by the route of the prompt each
    request belongs to;
  * the same cost lines split by whether the router was on, so time spent with
    /router off doesn't dilute the savings;
  * the override rate (/model changes and /router off per user prompt), a rough quality
    proxy only;
  * with --simulate, the same requests re-priced under other routing policies.

Requests from before a session's first routing-log record are counted apart ("before
routing log") and left out of the other figures.

Stdlib only, Python 3.9+. Prices come from plugin/hooks/prices.json, the one price table
the plugin's /router stats reads too. Nothing here reads or prints message content.
"""

import argparse
import bisect
import datetime as dt
import glob
import json
import os
import re
import sys
from collections import defaultdict
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PRICES = os.path.join(HERE, os.pardir, "plugin", "hooks", "prices.json")
MTOK = 1_000_000.0
TTL_5M = 5 * 60
TTL_1H = 60 * 60
# The model each routing tier runs on.
TIER_MODELS = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}


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


def request_chains(requests: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Requests grouped by chain (a transcript file's main thread, or one subagent), each
    in time order."""
    chains: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for r in requests:
        chains[(r["file"], r["chain"])].append(r)
    for chain in chains.values():
        chain.sort(key=lambda r: r["ts"])
    return list(chains.values())


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
    for chain in request_chains(requests):
        prev = None
        for r in chain:
            r["switch_reads"] = 0
            t = r["tokens"]
            cw = t["cw5m"] + t["cw1h"]
            if prev is not None and canonical_model(prev["model"]) != canonical_model(r["model"]) and cw:
                ttl = TTL_1H if t["cw1h"] else TTL_5M
                gap = (r["ts"] - prev["ts"]).total_seconds()
                if gap <= ttl:
                    r["switch_reads"] = min(cw, total(prev["tokens"]))
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
# Attribution: which prompt each request belongs to

BEFORE_FIRST_PROMPT = "before first prompt"
NOT_LOGGED = "session not in routing log"
ROUTER_OFF = "unrouted: off"


def attribute(requests: List[Dict[str, Any]], log: List[Dict[str, Any]]) -> None:
    """Sets r["prompt"] (the routing-log prompt record r belongs to, or None) and
    r["before_log"] (r came before its session's first routing-log record).

    A main-thread request belongs to the latest prompt in its session with ts <= the
    request's. A subagent belongs to the prompt its first request falls under, so every
    request of a subagent counts towards the prompt that started it. Sessions with no
    routing-log records at all are neither: r["logged"] is False.
    """
    first: Dict[str, dt.datetime] = {}
    prompts: Dict[str, List[Tuple[dt.datetime, Dict[str, Any]]]] = defaultdict(list)
    for rec in log:
        s, t = rec.get("session"), parse_ts(rec.get("ts"))
        if not s or t is None:
            continue
        if s not in first or t < first[s]:
            first[s] = t
        if rec.get("kind") == "prompt":
            prompts[s].append((t, rec))
    for ps in prompts.values():
        ps.sort(key=lambda p: p[0])
    times = {s: [p[0] for p in ps] for s, ps in prompts.items()}

    def prompt_at(session: str, t: dt.datetime) -> Optional[Dict[str, Any]]:
        i = bisect.bisect_right(times.get(session, []), t)
        return prompts[session][i - 1][1] if i else None

    for chain in request_chains(requests):
        start = chain[0]["ts"]
        for r in chain:
            s = r["session"]
            r["logged"] = s in first
            r["before_log"] = s in first and r["ts"] < first[s]
            r["prompt"] = prompt_at(s, start if r["chain"] else r["ts"])


def route_bucket(r: Dict[str, Any]) -> str:
    """The route of the prompt r belongs to: a tier it was routed to, or why it wasn't."""
    p = r.get("prompt")
    if p is None:
        return BEFORE_FIRST_PROMPT if r.get("logged") else NOT_LOGGED
    tier = p.get("tier")
    if tier in TIER_MODELS:
        return f"background → {tier}" if p.get("backgroundTier") else tier
    if p.get("skip") == "non-user":
        return f"unrouted: {p.get('originKind')}"
    if p.get("skip"):
        return f"unrouted: {p.get('skip')}"
    return f"unrouted: classifier {p.get('outcome')}"


def _bucket_order(b: str) -> Tuple[int, str]:
    fixed = ["haiku", "sonnet", "opus"]
    if b in fixed:
        return (0, str(fixed.index(b)))
    if b.startswith("background"):
        return (1, b)
    if b.startswith("unrouted"):
        return (2, b)
    return (3, b)


# ---------------------------------------------------------------------------
# Simulation: the same requests re-priced under another routing policy


def _routed_tier(r: Dict[str, Any]) -> Optional[str]:
    """The tier the classifier picked for r's prompt (not a background route), if any."""
    p = r.get("prompt")
    if r["chain"] or p is None or p.get("backgroundTier"):
        return None
    tier = p.get("tier")
    return tier if tier in TIER_MODELS else None


def _non_user(r: Dict[str, Any]) -> bool:
    p = r.get("prompt")
    return p is not None and (p.get("origin") == "other" or p.get("skip") == "non-user" or bool(p.get("backgroundTier")))


def _sonnet_first(floor: str) -> Callable[[Dict[str, Any]], Optional[str]]:
    def pick(r: Dict[str, Any]) -> Optional[str]:
        tier = _routed_tier(r)
        if tier is None:
            return TIER_MODELS["sonnet"]
        order = list(TIER_MODELS)
        return TIER_MODELS[order[max(order.index(tier), order.index(floor))]]
    return pick


# Each policy maps a request (with its prompt attributed) to the model it would have run
# on, or None for the model it actually ran on.
POLICIES: Dict[str, Callable[[Dict[str, Any]], Optional[str]]] = {
    "observed": lambda r: None,
    "opus-only": lambda r: TIER_MODELS["opus"],
    "sonnet-only": lambda r: TIER_MODELS["sonnet"],
    # Routed prompts keep their tier; unrouted prompts and subagents run on Sonnet.
    "sonnet-first": _sonnet_first("haiku"),
    "sonnet-first-floor": _sonnet_first("sonnet"),
    # Subagents and prompts that aren't the person's own on Sonnet; the rest as observed.
    "background-sonnet": lambda r: TIER_MODELS["sonnet"] if r["chain"] or _non_user(r) else None,
}
# Policies that run the classifier, so its overhead counts against their savings.
CLASSIFIED = {"observed", "sonnet-first", "sonnet-first-floor", "background-sonnet"}


def simulate(
    requests: List[Dict[str, Any]],
    prices: Dict[str, Any],
    pick: Callable[[Dict[str, Any]], Optional[str]],
    counted: Callable[[Dict[str, Any]], bool],
) -> Dict[str, Any]:
    """Re-prices requests as if each had run on pick(r), with the cache re-modelled.

    Each chain tracks, per model, when it last ran and the context it left in the cache
    (input + cache reads + cache writes + output). A request's cached tokens (its observed
    reads plus writes) are read up to that held context when the model's cache is still
    warm, and written otherwise. Warm means within 5 minutes, or 1 hour when the chain
    writes 1-hour cache. The first request of a chain keeps its observed split: its reads
    came from outside the chain (the shared system prompt and tools, or a resumed
    session). That shared prefix is taken to stay warm on any model, so a request whose
    model's cache went cold still reads up to the first request's reads: in real
    transcripts a switch back to a model after an hour away still read the ~40K-token
    system prompt.

    Every request moves the cache along; only those where counted(r) add to the result.
    Returns {"usd", "requests", "switches"}: switches counts counted requests whose
    model differs from the one before it in its chain.
    """
    us_mult = prices.get("us_inference_multiplier", 1.0)
    usd = 0.0
    n = switches = 0
    for chain in request_chains(requests):
        ttl = TTL_1H if any(r["tokens"]["cw1h"] for r in chain) else TTL_5M
        held: Dict[str, Tuple[dt.datetime, float]] = {}
        shared = chain[0]["tokens"]["cache_read"]
        prev_model = None
        for i, r in enumerate(chain):
            t = r["tokens"]
            model = canonical_model(pick(r) or r["model"])
            sim = dict(t)
            if i:
                cached = t["cache_read"] + t["cw5m"] + t["cw1h"]
                h = held.get(model)
                warm = h[1] if h is not None and (r["ts"] - h[0]).total_seconds() <= ttl else 0
                reads = min(cached, max(warm, shared))
                writes = cached - reads
                cw = t["cw5m"] + t["cw1h"]
                share_1h = t["cw1h"] / cw if cw else (1.0 if ttl == TTL_1H else 0.0)
                sim.update(cache_read=reads, cw1h=writes * share_1h, cw5m=writes * (1 - share_1h))
            held[model] = (r["ts"], total(t))
            if counted(r):
                prompt_tokens = t["input"] + t["cache_read"] + t["cw5m"] + t["cw1h"]
                rates = rate_card(prices, model, prompt_tokens)
                if rates is not None:
                    usd += price_tokens(rates, sim, us_mult if r.get("geo") == "us" else 1.0)
                    n += 1
                switches += 1 if prev_model is not None and model != prev_model else 0
            prev_model = model
    return {"usd": usd, "requests": n, "switches": switches}


# ---------------------------------------------------------------------------
# Report


def empty_bundle() -> Dict[str, float]:
    return {"input": 0, "cw5m": 0, "cw1h": 0, "cache_read": 0, "output": 0}


def total(t: Dict[str, float]) -> float:
    """Every token in a bundle: input + cache writes + cache reads + output. For a request this is
    also the prefix it leaves in the cache."""
    return t["input"] + t["cw5m"] + t["cw1h"] + t["cache_read"] + t["output"]


def build_report(
    requests: List[Dict[str, Any]],
    log: List[Dict[str, Any]],
    prices: Dict[str, Any],
    lo: Optional[dt.datetime] = None,
    hi: Optional[dt.datetime] = None,
    all_sessions: bool = False,
    simulate_policies: Iterable[str] = (),
) -> Dict[str, Any]:
    # Attribution sees the whole log, so a request just after `lo` still finds its prompt.
    attribute(requests, log)
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
    # Requests before their session's first routing-log record ran before the router was
    # logging there (often before it was installed): counted apart, not in the savings.
    before = [r for r in reqs if r.get("before_log")]
    reqs = [r for r in reqs if not r.get("before_log")]

    cf_model = prices["counterfactual_model"]
    us_mult = prices.get("us_inference_multiplier", 1.0)

    def priced(r: Dict[str, Any]) -> Optional[Dict[str, float]]:
        t = r["tokens"]
        rates = rate_card(prices, r["model"], t["input"] + t["cache_read"] + t["cw5m"] + t["cw1h"])
        if rates is not None and r.get("speed") not in (None, "standard"):
            rates = None  # fast mode is not in the price table
        return rates

    by_model: Dict[str, Dict[str, Any]] = {}
    unpriced: Dict[str, Dict[str, Any]] = {}
    by_source: Dict[str, Dict[str, Dict[str, Any]]] = {"main": {}, "subagent": {}}
    # Router on vs off: a request is "off" when its prompt was sent with /router off set.
    by_router: Dict[str, Dict[str, Any]] = {
        k: {"requests": 0, "sessions": set(), "actual_usd": 0.0, "counterfactual_usd": 0.0} for k in ("on", "off")
    }
    actual = counterfactual = penalty = 0.0

    for r in reqs:
        t = r["tokens"]
        prompt_tokens = t["input"] + t["cache_read"] + t["cw5m"] + t["cw1h"]
        mult = us_mult if r.get("geo") == "us" else 1.0
        rates = priced(r)
        name = canonical_model(r["model"])
        if rates is None:
            row = unpriced.setdefault(name, {"requests": 0, "tokens": 0})
            row["requests"] += 1
            row["tokens"] += total(t)
            continue
        cost = price_tokens(rates, t, mult)
        actual += cost
        src = by_source["subagent" if r["chain"] else "main"].setdefault(route_bucket(r), {"requests": 0, "cost": 0.0})
        src["requests"] += 1
        src["cost"] += cost

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
        cf_cost = price_tokens(cf_rates, cf, mult)
        counterfactual += cf_cost

        grp = by_router["off" if route_bucket(r) == ROUTER_OFF else "on"]
        grp["requests"] += 1
        grp["sessions"].add(r["session"])
        grp["actual_usd"] += cost
        grp["counterfactual_usd"] += cf_cost

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

    before_cost = 0.0
    for r in before:
        rates = priced(r)
        if rates is not None:
            before_cost += price_tokens(rates, r["tokens"], us_mult if r.get("geo") == "us" else 1.0)

    in_scope = {id(r) for r in reqs if priced(r) is not None}
    simulations = {}
    for name in simulate_policies:
        simulations[name] = simulate(requests, prices, POLICIES[name], lambda r: id(r) in in_scope)

    # The classifier only runs while the router is on, so its overhead counts there.
    for k, grp in by_router.items():
        grp["sessions"] = len(grp["sessions"])
        overhead = clf_cost if k == "on" and with_usage else 0.0
        grp["net_savings_usd"] = grp["counterfactual_usd"] - grp["actual_usd"] - overhead

    priced_requests = sum(m["requests"] for m in by_model.values())
    priced_tokens = sum(total(m["tokens"]) for m in by_model.values())
    return {
        "scope": scope,
        "requests": len(reqs),
        "before_log": {"requests": len(before), "cost": before_cost},
        "by_source": by_source,
        "by_router": by_router,
        "simulations": simulations,
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
    bl = rep["before_log"]
    if bl["requests"]:
        L.append(
            f"  plus {bl['requests']} before routing log ({_usd(bl['cost'])}): requests before their session's "
            "first routing-log record, left out of every figure below"
        )
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

    if rep["priced_requests"]:
        L.append("Spend by source and route (each request under the prompt it belongs to):")
        L.append(f"  {'source':<10}{'route':<34}{'requests':>9}{'API-equiv.':>13}{'share':>8}")
        for source, label_ in (("main", "main"), ("subagent", "subagent")):
            rows_ = rep["by_source"][source]
            for bucket, m in sorted(rows_.items(), key=lambda kv: _bucket_order(kv[0])):
                L.append(
                    f"  {label_:<10}{bucket:<34}{m['requests']:>9}{_usd(m['cost']):>13}"
                    f"{_pct(m['cost'], rep['actual_usd']):>8}"
                )
            if rows_:
                n = sum(m["requests"] for m in rows_.values())
                c = sum(m["cost"] for m in rows_.values())
                L.append(f"  {'':<10}{'total':<34}{n:>9}{_usd(c):>13}{_pct(c, rep['actual_usd']):>8}")
        L.append(
            "  (a main-thread request belongs to the latest prompt in its session at or before it; a subagent"
        )
        L.append("  to the prompt its first request falls under)")
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

    if rep["by_router"]["off"]["requests"]:
        L.append("Router on vs off (off: requests under a prompt sent with /router off set):")
        L.append(f"  {'router':<8}{'sessions':>9}{'requests':>9}{'actual':>13}{cf_label + ' only':>16}{'net savings':>14}")
        for k in ("on", "off"):
            g = rep["by_router"][k]
            n = g["net_savings_usd"]
            L.append(
                f"  {k:<8}{g['sessions']:>9}{g['requests']:>9}{_usd(g['actual_usd']):>13}"
                f"{_usd(g['counterfactual_usd']):>16}{_usd(n):>14}  ({_pct(n, g['counterfactual_usd']).strip()})"
            )
        L.append("  (classifier overhead counts against \"on\"; a session can appear in both rows)")
        L.append("")

    sims = rep["simulations"]
    if sims:
        base = sims.get("opus-only", {}).get("usd")
        clf = rep["classifier_usd"] or 0.0
        L.append("Simulated policies (the same requests re-priced, cache re-modelled per chain and model):")
        L.append(f"  {'policy':<22}{'API-equiv.':>13}{'savings vs opus-only':>26}{'switches':>10}")
        for name, s in sims.items():
            saved = None if base is None else base - s["usd"] - (clf if name in CLASSIFIED else 0.0)
            pct = f" ({_pct(saved, base).strip()})" if saved is not None else ""
            L.append(f"  {name:<22}{_usd(s['usd']):>13}{(_usd(saved) + pct) if saved is not None else '—':>26}{s['switches']:>10}")
        L.append("  (savings include the classifier overhead for policies that run it: observed, sonnet-first*,")
        L.append("  background-sonnet; the opus-only and sonnet-only rows need no classifier)")
        if "observed" in sims and rep["actual_usd"]:
            d = sims["observed"]["usd"] / rep["actual_usd"] - 1
            L.append(f"  calibration: observed simulates to {d:+.1%} of actual")
        if base is not None and rep["counterfactual_usd"]:
            d = base / rep["counterfactual_usd"] - 1
            L.append(f"  calibration: opus-only simulates to {d:+.1%} of the counterfactual")
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
    ap.add_argument(
        "--simulate",
        action="append",
        metavar="POLICY",
        choices=sorted(POLICIES) + ["all"],
        help="also re-price the same requests under POLICY (repeatable; 'all' for every one): "
        + ", ".join(POLICIES)
        + ". observed and opus-only are always added, for calibration and as the baseline",
    )
    ap.add_argument("--json", action="store_true", help="print the figures as JSON")
    a = ap.parse_args(argv)
    policies: List[str] = []
    if a.simulate:
        want = set(POLICIES) if "all" in a.simulate else set(a.simulate) | {"observed", "opus-only"}
        policies = [p for p in POLICIES if p in want]

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
    rep = build_report(requests, log, prices, lo, hi, a.all_sessions, policies)
    if a.json:
        print(json.dumps({**rep, "stats": dict(stats)}, indent=2, sort_keys=True, default=str))
    else:
        label = f"{a.since or 'start'} to {a.until or 'now'}"
        print(format_report(rep, stats, prices, label))
    return 0


if __name__ == "__main__":
    sys.exit(main())

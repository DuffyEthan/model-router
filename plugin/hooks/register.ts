import type { EngineInterface, ModelEffort, PromptOrigin, Register, TurnUsage } from 'claude-code'
import {
  appendCapped,
  asLog,
  classifierPrice,
  classifyOrigin,
  formatStats,
  LOG_KEY,
  toJsonl,
  toUsage,
  type ClassifierUsage,
  type LogRecord,
  type Outcome,
  type PromptRecord,
  type SkipReason,
} from './tracker'
import { switchCost, tierPrices, type CacheTtl, type TierPrices } from './switch'

type Tier = 'haiku' | 'sonnet' | 'opus'
type BackgroundTier = 'session' | 'sonnet' | 'haiku'
// `raisedFrom`: the tier the classifier picked when the floor raised it.
// `background`: the route came from the /router background setting, not the classifier.
// `kept`: the route stayed on the model the main thread last ran on because switching to
// `from` would have cost more (`cost`, USD) than the turn saved (`saving`).
type Route = {
  model: Tier
  effort: ModelEffort
  reason: string
  raisedFrom?: Tier
  background?: true
  kept?: { from: Tier; cost: number; saving: number }
}
type Classified = { route?: Route; outcome: Exclude<Outcome, 'skipped'>; usage?: ClassifierUsage }

const TIERS: readonly Tier[] = ['haiku', 'sonnet', 'opus']
const BACKGROUND_TIERS: readonly BackgroundTier[] = ['session', 'sonnet', 'haiku']
const CLASSIFIER_TIMEOUT_MS = 20000
const USAGE =
  'Usage: /router [on|off|status|stats|floor [haiku|sonnet|opus]|background [session|sonnet|haiku] [low|medium|high|xhigh]|sticky [on|off]|export [path]]'
const EFFORTS: readonly ModelEffort[] = ['low', 'medium', 'high', 'xhigh']
const DEFAULT_FLOOR: Tier = 'haiku' // no floor: every tier can be routed to
const DEFAULT_BACKGROUND: BackgroundTier = 'session'
// The prompt cache's lifetimes. A main thread idle longer has no warm cache to keep.
const CACHE_TTL_MS: Record<CacheTtl, number> = { '5m': 5 * 60_000, '1h': 60 * 60_000 }
// The share of the last prompt a later request must read from cache to show it was warm.
const WARM_READ = 0.8
// The effort a background route runs at unless `/router background <tier> <effort>` set one.
const BACKGROUND_EFFORTS: Record<'sonnet' | 'haiku', ModelEffort> = {
  sonnet: 'medium',
  haiku: 'low',
}

// The persisted floor, or the default when it is unset, unknown or corrupt.
function asFloor(v: unknown): Tier {
  return TIERS.includes(v as Tier) ? (v as Tier) : DEFAULT_FLOOR
}

function asBackground(v: unknown): BackgroundTier {
  return BACKGROUND_TIERS.includes(v as BackgroundTier) ? (v as BackgroundTier) : DEFAULT_BACKGROUND
}

// The persisted background effort, or undefined (the tier's default) when unset or corrupt.
function asEffort(v: unknown): ModelEffort | undefined {
  return EFFORTS.includes(v as ModelEffort) ? (v as ModelEffort) : undefined
}

// Never route below `floor`. A raised route keeps the classifier's effort when the floor
// tier accepts it; Haiku, Sonnet and Opus 5.5 all accept every level the classifier may
// return (low to xhigh), so the medium fallback only guards a future narrower tier.
function applyFloor(r: Route, floor: Tier): Route {
  if (TIERS.indexOf(r.model) >= TIERS.indexOf(floor)) return r
  const effort = EFFORTS.includes(r.effort) ? r.effort : 'medium'
  return { ...r, model: floor, effort, raisedFrom: r.model }
}

// turn.step takes full model ids, not aliases. Update these when new models ship.
const MODEL_IDS: Record<Tier, string> = {
  haiku: 'claude-haiku-5-5',
  sonnet: 'claude-sonnet-5-5',
  opus: 'claude-opus-5-5',
}

// Session model ids this router knows how to rewrite, by provider format:
//   first-party API   claude-opus-5-5, claude-haiku-4-5-20251001 (dated snapshot)
//   Google Vertex AI  claude-opus-5-5 (current models take the bare first-party id)
//   Bedrock (Mantle)  anthropic.claude-opus-5-5
//   Bedrock (Invoke)  us.anthropic.claude-opus-5-5, global.anthropic.claude-opus-5-5, ...
// Claude 5.x ids carry no version suffix on any provider, so MODEL_IDS plus the session's
// prefix is the whole id. Anything else (a Bedrock "-v1:0" suffix, a Vertex "@date"
// snapshot, an ARN, a "[1m]" variant, an unknown prefix) is unrecognised: the target
// tier's suffix can't be derived from it, so the request is left unchanged.
const BEDROCK_PREFIX = /^(?:(?:us|us-gov|eu|apac|jp|au|global)\.)?anthropic\.$/
const SESSION_ID = /^(.*?)claude-([a-z]+)-\d+(?:-\d+)?(-\d{8})?$/

// The full model id for `tier` in the session model's provider format, or undefined
// when that format isn't recognised.
function modelId(current: string, tier: Tier): string | undefined {
  const m = current.match(SESSION_ID)
  if (!m) return undefined
  const [, prefix = '', , dated] = m
  if (prefix === '') return MODEL_IDS[tier] // first-party or Vertex
  if (dated || !BEDROCK_PREFIX.test(prefix)) return undefined
  return prefix + MODEL_IDS[tier]
}

// The tier of a model id, or undefined when it isn't a recognised Haiku/Sonnet/Opus id.
function tierOf(current: string): Tier | undefined {
  const family = current.match(SESSION_ID)?.[2]
  return TIERS.includes(family as Tier) ? (family as Tier) : undefined
}

// The main thread's last request as the API reported it: the tier that answered, its
// prompt size (input + cache reads + cache writes) and when it ended.
type LastRequest = { tier: Tier; prompt: number; at: number | undefined }

// The last request from a step's reported usage; undefined when the answering model
// isn't a known tier or the usage counted no prompt.
function lastRequest(usage: TurnUsage, sent: string, at: number | undefined): LastRequest | undefined {
  const tier = tierOf(usage.model ?? '') ?? tierOf(sent)
  const prompt =
    (usage.input_tokens ?? 0) + (usage.cache_read_input_tokens ?? 0) + (usage.cache_creation_input_tokens ?? 0)
  return tier && prompt > 0 ? { tier, prompt, at } : undefined
}

// Whether a main-thread request shows the session writes 1-hour cache: it reported 1-hour
// writes (raw API usage the plugin API doesn't declare, read in case the engine passes it
// on), or, sent more than five minutes after `prev` on the same model, it read most of
// `prev`'s prompt from cache, which a 5-minute cache would have dropped.
function writesLongCache(
  usage: TurnUsage,
  cur: LastRequest | undefined,
  prev: LastRequest | undefined,
  sentAt: number | undefined,
): boolean {
  const raw = (usage as { cache_creation?: { ephemeral_1h_input_tokens?: unknown } }).cache_creation
  const written = raw?.ephemeral_1h_input_tokens
  if (typeof written === 'number' && written > 0) return true
  if (!cur || !prev || cur.tier !== prev.tier || sentAt === undefined || prev.at === undefined) return false
  return sentAt - prev.at > CACHE_TTL_MS['5m'] && (usage.cache_read_input_tokens ?? 0) >= WARM_READ * prev.prompt
}

// A route to a tier cheaper than the one the main thread last ran on, kept on that warm
// model while its cache (of lifetime `ttl`) lives and the switch would cost more than the
// turn saves. A route to the same or a more expensive tier is never changed.
function keepWarm(r: Route, last: LastRequest, now: number | undefined, prices: TierPrices | null, ttl: CacheTtl): Route {
  if (!prices || TIERS.indexOf(r.model) >= TIERS.indexOf(last.tier)) return r
  if (now !== undefined && last.at !== undefined && now - last.at > CACHE_TTL_MS[ttl]) return r
  const { cost, saving } = switchCost(last.prompt, last.tier, r.model, prices, ttl)
  return cost > saving ? { ...r, model: last.tier, kept: { from: r.model, cost, saving } } : r
}

const SYSTEM = `You are a router. You never answer the user's message. You only decide which Claude model and effort level should answer it, choosing the CHEAPEST option likely to produce a fully correct answer.

Models (cheapest to most expensive):
- haiku: simple Q&A, definitions, quick lookups, formatting, renaming, one-line or trivial code edits, shell one-liners, short summaries.
- sonnet: everyday coding, writing a function or small feature, debugging a clear error, explaining code, moderate refactors, writing tests, most docs.
- opus: hard multi-step reasoning, architecture/design decisions, large or cross-cutting refactors, subtle bugs, security review, ambiguous open-ended tasks, long agentic work across many files.

Effort: low (fast, little reasoning), medium (typical), high (careful reasoning), xhigh (hard agentic/coding work).

Rules:
- Prefer the cheaper model when in doubt between two adjacent tiers, unless the task is high-stakes (production data, security, irreversible actions).
- Judge the task's difficulty, not its length.
- You may see the end of the conversation for context. Route the NEW message. A short follow-up that approves or continues earlier work ("yes", "do it", "go ahead", "continue", "try again") gets the same model as that work, never a cheaper one.

Reply with only a JSON object, no prose: {"model": "haiku"|"sonnet"|"opus", "effort": "low"|"medium"|"high"|"xhigh", "reason": "<one short sentence>"}`

export const register: Register = on => {
  // Module state resets on a hot reload; `enabled`, `floor`, `background`,
  // `backgroundEffort` and `sticky` are persisted in $.store.
  let enabled = true
  let floor: Tier = DEFAULT_FLOOR
  let background: BackgroundTier = DEFAULT_BACKGROUND
  let backgroundEffort: ModelEffort | undefined // undefined: BACKGROUND_EFFORTS[background]
  let sticky = true
  let route: Route | undefined
  // The main thread's last request as the API reported it: the tier that answered, its
  // prompt size (input + cache reads + cache writes) and when it ended.
  let lastMain: LastRequest | undefined
  // The session's cache lifetime: 5 minutes until a request shows 1-hour writes.
  let cacheTtl: CacheTtl = '5m'
  let prices: TierPrices | null | undefined // null: prices.json could not be read
  const counts: Record<Tier, number> = { haiku: 0, sonnet: 0, opus: 0 }
  const bgEffort = (tier: 'sonnet' | 'haiku') => backgroundEffort ?? BACKGROUND_EFFORTS[tier]
  const bgLabel = () => (background === 'session' ? 'session' : `${background} · ${bgEffort(background)}`)

  on('session.start', async ($, e, next) => {
    enabled = (await $.store.get('enabled')) !== false
    floor = asFloor(await $.store.get('floor'))
    background = asBackground(await $.store.get('background'))
    backgroundEffort = asEffort(await $.store.get('backgroundEffort'))
    sticky = (await $.store.get('sticky')) !== false
    await $.command.register({
      name: 'router',
      description:
        'Model router: /router [on|off|status|stats|floor [haiku|sonnet|opus]|background [session|sonnet|haiku] [low|medium|high|xhigh]|sticky [on|off]|export [path]]',
    })
    return next(e)
  })

  // Classify each prompt the person typed before it enters the session. Every
  // submission clears the previous route first, so a turn that isn't classified (a slash
  // command, a background task's notification, a peer's message) runs on the session model.
  on('prompt.submit', async ($, e, next) => {
    route = undefined
    const ts = await nowIso($) // when the prompt arrived, for joining with transcripts
    const isUser = isUserPrompt(e.origin)
    const text = e.text
    let skip: SkipReason | undefined
    let outcome: Outcome = 'skipped'
    let usage: ClassifierUsage | undefined
    let latencyMs: number | null = null
    if (!enabled) skip = 'off'
    else if (!isUser) {
      if (background !== 'session') {
        const effort = bgEffort(background)
        route = { model: background, effort, reason: 'background', background: true }
        counts[background] += 1
        $.ui.status(`router → ${background} · ${effort}`)
      } else {
        skip = 'non-user'
      }
    }
    else if (!text.trim()) skip = 'empty'
    else if (text.trimStart().startsWith('/')) skip = 'slash'
    else {
      const t0 = await nowMs($)
      let c: Classified
      try {
        c = await classify($, text)
      } catch {
        c = { outcome: 'failed' } // a classifier error must never block the prompt
      }
      latencyMs = await elapsedSince($, t0)
      route = c.route && applyFloor(c.route, floor)
      if (route && sticky && lastMain) {
        try {
          if (prices === undefined) prices = (await readTierPrices($)) ?? null
          route = keepWarm(route, lastMain, await nowMs($), prices, cacheTtl)
        } catch {
          // a failed estimate leaves the classifier's route as it was
        }
      }
      outcome = c.outcome
      usage = c.usage
      if (route) {
        counts[route.model] += 1
        $.ui.status(`router → ${route.model} · ${route.effort}`)
      } else {
        $.ui.status('router → session model (classifier failed)')
      }
    }
    // Build the record from metadata only (never the prompt text), then let the
    // prompt in first so logging adds no latency to it.
    const record: PromptRecord = {
      v: 1,
      kind: 'prompt',
      ts,
      session: null,
      tier: route ? route.model : 'session',
      effort: route ? route.effort : 'session',
      latencyMs,
      outcome,
      ...(skip ? { skip } : {}),
      origin: classifyOrigin(text, isUser),
      originKind: e.origin?.kind ?? 'none',
      midTurn: e.turnId !== undefined,
      ...(route?.raisedFrom ? { floored: true } : {}),
      ...(route?.kept ? { sticky: true } : {}),
      ...(route?.background ? { backgroundTier: route.model } : {}),
      ...(usage ? { usage } : {}),
    }
    const result = await next(e)
    try {
      record.session = await sessionId($)
      await log($, record)
    } catch {
      // logging never alters the prompt's outcome
    }
    return result
  })

  // A manual /model (or picker / SDK set_model) change is a misroute signal. Automatic
  // switches (fallback, resume) are not the person overriding the router.
  on('classic.PostModelSwitch', async ($, e, next) => {
    const result = await next(e)
    try {
      if (e.source === 'command' || e.source === 'picker' || e.source === 'sdk') {
        await log($, {
          v: 1,
          kind: 'override',
          ts: await nowIso($),
          session: await sessionId($),
          action: 'model',
          from: e.from_model,
          to: e.to_model,
          source: e.source,
        })
      }
    } catch {
      // never let logging fail the hook
    }
    return result
  })

  // A slash command (e.g. /security-review) may start a turn of its own: run it on the
  // session model rather than whatever the last prompt was routed to.
  on('command.run', async ($, e, next) => {
    if (e.command !== 'router') route = undefined
    return next(e)
  })

  // Rewrite every main-thread model request of the turn to the routed model, and note
  // what each one cost for the switch-cost check.
  // Downgrade subagent requests above the background tier to it when configured; a
  // subagent already at or below it (e.g. a Haiku Explore agent) keeps its own model.
  on('turn.step', async function* ($, e, next) {
    if (e.agentId !== undefined) {
      if (!enabled || background === 'session') return yield* next(e)
      const tier = tierOf(e.model)
      const model = modelId(e.model, background)
      if (!tier || !model || TIERS.indexOf(tier) <= TIERS.indexOf(background)) return yield* next(e)
      return yield* next({ ...e, model, effort: bgEffort(background) })
    }

    let req = e
    if (enabled && route) {
      const model = modelId(e.model, route.model)
      if (model) req = { ...e, model, effort: route.effort } // unrecognised id format: leave the request alone
    }
    const sentAt = await nowMs($)
    const result = yield* next(req)
    try {
      if (result?.usage) {
        const cur = lastRequest(result.usage, req.model, await nowMs($))
        if (writesLongCache(result.usage, cur, lastMain, sentAt)) cacheTtl = '1h'
        lastMain = cur
      }
    } catch {
      // never let bookkeeping fail the step
    }
    return result
  })

  // A compacted conversation is a new prefix no model has cached: nothing is warm.
  on('session.compact', async ($, e, next) => {
    const result = await next(e)
    if (e.agentId === undefined && e.trigger !== 'precompute' && result.messages) lastMain = undefined
    return result
  })

  on('command.run', { command: 'router' }, async ($, e) => {
    const arg = e.args.trim().toLowerCase()
    if (arg === 'stats') {
      return {
        text:
          `${formatStats(await readLog($), await readClassifierPrice($))}\n` +
          `Floor: ${floor}\nBackground: ${bgLabel()}\nSticky: ${sticky ? 'on' : 'off'}`,
      }
    }
    if (arg === 'sticky' || arg.startsWith('sticky ')) {
      const want = arg.slice('sticky'.length).trim()
      if (want) {
        if (want !== 'on' && want !== 'off') return { text: USAGE }
        sticky = want === 'on'
        await $.store.set('sticky', sticky)
      }
      return {
        text: sticky
          ? 'Sticky: on (a cheaper pick stays on the warm model when switching would cost more than it saves)'
          : 'Sticky: off (every pick switches models, whatever the cache)',
      }
    }
    if (arg === 'floor' || arg.startsWith('floor ')) {
      const want = arg.slice('floor'.length).trim()
      if (want) {
        if (!TIERS.includes(want as Tier)) return { text: USAGE }
        floor = want as Tier
        await $.store.set('floor', floor)
      }
      return { text: `Floor: ${floor} (the router never picks a model below ${floor})` }
    }
    if (arg === 'background' || arg.startsWith('background ')) {
      const want = arg.slice('background'.length).trim().split(/\s+/).filter(Boolean)
      if (want.length) {
        // A tier, then optionally an effort; no effort resets it to the tier's default.
        const [tier, effort] = want
        if (want.length > 2 || !BACKGROUND_TIERS.includes(tier as BackgroundTier)) return { text: USAGE }
        if (effort !== undefined && (tier === 'session' || !EFFORTS.includes(effort as ModelEffort))) {
          return { text: USAGE }
        }
        background = tier as BackgroundTier
        backgroundEffort = effort as ModelEffort | undefined
        await $.store.set('background', background)
        await $.store.set('backgroundEffort', backgroundEffort ?? null)
      }
      return { text: `Background: ${bgLabel()} (background notifications and subagents route to ${background})` }
    }
    if (arg === 'export' || arg.startsWith('export ')) {
      try {
        return { text: await exportLog($, e.args.trim().slice('export'.length).trim()) }
      } catch (err) {
        return { text: `Export failed: ${err instanceof Error ? err.message : String(err)}` }
      }
    }
    if (arg === 'on' || arg === 'off') {
      enabled = arg === 'on'
      await $.store.set('enabled', enabled)
      if (!enabled) {
        route = undefined
        $.ui.status(undefined)
      }
      try {
        await log($, {
          v: 1,
          kind: 'override',
          ts: await nowIso($),
          session: await sessionId($),
          action: enabled ? 'router-on' : 'router-off',
        })
      } catch {
        // logging never blocks the command
      }
    } else if (arg && arg !== 'status') {
      return { text: USAGE }
    }
    const raised = route?.raisedFrom ? ` (raised from ${route.raisedFrom} by the floor)` : ''
    const kept = route?.kept
      ? ` (kept on ${route.model}: switch to ${route.kept.from} would cost more, ` +
        `~$${route.kept.cost.toFixed(2)} vs ~$${route.kept.saving.toFixed(2)} saved)`
      : ''
    const last = route ? `${route.model} · ${route.effort} — ${route.reason}${raised}${kept}` : 'none yet'
    return {
      text: [
        `Model router: ${enabled ? 'on' : 'off'}`,
        `Floor: ${floor}`,
        `Background: ${bgLabel()}`,
        `Sticky: ${sticky ? 'on' : 'off'}`,
        `Last route: ${last}`,
        `This session: haiku ${counts.haiku}, sonnet ${counts.sonnet}, opus ${counts.opus}`,
      ].join('\n'),
    }
  })
}

// Prompts that are the person's own words. Absent origin means the user's own.
function isUserPrompt(origin: PromptOrigin | undefined): boolean {
  if (!origin) return true
  switch (origin.kind) {
    case 'composer':
    case 'bridge':
    case 'sdk':
      return true
    case 'plugin':
      return origin.asUser === true
    default:
      return false
  }
}

async function nowMs($: EngineInterface): Promise<number | undefined> {
  try {
    return await $.clock.now()
  } catch {
    return undefined
  }
}

async function elapsedSince($: EngineInterface, t0: number | undefined): Promise<number | null> {
  if (t0 === undefined) return null
  try {
    return Math.max(0, Math.round((await $.clock.now()) - t0))
  } catch {
    return null
  }
}

async function classify($: EngineInterface, text: string): Promise<Classified> {
  let context = ''
  try {
    const messages = await $.session.messages()
    if (Array.isArray(messages)) {
      context = messages
        .slice(-4)
        .filter(m => m.text)
        .map(m => `${m.role}: ${m.text.slice(0, 600)}`)
        .join('\n')
    }
  } catch {
    // Context is a nice-to-have; route on the prompt alone.
  }

  const prompt =
    (context ? `<conversation_tail>\n${context}\n</conversation_tail>\n\n` : '') +
    `<new_message>\n${text.slice(0, 8000)}\n</new_message>`

  const r = await $.model.complete({
    model: 'haiku',
    effort: 'low',
    system: SYSTEM,
    prompt,
    maxTokens: 300,
    timeoutMs: CLASSIFIER_TIMEOUT_MS,
  })
  const usage = toUsage(r?.usage)
  if (!r.isAnswered) {
    // 'aborted' is the timeoutMs cut (or the turn's own abort); everything else failed.
    return { outcome: r.reason === 'aborted' ? 'timeout' : 'failed', usage }
  }

  const json = r.text.match(/\{[\s\S]*\}/)?.[0]
  if (!json) return { outcome: 'failed', usage }
  try {
    const d = JSON.parse(json) as Partial<Route>
    if (!d.model || !TIERS.includes(d.model)) return { outcome: 'failed', usage }
    const effort = d.effort && EFFORTS.includes(d.effort) ? d.effort : 'medium'
    return { route: { model: d.model, effort, reason: String(d.reason ?? '') }, outcome: 'ok', usage }
  } catch {
    return { outcome: 'failed', usage }
  }
}

// ---- Routing log I/O. Every helper swallows its own errors: logging never blocks or
// alters routing. Records are built from metadata only, never from prompt text.

async function nowIso($: EngineInterface): Promise<string> {
  try {
    return new Date(await $.clock.now()).toISOString()
  } catch {
    return new Date().toISOString()
  }
}

async function sessionId($: EngineInterface): Promise<string | null> {
  try {
    const id = await $.session.id()
    return typeof id === 'string' ? id : null
  } catch {
    return null
  }
}

async function readLog($: EngineInterface): Promise<LogRecord[]> {
  try {
    return asLog(await $.store.get(LOG_KEY))
  } catch {
    return []
  }
}

// Log appends run one after another so records written close together are all kept.
let logChain: Promise<void> = Promise.resolve()

function log($: EngineInterface, record: LogRecord): Promise<void> {
  logChain = logChain.then(() => appendLog($, record)).catch(() => undefined)
  return logChain
}

async function appendLog($: EngineInterface, record: LogRecord): Promise<void> {
  try {
    await $.store.set(LOG_KEY, appendCapped(await readLog($), record))
  } catch {
    // a full or unwritable store loses this record, nothing else
  }
}

async function readClassifierPrice($: EngineInterface) {
  try {
    return classifierPrice(await $.fs.read(`${$.plugin.root}/hooks/prices.json`))
  } catch {
    return undefined
  }
}

async function readTierPrices($: EngineInterface): Promise<TierPrices | undefined> {
  try {
    return tierPrices(await $.fs.read(`${$.plugin.root}/hooks/prices.json`), MODEL_IDS)
  } catch {
    return undefined
  }
}

// Writes the log as JSON Lines for scripts/router_report.py --log.
async function exportLog($: EngineInterface, arg: string): Promise<string> {
  const records = await readLog($)
  let path = arg
  if (!path) {
    const dir = (await $.env.get('CLAUDE_CONFIG_DIR')) ?? `${(await $.env.get('HOME')) ?? '.'}/.claude`
    path = `${dir}/model-router/routing-log.jsonl`
  }
  await $.fs.write(path, toJsonl(records))
  return `Wrote ${records.length} routing-log records to ${path}\n(python3 scripts/router_report.py --log ${path})`
}

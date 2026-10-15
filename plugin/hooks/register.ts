import type { EngineInterface, ModelEffort, PromptOrigin, Register } from 'claude-code'
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

type Tier = 'haiku' | 'sonnet' | 'opus'
// `raisedFrom`: the tier the classifier picked when the floor raised it.
// `guarded`: true when context length exceeded the guard threshold, raising haiku to sonnet.
type Route = { model: Tier; effort: ModelEffort; reason: string; raisedFrom?: Tier; guarded?: true }
type Classified = {
  route?: Route
  outcome: Exclude<Outcome, 'skipped'>
  usage?: ClassifierUsage
  contextTokens?: number
}

const TIERS: readonly Tier[] = ['haiku', 'sonnet', 'opus']
const CLASSIFIER_TIMEOUT_MS = 20000
const USAGE = 'Usage: /router [on|off|status|stats|floor [haiku|sonnet|opus]|guard [on|off|<threshold_tokens>]|export [path]]'
const EFFORTS: readonly ModelEffort[] = ['low', 'medium', 'high', 'xhigh']
const DEFAULT_FLOOR: Tier = 'haiku' // no floor: every tier can be routed to
const DEFAULT_GUARD_THRESHOLD = 100_000

type GuardConfig = { enabled: boolean; threshold: number }
const DEFAULT_GUARD: GuardConfig = { enabled: true, threshold: DEFAULT_GUARD_THRESHOLD }

// The persisted floor, or the default when it is unset, unknown or corrupt.
function asFloor(v: unknown): Tier {
  return TIERS.includes(v as Tier) ? (v as Tier) : DEFAULT_FLOOR
}

// The persisted guard, or the default when it is unset, unknown or corrupt.
function asGuard(v: unknown): GuardConfig {
  if (v === false || v === 'off') {
    return { enabled: false, threshold: DEFAULT_GUARD_THRESHOLD }
  }
  if (v === true || v === 'on') {
    return { enabled: true, threshold: DEFAULT_GUARD_THRESHOLD }
  }
  if (typeof v === 'number' && Number.isFinite(v) && v > 0) {
    return { enabled: true, threshold: Math.round(v) }
  }
  if (typeof v === 'string') {
    const parsed = Number(v.replace(/[,_]/g, ''))
    if (Number.isFinite(parsed) && parsed > 0) {
      return { enabled: true, threshold: Math.round(parsed) }
    }
  }
  if (typeof v === 'object' && v !== null) {
    const o = v as Record<string, unknown>
    const enabled = o.enabled !== false
    const threshold =
      typeof o.threshold === 'number' && Number.isFinite(o.threshold) && o.threshold > 0
        ? Math.round(o.threshold)
        : DEFAULT_GUARD_THRESHOLD
    return { enabled, threshold }
  }
  return { enabled: true, threshold: DEFAULT_GUARD_THRESHOLD }
}

// Never route below `floor`. A raised route keeps the classifier's effort when the floor
// tier accepts it; Haiku, Sonnet and Opus 5.5 all accept every level the classifier may
// return (low to xhigh), so the medium fallback only guards a future narrower tier.
function applyFloor(r: Route, floor: Tier): Route {
  if (TIERS.indexOf(r.model) >= TIERS.indexOf(floor)) return r
  const effort = EFFORTS.includes(r.effort) ? r.effort : 'medium'
  return { ...r, model: floor, effort, raisedFrom: r.model }
}

// If estimated context tokens exceed guard.threshold, guard against Haiku by elevating
// haiku to sonnet, preserving the classifier's effort level.
function applyGuard(r: Route, guard: GuardConfig, contextTokens: number): Route {
  if (!guard.enabled || contextTokens <= guard.threshold) return r
  if (r.model !== 'haiku') return r
  const effort = EFFORTS.includes(r.effort) ? r.effort : 'medium'
  return { ...r, model: 'sonnet', effort, guarded: true }
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
const SESSION_ID = /^(.*?)claude-[a-z]+-\d+(?:-\d+)?(-\d{8})?$/

// The full model id for `tier` in the session model's provider format, or undefined
// when that format isn't recognised.
function modelId(current: string, tier: Tier): string | undefined {
  const m = current.match(SESSION_ID)
  if (!m) return undefined
  const [, prefix = '', dated] = m
  if (prefix === '') return MODEL_IDS[tier] // first-party or Vertex
  if (dated || !BEDROCK_PREFIX.test(prefix)) return undefined
  return prefix + MODEL_IDS[tier]
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
  // Module state resets on a hot reload; `enabled`, `floor`, and `guard` are persisted in $.store.
  let enabled = true
  let floor: Tier = DEFAULT_FLOOR
  let guard: GuardConfig = DEFAULT_GUARD
  let route: Route | undefined
  const counts: Record<Tier, number> = { haiku: 0, sonnet: 0, opus: 0 }

  on('session.start', async ($, e, next) => {
    enabled = (await $.store.get('enabled')) !== false
    floor = asFloor(await $.store.get('floor'))
    guard = asGuard(await $.store.get('guard'))
    await $.command.register({
      name: 'router',
      description: 'Model router: /router [on|off|status|stats|floor [haiku|sonnet|opus]|guard [on|off|<threshold_tokens>]|export [path]]',
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
    else if (!isUser) skip = 'non-user'
    else if (!text.trim()) skip = 'empty'
    else if (text.trimStart().startsWith('/')) skip = 'slash'
    else {
      const t0 = await elapsedStart($)
      let c: Classified
      try {
        c = await classify($, text)
      } catch {
        c = { outcome: 'failed' } // a classifier error must never block the prompt
      }
      latencyMs = await elapsedSince($, t0)
      const contextTokens = c.contextTokens ?? (await estimateContextTokens($))
      let r = c.route && applyFloor(c.route, floor)
      r = r && applyGuard(r, guard, contextTokens)
      route = r
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
      ...(route?.guarded ? { guarded: true } : {}),
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

  // Rewrite every main-thread model request of the turn to the routed model.
  // Subagents keep their own configured models.
  on('turn.step', async function* ($, e, next) {
    if (!enabled || !route || e.agentId !== undefined) {
      return yield* next(e)
    }
    const model = modelId(e.model, route.model)
    if (!model) return yield* next(e) // unrecognised id format: leave the request alone
    return yield* next({ ...e, model, effort: route.effort })
  })

  on('command.run', { command: 'router' }, async ($, e) => {
    const arg = e.args.trim().toLowerCase()
    if (arg === 'stats') {
      return {
        text: `${formatStats(await readLog($), await readClassifierPrice($))}\nFloor: ${floor}\nGuard: ${guard.enabled ? `on (${guard.threshold} tokens)` : 'off'}`,
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
    if (arg === 'guard' || arg.startsWith('guard ')) {
      const want = arg.slice('guard'.length).trim()
      if (want) {
        if (want === 'off') {
          guard = { ...guard, enabled: false }
          await $.store.set('guard', guard)
        } else if (want === 'on') {
          guard = { ...guard, enabled: true }
          await $.store.set('guard', guard)
        } else {
          const num = Number(want.replace(/[,_]/g, ''))
          if (!Number.isFinite(num) || num <= 0 || !Number.isInteger(num)) {
            return { text: USAGE }
          }
          guard = { enabled: true, threshold: num }
          await $.store.set('guard', guard)
        }
      }
      return { text: `Guard: ${guard.enabled ? `on (${guard.threshold} tokens)` : 'off'}` }
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
    const raised = route?.guarded
      ? ' (raised from haiku by the guard)'
      : route?.raisedFrom
        ? ` (raised from ${route.raisedFrom} by the floor)`
        : ''
    const last = route ? `${route.model} · ${route.effort} — ${route.reason}${raised}` : 'none yet'
    return {
      text: [
        `Model router: ${enabled ? 'on' : 'off'}`,
        `Floor: ${floor}`,
        `Guard: ${guard.enabled ? `on (${guard.threshold} tokens)` : 'off'}`,
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

async function elapsedStart($: EngineInterface): Promise<number | undefined> {
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

function messageTextLength(m: unknown): number {
  if (!m || typeof m !== 'object') return 0
  const msg = m as Record<string, unknown>
  if (typeof msg.text === 'string') return msg.text.length
  if (typeof msg.content === 'string') return msg.content.length
  if (Array.isArray(msg.content)) {
    let len = 0
    for (const part of msg.content) {
      if (part && typeof part === 'object') {
        const p = part as Record<string, unknown>
        if (typeof p.text === 'string') len += p.text.length
      }
    }
    return len
  }
  return 0
}

function estimateContextTokensFromMessages(messages: unknown): number {
  if (!Array.isArray(messages)) return 0
  let chars = 0
  for (const m of messages) {
    chars += messageTextLength(m)
  }
  return Math.floor(chars / 4)
}

async function estimateContextTokens($: EngineInterface): Promise<number> {
  try {
    const messages = await $.session.messages()
    return estimateContextTokensFromMessages(messages)
  } catch {
    return 0
  }
}

async function classify($: EngineInterface, text: string): Promise<Classified> {
  let context = ''
  let contextTokens = 0
  try {
    const messages = await $.session.messages()
    if (Array.isArray(messages)) {
      contextTokens = estimateContextTokensFromMessages(messages)
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
    return { outcome: r.reason === 'aborted' ? 'timeout' : 'failed', usage, contextTokens }
  }

  const json = r.text.match(/\{[\s\S]*\}/)?.[0]
  if (!json) return { outcome: 'failed', usage, contextTokens }
  try {
    const d = JSON.parse(json) as Partial<Route>
    if (!d.model || !TIERS.includes(d.model)) return { outcome: 'failed', usage, contextTokens }
    const effort = d.effort && EFFORTS.includes(d.effort) ? d.effort : 'medium'
    return {
      route: { model: d.model, effort, reason: String(d.reason ?? '') },
      outcome: 'ok',
      usage,
      contextTokens,
    }
  } catch {
    return { outcome: 'failed', usage, contextTokens }
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

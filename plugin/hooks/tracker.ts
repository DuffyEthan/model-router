// Routing log: one small record per prompt and per manual override, kept in the
// plugin's own $.store so scripts/router_report.py can join it with Claude Code's
// session transcripts. Records never hold prompt text, or any part of it.
//
// $.store is a JSON file of the plugin's own under the Claude Code config directory
// (currently ~/.claude/plugins/store/<plugin>_<marketplace>-<hash>.json) and refuses a
// store over 4 MiB of JSON text in all, so the log is capped by count and by bytes.
import type { ModelEffort } from 'claude-code'

export const LOG_KEY = 'log'
export const LOG_VERSION = 1
// ~200-300 bytes a record: 4000 records is ~1 MB, well under the 4 MiB store limit.
export const MAX_RECORDS = 4000
export const MAX_LOG_BYTES = 1_500_000

export type Tier = 'haiku' | 'sonnet' | 'opus'
export type Outcome = 'ok' | 'failed' | 'timeout' | 'skipped'
export type OriginClass = 'user' | 'slash' | 'other'
export type SkipReason = 'off' | 'non-user' | 'slash' | 'empty'

export type ClassifierUsage = { in: number; out: number; cr: number; cw: number }

export type PromptRecord = {
  v: number
  kind: 'prompt'
  ts: string // ISO 8601, UTC
  session: string | null
  tier: Tier | 'session' // 'session': not routed, the session model answered
  effort: ModelEffort | 'session'
  latencyMs: number | null // classifier wall time; null when it did not run
  outcome: Outcome
  skip?: SkipReason
  origin: OriginClass
  originKind: string // PromptOrigin.kind as the engine stamped it ('none' when absent)
  midTurn: boolean // prompt.submit carried a turnId: typed over / delivered into a running turn
  floored?: true // the classifier picked a tier below /router floor; `tier` is the floor
  backgroundTier?: Tier // the prompt ran on the background tier setting
  usage?: ClassifierUsage // the classifier call's own tokens, when it reported them
}

export type OverrideRecord = {
  v: number
  kind: 'override'
  ts: string
  session: string | null
  action: 'router-off' | 'router-on' | 'model'
  from?: string // /model: the resolved model ids
  to?: string
  source?: string // /model: PostModelSwitch source (command | picker | sdk)
}

export type LogRecord = PromptRecord | OverrideRecord

export function classifyOrigin(text: string, isUser: boolean): OriginClass {
  if (!isUser) return 'other'
  return text.trimStart().startsWith('/') ? 'slash' : 'user'
}

export function toUsage(u: unknown): ClassifierUsage | undefined {
  if (!u || typeof u !== 'object') return undefined
  const r = u as Record<string, unknown>
  const n = (k: string) => (typeof r[k] === 'number' ? (r[k] as number) : 0)
  const usage = {
    in: n('input_tokens'),
    out: n('output_tokens'),
    cr: n('cache_read_input_tokens'),
    cw: n('cache_creation_input_tokens'),
  }
  return usage.in + usage.out + usage.cr + usage.cw > 0 ? usage : undefined
}

// Appends `record` and keeps the newest records within both budgets.
export function appendCapped(log: readonly LogRecord[], record: LogRecord): LogRecord[] {
  return capLog([...log, record])
}

// Keep the newest records within both budgets.
export function capLog(log: LogRecord[]): LogRecord[] {
  let out = log.length > MAX_RECORDS ? log.slice(log.length - MAX_RECORDS) : log
  let bytes = JSON.stringify(out).length
  while (bytes > MAX_LOG_BYTES && out.length > 0) {
    const drop = Math.max(1, Math.ceil(out.length / 10))
    out = out.slice(drop)
    bytes = JSON.stringify(out).length
  }
  return out
}

// ---- /router stats: a summary of the routing log alone (no token or cost data) ----

type HaikuPrice = { input: number; cache_write_5m: number; cache_read: number; output: number }

function pct(n: number, d: number): string {
  return d ? `${((100 * n) / d).toFixed(1)}%` : '—'
}

function percentile(xs: number[], p: number): number | undefined {
  if (!xs.length) return undefined
  const s = [...xs].sort((a, b) => a - b)
  return s[Math.min(s.length - 1, Math.max(0, Math.ceil((p / 100) * s.length) - 1))]
}

export function formatStats(log: readonly LogRecord[], price: HaikuPrice | undefined): string {
  const prompts = log.filter((r): r is PromptRecord => r?.kind === 'prompt')
  const overrides = log.filter((r): r is OverrideRecord => r?.kind === 'override')
  const user = prompts.filter(r => r.origin === 'user')
  const routed = prompts.filter(r => r.tier !== 'session')
  const lines: string[] = []
  lines.push('Model router stats — from the routing log only (no token or cost data;')
  lines.push('run scripts/router_report.py to join with session transcripts).')
  if (!prompts.length && !overrides.length) {
    lines.push('No records yet.')
    return lines.join('\n')
  }
  const first = log[0]?.ts?.slice(0, 10) ?? '?'
  const last = log[log.length - 1]?.ts?.slice(0, 10) ?? '?'
  lines.push(`Records: ${log.length} (${first} → ${last})`)

  const byOrigin = { user: 0, slash: 0, other: 0 }
  for (const r of prompts) byOrigin[r.origin] = (byOrigin[r.origin] ?? 0) + 1
  const otherKinds: Record<string, number> = {}
  for (const r of prompts) if (r.origin === 'other') otherKinds[r.originKind] = (otherKinds[r.originKind] ?? 0) + 1
  const kinds = Object.entries(otherKinds).map(([k, n]) => `${k} ${n}`).join(', ')
  lines.push(
    `Prompts: ${prompts.length} — user ${byOrigin.user}, slash ${byOrigin.slash}, other ${byOrigin.other}` +
      (kinds ? ` (${kinds})` : ''),
  )
  lines.push(`Mid-turn prompts: ${prompts.filter(r => r.midTurn).length}`)

  const tiers = { haiku: 0, sonnet: 0, opus: 0 }
  for (const r of routed) tiers[r.tier as Tier] += 1
  lines.push(
    `Routed (share of prompts): haiku ${tiers.haiku} (${pct(tiers.haiku, prompts.length)}), ` +
      `sonnet ${tiers.sonnet} (${pct(tiers.sonnet, prompts.length)}), ` +
      `opus ${tiers.opus} (${pct(tiers.opus, prompts.length)}), ` +
      `session model ${prompts.length - routed.length} (${pct(prompts.length - routed.length, prompts.length)})`,
  )
  const bg = prompts.filter(r => r.backgroundTier)
  const bgTiers = { haiku: 0, sonnet: 0, opus: 0 }
  for (const r of bg) bgTiers[r.backgroundTier as Tier] += 1
  const bgDetail = Object.entries(bgTiers).filter(([, n]) => n > 0).map(([t, n]) => `${t} ${n}`).join(', ')
  lines.push(`Background routed: ${bg.length}` + (bgDetail ? ` (${bgDetail})` : ''))

  const outcomes = { ok: 0, failed: 0, timeout: 0, skipped: 0 }
  for (const r of prompts) outcomes[r.outcome] = (outcomes[r.outcome] ?? 0) + 1
  lines.push(
    `Classifier: ok ${outcomes.ok}, failed ${outcomes.failed}, timeout ${outcomes.timeout}, skipped ${outcomes.skipped}`,
  )
  const lat = prompts.map(r => r.latencyMs).filter((x): x is number => typeof x === 'number')
  if (lat.length) lines.push(`Classifier latency: p50 ${percentile(lat, 50)} ms, p90 ${percentile(lat, 90)} ms`)

  const calls = prompts.filter(r => r.outcome !== 'skipped')
  const tok = { in: 0, out: 0, cr: 0, cw: 0 }
  for (const r of calls) {
    if (!r.usage) continue
    tok.in += r.usage.in
    tok.out += r.usage.out
    tok.cr += r.usage.cr
    tok.cw += r.usage.cw
  }
  let overhead = `Classifier overhead: ${calls.length} calls, ${tok.in + tok.cr + tok.cw} input / ${tok.out} output tokens`
  if (price) {
    const usd =
      (tok.in * price.input + tok.cw * price.cache_write_5m + tok.cr * price.cache_read + tok.out * price.output) / 1e6
    overhead += ` ≈ $${usd.toFixed(4)} API-equivalent`
  }
  lines.push(overhead)

  const offs = overrides.filter(r => r.action === 'router-off').length
  const models = overrides.filter(r => r.action === 'model').length
  lines.push(
    `Overrides: /model ${models}, /router off ${offs} — override rate ${pct(models + offs, user.length)} ` +
      'of user prompts (a rough quality proxy, not a measure of answer quality)',
  )
  return lines.join('\n')
}

export function asLog(v: unknown): LogRecord[] {
  return Array.isArray(v) ? (v as LogRecord[]) : []
}

// The classifier's price row from prices.json's text, or undefined.
export function classifierPrice(raw: unknown): HaikuPrice | undefined {
  try {
    const d = JSON.parse(typeof raw === 'string' ? raw : '') as {
      classifier_model?: string
      models?: Record<string, HaikuPrice>
    }
    const p = d.models?.[d.classifier_model ?? '']
    return p && typeof p.input === 'number' ? p : undefined
  } catch {
    return undefined
  }
}

export function toJsonl(log: readonly LogRecord[]): string {
  return log.map(r => JSON.stringify(r)).join('\n') + (log.length ? '\n' : '')
}

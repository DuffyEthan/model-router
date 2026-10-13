import type { EngineInterface, ModelEffort, Register } from 'claude-code'

type Tier = 'haiku' | 'sonnet' | 'opus'
type Route = { model: Tier; effort: ModelEffort; reason: string }

const TIERS: readonly Tier[] = ['haiku', 'sonnet', 'opus']
const EFFORTS: readonly ModelEffort[] = ['low', 'medium', 'high', 'xhigh']

// turn.step takes full model ids, not aliases. Update these when new models ship.
const MODEL_IDS: Record<Tier, string> = {
  haiku: 'claude-haiku-5-5',
  sonnet: 'claude-sonnet-5-5',
  opus: 'claude-opus-5-5',
}

// Keep any provider prefix the session's model id carries (e.g. Bedrock's "anthropic.").
function modelId(current: string, tier: Tier): string {
  const prefix = current.match(/^(.*?)claude-/)?.[1] ?? ''
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
- You may see the end of the conversation for context. Route the NEW message. A short follow-up ("yes", "do it", "continue", "try again") inherits the difficulty of the task it refers to.

Reply with only a JSON object, no prose: {"model": "haiku"|"sonnet"|"opus", "effort": "low"|"medium"|"high"|"xhigh", "reason": "<one short sentence>"}`

export const register: Register = on => {
  // Module state resets on a hot reload; `enabled` is persisted in $.store.
  let enabled = true
  let route: Route | undefined
  const counts: Record<Tier, number> = { haiku: 0, sonnet: 0, opus: 0 }

  on('session.start', async ($, e, next) => {
    enabled = (await $.store.get('enabled')) !== false
    await $.command.register({
      name: 'router',
      description: 'Model router: /router [on|off|status]',
    })
    return next(e)
  })

  // Classify each prompt before it enters the session.
  on('prompt.submit', async ($, e, next) => {
    if (enabled && e.text.trim() && !e.text.trimStart().startsWith('/')) {
      route = undefined // never reuse a stale route if classification throws
      route = await classify($, e.text)
      if (route) {
        counts[route.model] += 1
        $.ui.status(`router → ${route.model} · ${route.effort}`)
      } else {
        $.ui.status('router → session model (classifier failed)')
      }
    }
    return next(e)
  })

  // Rewrite every main-thread model request of the turn to the routed model.
  // Subagents keep their own configured models.
  on('turn.step', async function* ($, e, next) {
    if (!enabled || !route || e.agentId !== undefined) {
      return yield* next(e)
    }
    return yield* next({ ...e, model: modelId(e.model, route.model), effort: route.effort })
  })

  on('command.run', { command: 'router' }, async ($, e) => {
    const arg = e.args.trim().toLowerCase()
    if (arg === 'on' || arg === 'off') {
      enabled = arg === 'on'
      await $.store.set('enabled', enabled)
      if (!enabled) {
        route = undefined
        $.ui.status(undefined)
      }
    } else if (arg && arg !== 'status') {
      return { text: 'Usage: /router [on|off|status]' }
    }
    const last = route ? `${route.model} · ${route.effort} — ${route.reason}` : 'none yet'
    return {
      text: [
        `Model router: ${enabled ? 'on' : 'off'}`,
        `Last route: ${last}`,
        `This session: haiku ${counts.haiku}, sonnet ${counts.sonnet}, opus ${counts.opus}`,
      ].join('\n'),
    }
  })
}

async function classify($: EngineInterface, text: string): Promise<Route | undefined> {
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
    timeoutMs: 20000,
  })
  if (!r.isAnswered) return undefined

  const json = r.text.match(/\{[\s\S]*\}/)?.[0]
  if (!json) return undefined
  try {
    const d = JSON.parse(json) as Partial<Route>
    if (!d.model || !TIERS.includes(d.model)) return undefined
    const effort = d.effort && EFFORTS.includes(d.effort) ? d.effort : 'medium'
    return { model: d.model, effort, reason: String(d.reason ?? '') }
  } catch {
    return undefined
  }
}

// Switch cost: whether moving the main thread to a cheaper model for one turn costs more
// in prompt-cache writes than the turn saves.
//
// The prompt cache is per model. A turn on another model first writes the whole context
// (system prompt, tools, conversation; ~35K tokens even on a session's first request) to
// that model's cache, and when the session comes back the warm model writes it again.
// Measured on real transcripts, those writes cost about twice what the cheaper turns
// saved, so a downgrade only pays when the context is small.
import type { Tier } from './tracker'

export type Rates = { input: number; cache_write_5m: number; cache_write_1h: number; cache_read: number; output: number }
type PriceRow = Rates & { long_context?: Rates & { over_prompt_tokens: number } }
export type TierPrices = Record<Tier, PriceRow>
// How long the session's cache writes live: Claude Code writes 5-minute or 1-hour cache.
export type CacheTtl = '5m' | '1h'

// The turn the comparison assumes the cheaper model would answer: a few requests, some
// new input (the prompt, tool results) and some output. Generous for a prompt the
// classifier sent down a tier, so the estimate leans toward switching.
export const ASSUMED_TURN = { steps: 3, newInput: 5_000, output: 2_000 }

// Each tier's price row from prices.json's text, by `ids` (tier -> model id), or
// undefined when the text doesn't parse or any tier's row is missing.
export function tierPrices(raw: unknown, ids: Record<Tier, string>): TierPrices | undefined {
  try {
    const d = JSON.parse(typeof raw === 'string' ? raw : '') as { models?: Record<string, PriceRow> }
    const out = {} as TierPrices
    for (const tier of Object.keys(ids) as Tier[]) {
      const p = d.models?.[ids[tier]]
      if (!p || ![p.input, p.cache_write_5m, p.cache_write_1h, p.cache_read, p.output].every(x => typeof x === 'number')) return undefined
      out[tier] = p
    }
    return out
  } catch {
    return undefined
  }
}

// The rates a request of `promptTokens` (input + cache reads + cache writes) is billed at.
function ratesFor(row: PriceRow, promptTokens: number): Rates {
  const lc = row.long_context
  return lc && promptTokens > lc.over_prompt_tokens ? lc : row
}

// For a turn moved from the `warm` model (whose cache holds `context` tokens) to the
// cheaper `target`, in USD, every cache write priced at the session's `ttl`:
//   cost:   the one-off switch, writing the context to the target's cache and writing it
//           to the warm model's again when the session comes back;
//   saving: what the assumed turn costs on the warm model, plus the cache read the return
//           would otherwise have been, less what the turn costs on the target.
// Staying on the warm model is cheaper when cost > saving.
export function switchCost(
  context: number,
  warm: Tier,
  target: Tier,
  prices: TierPrices,
  ttl: CacheTtl = '5m',
): { cost: number; saving: number } {
  const { steps, newInput, output } = ASSUMED_TURN
  const write = ttl === '1h' ? 'cache_write_1h' : 'cache_write_5m'
  const w = ratesFor(prices[warm], context)
  const t = ratesFor(prices[target], context)
  const cost = context * (t[write] + w[write])
  const onWarm = steps * context * w.cache_read + newInput * w[write] + output * w.output + context * w.cache_read
  const onTarget = (steps - 1) * context * t.cache_read + newInput * t[write] + output * t.output
  return { cost: cost / 1e6, saving: (onWarm - onTarget) / 1e6 }
}

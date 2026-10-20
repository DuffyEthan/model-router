import { describe, expect, test } from 'claude-code/testing'
import { switchCost, tierPrices, type TierPrices } from '../hooks/switch'
import { PRICES_JSON } from './prices'

const IDS = { haiku: 'claude-haiku-5-5', sonnet: 'claude-sonnet-5-5', opus: 'claude-opus-5-5' } as const
const PRICES = tierPrices(PRICES_JSON, IDS) as TierPrices
const near = (got: number, want: number) => expect(Math.abs(got - want)).toBeLessThan(1e-9)

describe('switch cost', () => {
  test('reads each tier\'s row from prices.json', () => {
    expect(PRICES.opus.cache_write_5m).toBe(5)
    expect(PRICES.haiku.long_context?.over_prompt_tokens).toBe(100000)
  })

  test('a missing tier, a malformed row or bad JSON gives no prices', () => {
    const only = JSON.stringify({ models: { 'claude-haiku-5-5': { input: 0.1, cache_write_5m: 0.125, cache_read: 0.01, output: 0.5 } } })
    expect(tierPrices(only, IDS)).toBeUndefined()
    const bad = JSON.parse(PRICES_JSON)
    bad.models['claude-opus-5-5'].cache_read = 'cheap'
    expect(tierPrices(JSON.stringify(bad), IDS)).toBeUndefined()
    expect(tierPrices('{nope', IDS)).toBeUndefined()
    expect(tierPrices(undefined, IDS)).toBeUndefined()
  })

  test('Opus to Haiku at 100K: writing the context twice outweighs the turn', () => {
    const { cost, saving } = switchCost(100_000, 'opus', 'haiku', PRICES)
    // 100K x ($0.125 Haiku write + $5 Opus re-write) per million
    near(cost, 0.5125)
    // on Opus: 3 reads + 5K written + 2K out + the return's read; on Haiku: 2 reads + 5K written + 2K out
    near(saving, 0.145 - 0.003625)
  })

  test('a 1-hour cache prices every write at the 1-hour rate', () => {
    const { cost, saving } = switchCost(100_000, 'opus', 'haiku', PRICES, '1h')
    // 100K x ($0.20 Haiku write + $8 Opus re-write) per million
    near(cost, 0.82)
    near(saving, 0.16 - 0.004)
  })

  test('a row without a 1-hour write rate gives no prices', () => {
    const bad = JSON.parse(PRICES_JSON)
    delete bad.models['claude-sonnet-5-5'].cache_write_1h
    expect(tierPrices(JSON.stringify(bad), IDS)).toBeUndefined()
  })

  test('Haiku bills a prompt over 100K at its long-context rates', () => {
    const { cost, saving } = switchCost(150_000, 'opus', 'haiku', PRICES)
    near(cost, 0.15 * (0.625 + 5))
    near(saving, 0.185 - 0.023125)
  })

  // ~35K tokens (system prompt and tools) is the least a real request carries.
  for (const [warm, target] of [['opus', 'haiku'], ['opus', 'sonnet'], ['sonnet', 'haiku']] as const) {
    test(`${warm} to ${target}: costs more than it saves from 35K, saves on a small context`, () => {
      for (const context of [35_000, 100_000, 150_000, 500_000]) {
        for (const ttl of ['5m', '1h'] as const) {
          const { cost, saving } = switchCost(context, warm, target, PRICES, ttl)
          expect(cost).toBeGreaterThan(saving)
        }
      }
      const small = switchCost(2_000, warm, target, PRICES)
      expect(small.cost).toBeLessThan(small.saving)
    })
  }
})

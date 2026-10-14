import { describe, expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'
import { capLog, formatStats, MAX_RECORDS, type LogRecord, type PromptRecord } from '../hooks/tracker'

const STEP = { turnId: 't1', index: 0, model: 'claude-opus-5-5', effort: 'high', messageCount: 3 } as const
const HAIKU = '{"model":"haiku","effort":"low","reason":"trivial"}'
const USAGE = { input_tokens: 900, output_tokens: 20, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 }
const SESSION = 'sess-0001'
// A prompt whose words must never reach the store or an export.
const SECRET = 'zebra-quartz-marmalade-7731 please refactor the payments module'
const SECRET_PARTS = ['zebra', 'quartz', 'marmalade', '7731', 'payments', 'refactor']

type Store = { data: Record<string, unknown>; writes: string[]; failSet: boolean }

// Beneath the plugin: an in-memory store that records every write, Haiku answering
// `reply`, a session id, and fs.read / fs.write for prices.json and /router export.
function world(on: On, reply: string | (() => unknown), seed: Record<string, unknown> = {}) {
  const store: Store = { data: { ...seed }, writes: [], failSet: false }
  const files: Record<string, string> = {}
  on('store.get', (_$, e) => ({ value: store.data[e.key] }) as never)
  on('store.set', (_$, e) => {
    if (store.failSet) throw new Error('store full')
    const json = JSON.stringify(e.value)
    store.writes.push(json)
    store.data[e.key] = JSON.parse(json)
    return { value: undefined } as never
  })
  on('store.keys', () => ({ value: Object.keys(store.data) }) as never)
  on('prompt.submit', (_$, e) => ({ text: e.text }) as never)
  on('session.start', (_$, e) => ({ cwd: e.cwd }) as never)
  on('session.id', () => ({ value: SESSION }) as never)
  on('command.register', () => ({ value: { command: 'router' } }) as never)
  on('command.run', () => ({ text: '' }) as never)
  on('session.messages', () => ({ value: [] }) as never)
  on('ui.status', () => ({ value: undefined }) as never)
  on('classic.PostModelSwitch', () => ({}) as never)
  on('env.get', (_$, e) => ({ value: e.name === 'HOME' ? '/home/test' : undefined }) as never)
  on('fs.write', (_$, e) => {
    files[e.path] = e.text
    return { value: undefined } as never
  })
  on('fs.read', (_$, e) => {
    if (!e.path.endsWith('/hooks/prices.json')) throw new Error('ENOENT')
    return {
      value: JSON.stringify({
        classifier_model: 'claude-haiku-5-5',
        models: { 'claude-haiku-5-5': { input: 0.1, cache_write_5m: 0.125, cache_read: 0.01, output: 0.5 } },
      }),
    } as never
  })
  on('model.complete', () => {
    if (typeof reply === 'string') return { value: { isAnswered: true, text: reply, usage: USAGE } } as never
    const value: unknown = reply()
    return (value instanceof Promise ? value.then(v => ({ value: v })) : { value }) as never
  })
  return { store, files }
}

function captureStep(on: On) {
  const sent: { model: string; effort?: unknown }[] = []
  on('turn.step', async function* (_$, e) {
    sent.push({ model: e.model, effort: e.effort })
    return { turnId: e.turnId, index: e.index, answer: '', toolUses: [] } as never
  })
  return sent
}

async function step($: any) {
  const stream = $.turn.step({ ...STEP })
  for await (const _ of stream) { /* drain */ }
  await stream.result
}

const start = ($: any) => $.session.start({ cwd: '/tmp', surface: null, isInteractive: true })
const submit = ($: any, text: string, extra: Record<string, unknown> = {}) =>
  $.prompt.submit({ text, origin: { kind: 'composer' }, ...extra })
const router = ($: any, args = '') =>
  $.command.run({
    command: 'router',
    args,
    origin: { kind: 'composer' },
    presentation: { isFullscreen: false, columns: 80 },
  })
const records = (s: Store) => ((s.data.log as LogRecord[] | undefined) ?? [])
const prompts = (s: Store) => records(s).filter((r): r is PromptRecord => r.kind === 'prompt')

describe('routing log: prompt records', () => {
  test('a routed user prompt logs tier, effort, latency, outcome, origin and session', async ($, on) => {
    const clock = mock.clock(on, { now: Date.UTC(2026, 9, 14, 12, 0, 0) })
    const { store } = world(on, async () => {
      await clock.advance(1234) // the classifier takes 1.234 s
      return { isAnswered: true, text: HAIKU, usage: USAGE }
    })
    await start($)
    await submit($, 'what does ls -la do')
    expect(prompts(store)).toEqual([
      {
        v: 1,
        kind: 'prompt',
        ts: '2026-10-14T12:00:00.000Z', // when the prompt arrived
        session: SESSION,
        tier: 'haiku',
        effort: 'low',
        latencyMs: 1234,
        outcome: 'ok',
        origin: 'user',
        originKind: 'composer',
        midTurn: false,
        usage: { in: 900, out: 20, cr: 0, cw: 0 },
      },
    ])
  })

  test('the prompt text never appears in the stored records or the export', async ($, on) => {
    const { store, files } = world(on, HAIKU)
    await start($)
    await submit($, SECRET)
    await submit($, `/${SECRET}`)
    await submit($, SECRET, { origin: { kind: 'scheduled-trigger' } })
    await submit($, SECRET, { turnId: 'turn-9' })
    await router($, 'off')
    await submit($, SECRET)
    await router($, 'export /tmp/out.jsonl')
    expect(prompts(store).length).toBe(5)
    const everything = [...store.writes, JSON.stringify(store.data), ...Object.values(files)].join('\n')
    for (const part of SECRET_PARTS) expect(everything).not.toContain(part)
  })

  for (const [kind, extra] of [
    ['scheduled-trigger', {}],
    ['slack-ping', {}],
    ['channel', { server: 'slack' }],
    ['auto-continuation', {}],
    ['task-notification', {}],
    ['plugin', { name: 'other' }],
  ] as const) {
    test(`a ${kind} prompt is logged unclassified with its origin`, async ($, on) => {
      let calls = 0
      const { store } = world(on, () => {
        calls += 1
        return { isAnswered: true, text: HAIKU, usage: USAGE }
      })
      await start($)
      await submit($, 'Background task finished', { origin: { kind, ...extra } })
      expect(calls).toBe(0)
      expect(prompts(store)[0]).toMatchObject({
        tier: 'session',
        effort: 'session',
        latencyMs: null,
        outcome: 'skipped',
        skip: 'non-user',
        origin: 'other',
        originKind: kind,
      })
    })
  }

  test('a slash command is logged as slash and skipped', async ($, on) => {
    const { store } = world(on, HAIKU)
    await start($)
    await submit($, '/security-review')
    expect(prompts(store)[0]).toMatchObject({ origin: 'slash', outcome: 'skipped', skip: 'slash', tier: 'session' })
  })

  test('a mid-turn prompt is flagged and still routed as before', async ($, on) => {
    const { store } = world(on, HAIKU)
    const sent = captureStep(on)
    await start($)
    await submit($, 'also check the tests', { turnId: 'turn-1' })
    await step($)
    expect(sent[0]?.model).toBe('claude-haiku-5-5')
    expect(prompts(store)[0]).toMatchObject({ midTurn: true, tier: 'haiku', outcome: 'ok' })
  })

  test('prompts while the router is off are logged as skipped', async ($, on) => {
    const { store } = world(on, HAIKU, { enabled: false })
    await start($)
    await submit($, 'hi')
    expect(prompts(store)[0]).toMatchObject({ outcome: 'skipped', skip: 'off', origin: 'user', tier: 'session' })
  })

  for (const [name, reply, outcome] of [
    ['an unparseable reply', () => ({ isAnswered: true, text: 'no json', usage: USAGE }), 'failed'],
    ['an API error', () => ({ isAnswered: false, reason: 'api-error', status: 529, error: 'overloaded', usage: USAGE }), 'failed'],
    ['a timeout', () => ({ isAnswered: false, reason: 'aborted', usage: USAGE }), 'timeout'],
    ['a throw', () => { throw new Error('boom') }, 'failed'],
  ] as const) {
    test(`${name} is logged as ${outcome}`, async ($, on) => {
      mock.clock(on)
      const { store } = world(on, reply as () => unknown)
      await start($)
      await submit($, 'hello there')
      expect(prompts(store)[0]).toMatchObject({ outcome, tier: 'session', origin: 'user' })
      expect(typeof prompts(store)[0]?.latencyMs).toBe('number')
    })
  }

  test('a store that refuses writes changes nothing about routing', async ($, on) => {
    const { store } = world(on, HAIKU)
    const sent = captureStep(on)
    store.failSet = true
    await start($)
    expect(await submit($, 'what does ls -la do')).toEqual({ text: 'what does ls -la do' })
    await step($)
    expect(sent).toEqual([{ model: 'claude-haiku-5-5', effort: 'low' }])
    expect((await router($, 'status')).text).toContain('Model router: on')
  })

  test('the log keeps only the newest MAX_RECORDS records', async ($, on) => {
    const old: LogRecord[] = Array.from({ length: MAX_RECORDS }, (_, i) => ({
      v: 1, kind: 'override', ts: `old-${i}`, session: null, action: 'router-on',
    }))
    const { store } = world(on, HAIKU, { log: old })
    await start($)
    await submit($, 'hi')
    const log = records(store)
    expect(log.length).toBe(MAX_RECORDS)
    expect(log[0]?.ts).toBe('old-1')
    expect(log[log.length - 1]?.kind).toBe('prompt')
  })
})

describe('routing log: overrides', () => {
  test('/router off and on are logged as overrides', async ($, on) => {
    const { store } = world(on, HAIKU)
    await start($)
    await router($, 'off')
    await router($, 'on')
    await router($, 'status')
    expect(records(store).map(r => (r.kind === 'override' ? r.action : r.kind))).toEqual(['router-off', 'router-on'])
  })

  for (const source of ['command', 'picker', 'sdk'] as const) {
    test(`a /model change (${source}) is logged as an override`, async ($, on) => {
      const { store } = world(on, HAIKU)
      await start($)
      await $.classic.PostModelSwitch({
        from_model: 'claude-sonnet-5-5', to_model: 'claude-opus-5-5', requested_model: 'opus', source,
        context_tokens: 1000, prompt_cache_warm: true, cache_ttl: '5m', estimated_cache_write_usd: 0.005, pricing: 'catalog',
      } as never)
      expect(records(store)).toEqual([
        { v: 1, kind: 'override', ts: expect.any(String), session: SESSION, action: 'model',
          from: 'claude-sonnet-5-5', to: 'claude-opus-5-5', source },
      ])
    })
  }

  for (const source of ['auto', 'resume'] as const) {
    test(`an automatic model switch (${source}) is not an override`, async ($, on) => {
      const { store } = world(on, HAIKU)
      await start($)
      await $.classic.PostModelSwitch({
        from_model: 'claude-opus-5-5', to_model: 'claude-sonnet-5-5', requested_model: null, source,
        context_tokens: 0, prompt_cache_warm: false, cache_ttl: '5m', estimated_cache_write_usd: 0, pricing: 'catalog',
      } as never)
      expect(records(store)).toEqual([])
    })
  }
})

describe('/router stats and export', () => {
  test('stats summarises the log and says it has no token data', async ($, on) => {
    world(on, HAIKU)
    await start($)
    await submit($, 'what does ls -la do')
    await submit($, 'Background task finished', { origin: { kind: 'scheduled-trigger' } })
    await router($, 'off')
    const text = (await router($, 'stats')).text
    expect(text).toContain('from the routing log only')
    expect(text).toContain('Prompts: 2 — user 1, slash 0, other 1 (scheduled-trigger 1)')
    expect(text).toContain('haiku 1 (50.0%)')
    expect(text).toContain('Classifier: ok 1, failed 0, timeout 0, skipped 1')
    // 900 input × $0.10/M + 20 output × $0.50/M = $0.00009 + $0.00001 = $0.0001
    expect(text).toContain('1 calls, 900 input / 20 output tokens ≈ $0.0001 API-equivalent')
    expect(text).toContain('/router off 1 — override rate 100.0% of user prompts (a rough quality proxy')
  })

  test('stats on an empty log', async ($, on) => {
    world(on, HAIKU)
    await start($)
    expect((await router($, 'stats')).text).toContain('No records yet.')
  })

  test('export writes JSON Lines to the default path', async ($, on) => {
    const { files } = world(on, HAIKU)
    await start($)
    await submit($, 'hi')
    const r = await router($, 'export')
    const path = '/home/test/.claude/model-router/routing-log.jsonl'
    expect(r.text).toContain(`Wrote 1 routing-log records to ${path}`)
    const lines = (files[path] ?? '').trim().split('\n')
    expect(lines.length).toBe(1)
    expect(JSON.parse(lines[0] ?? '{}')).toMatchObject({ kind: 'prompt', tier: 'haiku' })
  })

  test('the usage line lists the new subcommands', async ($, on) => {
    world(on, HAIKU)
    await start($)
    expect((await router($, 'banana')).text).toBe(
      'Usage: /router [on|off|status|stats|floor [haiku|sonnet|opus]|export [path]]',
    )
  })
})

describe('routing log: floor', () => {
  test('a prompt the floor raised logs the tier used and floored: true', async ($, on) => {
    const { store } = world(on, HAIKU, { floor: 'sonnet' })
    await start($)
    await submit($, 'what does ls -la do')
    expect(prompts(store)[0]).toMatchObject({ tier: 'sonnet', effort: 'low', outcome: 'ok', floored: true })
  })

  test('a prompt at or above the floor carries no floored field', async ($, on) => {
    const { store } = world(on, '{"model":"opus","effort":"high","reason":"x"}', { floor: 'sonnet' })
    await start($)
    await submit($, 'design the migration')
    await submit($, '/clear')
    for (const r of prompts(store)) expect('floored' in r).toBe(false)
  })

  test('/router floor saves the setting in the store; a bad one writes nothing', async ($, on) => {
    const { store } = world(on, HAIKU)
    await start($)
    await router($, 'floor sonnet')
    expect(store.data.floor).toBe('sonnet')
    await router($, 'floor banana')
    expect(store.data.floor).toBe('sonnet')
    await router($, 'floor haiku')
    expect(store.data.floor).toBe('haiku')
  })

  test('stats shows the current floor', async ($, on) => {
    world(on, HAIKU, { floor: 'opus' })
    await start($)
    expect((await router($, 'stats')).text).toContain('\nFloor: opus')
  })

  test('stats on an empty log still shows the floor', async ($, on) => {
    world(on, HAIKU)
    await start($)
    const text = (await router($, 'stats')).text
    expect(text).toContain('No records yet.')
    expect(text).toContain('Floor: haiku')
  })
})

describe('tracker helpers', () => {
  test('capLog trims by byte budget as well as count', () => {
    const big: LogRecord[] = Array.from({ length: 3000 }, (_, i) => ({
      v: 1, kind: 'override', ts: `t${i}`, session: 'x'.repeat(600), action: 'model',
    }))
    const out = capLog(big)
    expect(JSON.stringify(out).length).toBeLessThanOrEqual(1_500_000)
    expect(out[out.length - 1]?.ts).toBe('t2999')
  })

  test('formatStats ignores malformed entries', () => {
    expect(formatStats([null as never, { kind: 'mystery' } as never], undefined)).toContain('No records yet.')
  })
})

import { describe, expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

const STEP = { turnId: 't1', index: 0, model: 'claude-opus-5-5', effort: 'high', messageCount: 3 } as const

// Beneath the plugin: Haiku "answers" with `reply`; the session has no history.
function world(on: On, reply: string | (() => never), stored: Record<string, unknown> = {}) {
  mock.store(on, stored)
  on('prompt.submit', (_$, e) => ({ text: e.text }) as never)
  on('session.start', (_$, e) => ({ cwd: e.cwd }) as never)
  on('command.register', () => ({ value: { command: 'router' } }) as never)
  on('session.messages', () => ({ value: [] }) as never)
  on('ui.status', () => ({ value: undefined }) as never)
  on('model.complete', () => ({
    value: typeof reply === 'string' ? { isAnswered: true, text: reply, usage: {} } : reply(),
  }) as never)
}

// Records the model + effort the engine would have sent.
function captureStep(on: On) {
  const sent: { model: string; effort?: unknown }[] = []
  on('turn.step', async function* (_$, e) {
    sent.push({ model: e.model, effort: e.effort })
    return { turnId: e.turnId, index: e.index, answer: '', toolUses: [] } as never
  })
  return sent
}

async function step($: any, over: Record<string, unknown> = {}) {
  const stream = $.turn.step({ ...STEP, ...over })
  for await (const _ of stream) { /* drain */ }
  await stream.result
}

const start = ($: any) => $.session.start({ cwd: '/tmp', surface: null, isInteractive: true })
const submit = ($: any, text: string) => $.prompt.submit({ text })
const router = ($: any, args = '') =>
  $.command.run({
    command: 'router',
    args,
    origin: { kind: 'composer' },
    presentation: { isFullscreen: false, columns: 80 },
  })

describe('routing', () => {
  test('rewrites the turn to the tier Haiku picks', async ($, on) => {
    world(on, '{"model":"haiku","effort":"low","reason":"trivial"}')
    const sent = captureStep(on)
    await start($)
    await submit($, 'what does ls -la do')
    await step($)
    expect(sent).toEqual([{ model: 'claude-haiku-5-5', effort: 'low' }])
  })

  test('maps sonnet and opus to full model ids', async ($, on) => {
    world(on, '{"model":"sonnet","effort":"medium","reason":"coding"}')
    const sent = captureStep(on)
    await start($)
    await submit($, 'write a function')
    await step($)
    expect(sent[0]).toEqual({ model: 'claude-sonnet-5-5', effort: 'medium' })
  })

  test('keeps a provider prefix on the model id', async ($, on) => {
    world(on, '{"model":"opus","effort":"xhigh","reason":"hard"}')
    const sent = captureStep(on)
    await start($)
    await submit($, 'design the migration')
    await step($, { model: 'anthropic.claude-sonnet-5-5' })
    expect(sent[0]?.model).toBe('anthropic.claude-opus-5-5')
  })

  test('tolerates prose around the JSON and bad effort', async ($, on) => {
    world(on, 'Sure! {"model":"sonnet","effort":"turbo","reason":"x"} done')
    const sent = captureStep(on)
    await start($)
    await submit($, 'do a thing')
    await step($)
    expect(sent[0]).toEqual({ model: 'claude-sonnet-5-5', effort: 'medium' })
  })

})

describe('fails safe', () => {
  for (const [name, reply] of [
    ['garbage reply', 'I cannot do that'],
    ['unknown model', '{"model":"gpt","effort":"low","reason":"x"}'],
    ['broken JSON', '{"model":'],
  ] as const) {
    test(`leaves the session model alone on ${name}`, async ($, on) => {
      world(on, reply)
      const sent = captureStep(on)
      await start($)
      await submit($, 'hello there')
      await step($)
      expect(sent).toEqual([{ model: STEP.model, effort: STEP.effort }])
    })
  }

  test('leaves the session model alone when the classifier call fails', async ($, on) => {
    world(on, () => ({ isAnswered: false, reason: 'api-error' }) as never)
    const sent = captureStep(on)
    await start($)
    await submit($, 'hello there')
    await step($)
    expect(sent[0]?.model).toBe(STEP.model)
  })

  test('does not reuse an old route when classification throws', async ($, on) => {
    let n = 0
    world(on, () => {
      n += 1
      if (n > 1) throw new Error('classifier blew up') // a rejected call, not just a failed one
      return { isAnswered: true, text: '{"model":"haiku","effort":"low","reason":"x"}', usage: {} } as never
    })
    const sent = captureStep(on)
    await start($)
    await submit($, 'first')
    await submit($, 'second').catch(() => undefined)
    await step($)
    expect(sent[0]?.model).toBe(STEP.model)
  })

  test('does not route subagent requests', async ($, on) => {
    world(on, '{"model":"haiku","effort":"low","reason":"x"}')
    const sent = captureStep(on)
    await start($)
    await submit($, 'hi')
    await step($, { agentId: 'sub-1' })
    expect(sent[0]?.model).toBe(STEP.model)
  })

  test('does not classify slash commands or empty prompts', async ($, on) => {
    let calls = 0
    world(on, () => { calls += 1; return { isAnswered: true, text: '{}', usage: {} } as never })
    await start($)
    await submit($, '/clear')
    await submit($, '   ')
    expect(calls).toBe(0)
  })
})

describe('/router command', () => {
  test('status reports on and counts', async ($, on) => {
    world(on, '{"model":"opus","effort":"high","reason":"hard task"}')
    await start($)
    await submit($, 'design something big')
    const r = await router($)
    expect(r.text).toContain('Model router: on')
    expect(r.text).toContain('opus · high — hard task')
    expect(r.text).toContain('opus 1')
  })

  test('off stops routing, on resumes', async ($, on) => {
    world(on, '{"model":"haiku","effort":"low","reason":"x"}')
    const sent = captureStep(on)
    await start($)
    expect((await router($, 'off')).text).toContain('Model router: off')
    await submit($, 'hi')
    await step($)
    expect(sent[0]?.model).toBe(STEP.model)
    expect((await router($, 'on')).text).toContain('Model router: on')
    await submit($, 'hi again')
    await step($)
    expect(sent[1]?.model).toBe('claude-haiku-5-5')
  })

  test('rejects unknown arguments', async ($, on) => {
    world(on, '{}')
    await start($)
    expect((await router($, 'banana')).text).toContain('Usage: /router')
  })

  test('an off setting saved by an earlier session is honoured', async ($, on) => {
    world(on, '{"model":"haiku","effort":"low","reason":"x"}', { enabled: false })
    const sent = captureStep(on)
    await start($)
    await submit($, 'hi')
    await step($)
    expect(sent[0]?.model).toBe(STEP.model)
    expect((await router($)).text).toContain('Model router: off')
  })
})

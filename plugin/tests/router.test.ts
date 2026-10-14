import { describe, expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'

const STEP = { turnId: 't1', index: 0, model: 'claude-opus-5-5', effort: 'high', messageCount: 3 } as const

// Beneath the plugin: Haiku "answers" with `reply`; the session has no history.
// Returns the status-line texts the plugin set, in order.
function world(on: On, reply: string | (() => unknown), stored: Record<string, unknown> = {}) {
  const statuses: (string | undefined)[] = []
  mock.store(on, stored)
  on('prompt.submit', (_$, e) => ({ text: e.text }) as never)
  on('session.start', (_$, e) => ({ cwd: e.cwd }) as never)
  on('command.register', () => ({ value: { command: 'router' } }) as never)
  on('command.run', () => ({ text: '' }) as never)
  on('session.messages', () => ({ value: [] }) as never)
  on('ui.status', (_$, e) => {
    statuses.push(e.text)
    return { value: undefined } as never
  })
  on('model.complete', () => {
    if (typeof reply === 'string') return { value: { isAnswered: true, text: reply, usage: {} } } as never
    const value: unknown = reply()
    // A reply that is a promise settles the call when it does (a rejection rejects it).
    return (value instanceof Promise ? value.then(v => ({ value: v })) : { value }) as never
  })
  return statuses
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
const submit = ($: any, text: string, origin?: unknown) =>
  $.prompt.submit(origin ? { text, origin } : { text })
const HAIKU = '{"model":"haiku","effort":"low","reason":"trivial"}'
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

  test('tolerates prose around the JSON and bad effort', async ($, on) => {
    world(on, 'Sure! {"model":"sonnet","effort":"turbo","reason":"x"} done')
    const sent = captureStep(on)
    await start($)
    await submit($, 'do a thing')
    await step($)
    expect(sent[0]).toEqual({ model: 'claude-sonnet-5-5', effort: 'medium' })
  })

})

describe('provider model ids', () => {
  // Session model id -> id the routed (opus) request should carry. Claude 5.x ids have no
  // version suffix on any provider, so the session's prefix is all that carries over.
  for (const [session, expected] of [
    ['claude-sonnet-5-5', 'claude-opus-5-5'], // first-party API, and Vertex AI (bare id)
    ['claude-haiku-4-5-20251001', 'claude-opus-5-5'], // first-party dated snapshot
    ['anthropic.claude-sonnet-5-5', 'anthropic.claude-opus-5-5'], // Bedrock Mantle / base id
    ['us.anthropic.claude-sonnet-5-5', 'us.anthropic.claude-opus-5-5'], // Bedrock regional profile
    ['eu.anthropic.claude-sonnet-5-5', 'eu.anthropic.claude-opus-5-5'],
    ['apac.anthropic.claude-sonnet-5-5', 'apac.anthropic.claude-opus-5-5'],
    ['us-gov.anthropic.claude-sonnet-5-5', 'us-gov.anthropic.claude-opus-5-5'],
    ['global.anthropic.claude-sonnet-5-5', 'global.anthropic.claude-opus-5-5'], // Bedrock global profile
  ] as const) {
    test(`routes ${session} to ${expected}`, async ($, on) => {
      world(on, '{"model":"opus","effort":"xhigh","reason":"hard"}')
      const sent = captureStep(on)
      await start($)
      await submit($, 'design the migration')
      await step($, { model: session })
      expect(sent[0]).toEqual({ model: expected, effort: 'xhigh' })
    })
  }

  // Formats whose suffix for another tier can't be derived: leave the request unchanged.
  for (const session of [
    'us.anthropic.claude-opus-5-5-v1:0', // Bedrock "-vN:M" suffix
    'anthropic.claude-haiku-4-5-20251001-v1:0', // Bedrock ARN-versioned base id
    'global.anthropic.claude-opus-4-6-v1', // Bedrock "-vN" suffix
    'claude-haiku-4-5@20251001', // Vertex dated snapshot
    'arn:aws:bedrock:us-east-2:123456789012:application-inference-profile/opus-prod',
    'claude-opus-4-6[1m]',
    'mycorp.claude-opus-5-5', // unknown provider prefix
    'us.anthropic.claude-opus-5-5-20260101', // dated id with a Bedrock prefix
  ]) {
    test(`leaves unrecognised id ${session} unchanged`, async ($, on) => {
      world(on, HAIKU)
      const sent = captureStep(on)
      await start($)
      await submit($, 'hi')
      await step($, { model: session })
      expect(sent).toEqual([{ model: session, effort: STEP.effort }])
    })
  }
})

describe('stale routes', () => {
  test('a slash command after a Haiku-routed prompt runs on the session model', async ($, on) => {
    world(on, HAIKU)
    const sent = captureStep(on)
    await start($)
    await submit($, 'what does ls -la do')
    await step($)
    await submit($, '/security-review')
    await step($)
    expect(sent.map(s => s.model)).toEqual(['claude-haiku-5-5', STEP.model])
  })

  test('a command run after a Haiku-routed prompt runs on the session model', async ($, on) => {
    world(on, HAIKU)
    const sent = captureStep(on)
    await start($)
    await submit($, 'what does ls -la do')
    await $.command.run({ command: 'security-review', args: '' })
    await step($)
    expect(sent[0]?.model).toBe(STEP.model)
  })

  test('/router status keeps the current route', async ($, on) => {
    world(on, HAIKU)
    const sent = captureStep(on)
    await start($)
    await submit($, 'what does ls -la do')
    await router($, 'status')
    await step($)
    expect(sent[0]?.model).toBe('claude-haiku-5-5')
  })

  for (const origin of [
    { kind: 'task-notification' },
    { kind: 'scheduled-trigger' },
    { kind: 'peer' },
    { kind: 'unclassified' },
    { kind: 'plugin', name: 'other' },
  ]) {
    test(`a ${origin.kind} turn after a Haiku-routed prompt runs on the session model`, async ($, on) => {
      let calls = 0
      world(on, () => {
        calls += 1
        return { isAnswered: true, text: HAIKU, usage: {} } as never
      })
      const sent = captureStep(on)
      await start($)
      await submit($, 'what does ls -la do', { kind: 'composer' })
      await step($)
      await submit($, 'Background task finished: build passed', origin)
      await step($)
      expect(sent.map(s => s.model)).toEqual(['claude-haiku-5-5', STEP.model])
      expect(calls).toBe(1) // non-user turns are not sent to the classifier
    })
  }

  test('a plugin prompt submitted as the user is classified', async ($, on) => {
    world(on, HAIKU)
    const sent = captureStep(on)
    await start($)
    await submit($, 'hi', { kind: 'plugin', name: 'other', asUser: true })
    await step($)
    expect(sent[0]?.model).toBe('claude-haiku-5-5')
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

  for (const [kind, fail] of [
    ['synchronously', () => { throw new Error('classifier blew up') }],
    ['asynchronously', () => Promise.reject(new Error('classifier blew up'))],
  ] as const) {
    test(`a classifier that throws ${kind} never blocks the prompt`, async ($, on) => {
      const statuses = world(on, fail as () => never)
      const sent = captureStep(on)
      await start($)
      const r = await submit($, 'hello there')
      expect(r).toEqual({ text: 'hello there' })
      await step($)
      expect(sent).toEqual([{ model: STEP.model, effort: STEP.effort }])
      expect(statuses).toEqual(['router → session model (classifier failed)'])
    })
  }

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

describe('/router floor', () => {
  const SONNET = '{"model":"sonnet","effort":"medium","reason":"coding"}'
  const OPUS = '{"model":"opus","effort":"xhigh","reason":"hard"}'

  test('default floor is haiku: a Haiku pick is not raised', async ($, on) => {
    const statuses = world(on, HAIKU)
    const sent = captureStep(on)
    await start($)
    await submit($, 'what does ls -la do')
    await step($)
    expect(sent).toEqual([{ model: 'claude-haiku-5-5', effort: 'low' }])
    expect(statuses).toEqual(['router → haiku · low'])
    expect((await router($, 'floor')).text).toContain('Floor: haiku')
  })

  // [floor, classifier reply, model sent, effort sent]
  for (const [floor, reply, model, effort] of [
    ['haiku', SONNET, 'claude-sonnet-5-5', 'medium'],
    ['haiku', OPUS, 'claude-opus-5-5', 'xhigh'],
    ['sonnet', HAIKU, 'claude-sonnet-5-5', 'low'],
    ['sonnet', SONNET, 'claude-sonnet-5-5', 'medium'],
    ['sonnet', OPUS, 'claude-opus-5-5', 'xhigh'],
    ['opus', HAIKU, 'claude-opus-5-5', 'low'],
    ['opus', SONNET, 'claude-opus-5-5', 'medium'],
    ['opus', OPUS, 'claude-opus-5-5', 'xhigh'],
  ] as const) {
    test(`floor ${floor}: ${JSON.parse(reply).model} is sent as ${model} @ ${effort}`, async ($, on) => {
      world(on, reply)
      const sent = captureStep(on)
      await start($)
      await router($, `floor ${floor}`)
      await submit($, 'a prompt')
      await step($)
      expect(sent).toEqual([{ model, effort }])
    })
  }

  test('a raised route keeps every effort the classifier can return', async ($, on) => {
    const efforts = ['low', 'medium', 'high', 'xhigh']
    let i = 0
    world(on, () => {
      const text = `{"model":"haiku","effort":"${efforts[i++]}","reason":"x"}`
      return { isAnswered: true, text, usage: {} } as never
    })
    const sent = captureStep(on)
    await start($)
    await router($, 'floor opus')
    for (const _ of efforts) {
      await submit($, 'a prompt')
      await step($)
    }
    expect(sent).toEqual(efforts.map(effort => ({ model: 'claude-opus-5-5', effort })))
  })

  test('a raised route with a bad effort runs at medium', async ($, on) => {
    world(on, '{"model":"haiku","effort":"turbo","reason":"x"}')
    const sent = captureStep(on)
    await start($)
    await router($, 'floor sonnet')
    await submit($, 'a prompt')
    await step($)
    expect(sent).toEqual([{ model: 'claude-sonnet-5-5', effort: 'medium' }])
  })

  test('the status line and /router status show the raised route', async ($, on) => {
    const statuses = world(on, HAIKU)
    await start($)
    await router($, 'floor sonnet')
    await submit($, 'what does ls -la do')
    expect(statuses).toEqual(['router → sonnet · low'])
    const text = (await router($, 'status')).text
    expect(text).toContain('Floor: sonnet')
    expect(text).toContain('Last route: sonnet · low — trivial (raised from haiku by the floor)')
    expect(text).toContain('haiku 0, sonnet 1, opus 0') // counts the tier actually used
  })

  test('/router floor with no argument reports the floor', async ($, on) => {
    world(on, HAIKU, { floor: 'opus' })
    await start($)
    expect((await router($, 'floor')).text).toBe('Floor: opus (the router never picks a model below opus)')
  })

  test('/router floor <tier> sets it, is case-insensitive and reports it', async ($, on) => {
    world(on, HAIKU)
    await start($)
    expect((await router($, 'floor Sonnet')).text).toBe(
      'Floor: sonnet (the router never picks a model below sonnet)',
    )
    expect((await router($)).text).toContain('Floor: sonnet')
  })

  for (const args of ['floor banana', 'floor gpt-4', 'floor sonnet opus', 'floor 2', 'floors']) {
    test(`rejects /router ${args} and keeps the floor`, async ($, on) => {
      world(on, HAIKU, { floor: 'sonnet' })
      await start($)
      expect((await router($, args)).text).toBe(
        'Usage: /router [on|off|status|stats|floor [haiku|sonnet|opus]|export [path]]',
      )
      expect((await router($, 'floor')).text).toContain('Floor: sonnet')
    })
  }

  test('status shows the default floor', async ($, on) => {
    world(on, HAIKU)
    await start($)
    expect((await router($)).text).toContain('Floor: haiku')
  })

  test('a floor saved by an earlier session is honoured', async ($, on) => {
    world(on, HAIKU, { floor: 'opus' })
    const sent = captureStep(on)
    await start($)
    await submit($, 'hi')
    await step($)
    expect(sent).toEqual([{ model: 'claude-opus-5-5', effort: 'low' }])
  })

  for (const corrupt of ['banana', 'Opus', '', 3, null, true, ['opus'], { tier: 'opus' }]) {
    test(`a stored floor of ${JSON.stringify(corrupt)} is treated as haiku`, async ($, on) => {
      world(on, HAIKU, { floor: corrupt })
      const sent = captureStep(on)
      await start($)
      await submit($, 'hi')
      await step($)
      expect(sent).toEqual([{ model: 'claude-haiku-5-5', effort: 'low' }])
      expect((await router($, 'floor')).text).toContain('Floor: haiku')
    })
  }
})

describe('/router floor leaves unrouted turns alone', () => {
  // With the highest floor stored, none of these paths may change the session model.
  const FLOOR = { floor: 'opus' }
  const SESSION = { model: 'claude-haiku-5-5', effort: 'low' } as const

  test('slash commands', async ($, on) => {
    world(on, HAIKU, FLOOR)
    const sent = captureStep(on)
    await start($)
    await submit($, '/security-review')
    await step($, SESSION)
    await $.command.run({ command: 'security-review', args: '' })
    await step($, SESSION)
    expect(sent).toEqual([SESSION, SESSION])
  })

  test('non-user turns', async ($, on) => {
    world(on, HAIKU, FLOOR)
    const sent = captureStep(on)
    await start($)
    await submit($, 'Background task finished', { kind: 'task-notification' })
    await step($, SESSION)
    expect(sent).toEqual([SESSION])
  })

  test('subagent requests', async ($, on) => {
    world(on, HAIKU, FLOOR)
    const sent = captureStep(on)
    await start($)
    await submit($, 'hi')
    await step($, { ...SESSION, agentId: 'sub-1' })
    expect(sent).toEqual([SESSION])
  })

  for (const [name, reply] of [
    ['a failed classifier call', () => ({ isAnswered: false, reason: 'api-error' })],
    ['a classifier timeout', () => ({ isAnswered: false, reason: 'aborted' })],
    ['an unusable reply', () => ({ isAnswered: true, text: 'no idea', usage: {} })],
    ['a classifier that throws', () => { throw new Error('boom') }],
  ] as const) {
    test(name, async ($, on) => {
      const statuses = world(on, reply as () => never, FLOOR)
      const sent = captureStep(on)
      await start($)
      await submit($, 'hello there')
      await step($, SESSION)
      expect(sent).toEqual([SESSION])
      expect(statuses).toEqual(['router → session model (classifier failed)'])
    })
  }

  test('unrecognised model ids', async ($, on) => {
    world(on, HAIKU, FLOOR)
    const sent = captureStep(on)
    await start($)
    await submit($, 'hi')
    await step($, { model: 'us.anthropic.claude-haiku-5-5-v1:0', effort: 'low' })
    expect(sent).toEqual([{ model: 'us.anthropic.claude-haiku-5-5-v1:0', effort: 'low' }])
  })

  test('the router switched off', async ($, on) => {
    world(on, HAIKU, { ...FLOOR, enabled: false })
    const sent = captureStep(on)
    await start($)
    await submit($, 'hi')
    await step($, SESSION)
    expect(sent).toEqual([SESSION])
  })
})

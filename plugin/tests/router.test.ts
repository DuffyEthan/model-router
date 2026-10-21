import { describe, expect, mock, test } from 'claude-code/testing'
import type { On } from 'claude-code'
import { PRICES_JSON } from './prices'

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
        'Usage: /router [on|off|status|stats|floor [haiku|sonnet|opus]|ceiling [haiku|sonnet|opus]|background [session|sonnet|haiku] [low|medium|high|xhigh]|sticky [on|off]|export [path]]',
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

describe('/router ceiling', () => {
  const SONNET = '{"model":"sonnet","effort":"medium","reason":"coding"}'
  const OPUS = '{"model":"opus","effort":"xhigh","reason":"hard"}'

  test('default ceiling is opus: an Opus pick is not capped', async ($, on) => {
    const statuses = world(on, OPUS)
    const sent = captureStep(on)
    await start($)
    await submit($, 'design the migration')
    await step($)
    expect(sent).toEqual([{ model: 'claude-opus-5-5', effort: 'xhigh' }])
    expect(statuses).toEqual(['router → opus · xhigh'])
    expect((await router($, 'ceiling')).text).toContain('Ceiling: opus')
  })

  // [ceiling, classifier reply, model sent, effort sent]
  for (const [ceiling, reply, model, effort] of [
    ['opus', HAIKU, 'claude-haiku-5-5', 'low'],
    ['opus', SONNET, 'claude-sonnet-5-5', 'medium'],
    ['sonnet', HAIKU, 'claude-haiku-5-5', 'low'],
    ['sonnet', SONNET, 'claude-sonnet-5-5', 'medium'],
    ['sonnet', OPUS, 'claude-sonnet-5-5', 'xhigh'],
    ['haiku', HAIKU, 'claude-haiku-5-5', 'low'],
    ['haiku', SONNET, 'claude-haiku-5-5', 'medium'],
    ['haiku', OPUS, 'claude-haiku-5-5', 'xhigh'],
  ] as const) {
    test(`ceiling ${ceiling}: ${JSON.parse(reply).model} is sent as ${model} @ ${effort}`, async ($, on) => {
      world(on, reply)
      const sent = captureStep(on)
      await start($)
      await router($, `ceiling ${ceiling}`)
      await submit($, 'a prompt')
      await step($)
      expect(sent).toEqual([{ model, effort }])
    })
  }

  test('a capped route with a bad effort runs at medium', async ($, on) => {
    world(on, '{"model":"opus","effort":"turbo","reason":"x"}')
    const sent = captureStep(on)
    await start($)
    await router($, 'ceiling sonnet')
    await submit($, 'a prompt')
    await step($)
    expect(sent).toEqual([{ model: 'claude-sonnet-5-5', effort: 'medium' }])
  })

  test('the status line and /router status show the capped route', async ($, on) => {
    const statuses = world(on, OPUS)
    await start($)
    await router($, 'ceiling sonnet')
    await submit($, 'design the migration')
    expect(statuses).toEqual(['router → sonnet · xhigh'])
    const text = (await router($, 'status')).text
    expect(text).toContain('Ceiling: sonnet')
    expect(text).toContain('Last route: sonnet · xhigh — hard (capped from opus by the ceiling)')
    expect(text).toContain('haiku 0, sonnet 1, opus 0') // counts the tier actually used
  })

  test('/router ceiling with no argument reports the ceiling', async ($, on) => {
    world(on, HAIKU, { ceiling: 'haiku' })
    await start($)
    expect((await router($, 'ceiling')).text).toBe('Ceiling: haiku (the router never picks a model above haiku)')
  })

  test('/router ceiling <tier> sets it, is case-insensitive and reports it', async ($, on) => {
    world(on, HAIKU)
    await start($)
    expect((await router($, 'ceiling Sonnet')).text).toBe(
      'Ceiling: sonnet (the router never picks a model above sonnet)',
    )
    expect((await router($)).text).toContain('Ceiling: sonnet')
  })

  for (const args of ['ceiling banana', 'ceiling gpt-4', 'ceiling sonnet opus', 'ceiling 2', 'ceilings']) {
    test(`rejects /router ${args} and keeps the ceiling`, async ($, on) => {
      world(on, HAIKU, { ceiling: 'sonnet' })
      await start($)
      expect((await router($, args)).text).toBe(
        'Usage: /router [on|off|status|stats|floor [haiku|sonnet|opus]|ceiling [haiku|sonnet|opus]|background [session|sonnet|haiku] [low|medium|high|xhigh]|sticky [on|off]|export [path]]',
      )
      expect((await router($, 'ceiling')).text).toContain('Ceiling: sonnet')
    })
  }

  test('refuses a ceiling below the floor and keeps both', async ($, on) => {
    world(on, HAIKU, { floor: 'sonnet' })
    await start($)
    expect((await router($, 'ceiling haiku')).text).toBe(
      'Ceiling not changed: haiku is below the floor (sonnet). Lower the floor first.',
    )
    const text = (await router($)).text
    expect(text).toContain('Floor: sonnet')
    expect(text).toContain('Ceiling: opus')
  })

  test('refuses a floor above the ceiling and keeps both', async ($, on) => {
    world(on, HAIKU, { ceiling: 'sonnet' })
    await start($)
    expect((await router($, 'floor opus')).text).toBe(
      'Floor not changed: opus is above the ceiling (sonnet). Raise the ceiling first.',
    )
    const text = (await router($)).text
    expect(text).toContain('Floor: haiku')
    expect(text).toContain('Ceiling: sonnet')
  })

  test('a floor equal to the ceiling pins every route to that tier', async ($, on) => {
    world(on, sequence(HAIKU, OPUS))
    const sent = captureStep(on)
    await start($)
    await router($, 'ceiling sonnet')
    await router($, 'floor sonnet')
    for (const _ of [1, 2]) {
      await submit($, 'a prompt')
      await step($)
    }
    expect(sent).toEqual([
      { model: 'claude-sonnet-5-5', effort: 'low' },
      { model: 'claude-sonnet-5-5', effort: 'xhigh' },
    ])
  })

  test('a stored floor above the stored ceiling: the ceiling wins', async ($, on) => {
    world(on, HAIKU, { floor: 'opus', ceiling: 'sonnet' })
    const sent = captureStep(on)
    await start($)
    await submit($, 'hi')
    await step($)
    expect(sent).toEqual([{ model: 'claude-sonnet-5-5', effort: 'low' }])
  })

  test('a ceiling saved by an earlier session is honoured', async ($, on) => {
    world(on, OPUS, { ceiling: 'haiku' })
    const sent = captureStep(on)
    await start($)
    await submit($, 'design it')
    await step($)
    expect(sent).toEqual([{ model: 'claude-haiku-5-5', effort: 'xhigh' }])
  })

  for (const corrupt of ['banana', 'Haiku', '', 3, null, true, ['haiku'], { tier: 'haiku' }]) {
    test(`a stored ceiling of ${JSON.stringify(corrupt)} is treated as opus`, async ($, on) => {
      world(on, OPUS, { ceiling: corrupt })
      const sent = captureStep(on)
      await start($)
      await submit($, 'design it')
      await step($)
      expect(sent).toEqual([{ model: 'claude-opus-5-5', effort: 'xhigh' }])
      expect((await router($, 'ceiling')).text).toContain('Ceiling: opus')
    })
  }

  // Like the floor, the ceiling only changes what the classifier picks.
  test('slash commands and non-user turns keep the session model', async ($, on) => {
    world(on, HAIKU, { ceiling: 'haiku' })
    const sent = captureStep(on)
    await start($)
    await submit($, '/security-review')
    await step($)
    await submit($, 'Background task finished', { kind: 'task-notification' })
    await step($)
    expect(sent).toEqual([
      { model: STEP.model, effort: STEP.effort },
      { model: STEP.model, effort: STEP.effort },
    ])
  })

  test('the background setting is not capped', async ($, on) => {
    world(on, HAIKU, { ceiling: 'haiku', background: 'sonnet' })
    const sent = captureStep(on)
    await start($)
    await submit($, 'Background task finished', { kind: 'task-notification' })
    await step($)
    expect(sent).toEqual([{ model: 'claude-sonnet-5-5', effort: 'medium' }])
  })
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

describe('/router background', () => {
  test('status shows the default background setting', async ($, on) => {
    world(on, HAIKU)
    await start($)
    expect((await router($)).text).toContain('Background: session')
    expect((await router($, 'background')).text).toBe(
      'Background: session (background notifications and subagents route to session)',
    )
  })

  test('/router background <tier> sets it, is case-insensitive and reports it', async ($, on) => {
    world(on, HAIKU)
    await start($)
    expect((await router($, 'background Sonnet')).text).toBe(
      'Background: sonnet · medium (background notifications and subagents route to sonnet)',
    )
    expect((await router($)).text).toContain('Background: sonnet')
    expect((await router($, 'background haiku')).text).toBe(
      'Background: haiku · low (background notifications and subagents route to haiku)',
    )
    expect((await router($)).text).toContain('Background: haiku')
    expect((await router($, 'background session')).text).toBe(
      'Background: session (background notifications and subagents route to session)',
    )
    expect((await router($)).text).toContain('Background: session')
  })

  for (const args of [
    'background opus',
    'background banana',
    'background gpt-4',
    'background sonnet haiku',
    'background 2',
    'backgrounds',
    'background sonnet max',
    'background sonnet high extra',
    'background session high',
    'background high',
  ]) {
    test(`rejects /router ${args} and keeps the background setting`, async ($, on) => {
      world(on, HAIKU, { background: 'sonnet' })
      await start($)
      expect((await router($, args)).text).toBe(
        'Usage: /router [on|off|status|stats|floor [haiku|sonnet|opus]|ceiling [haiku|sonnet|opus]|background [session|sonnet|haiku] [low|medium|high|xhigh]|sticky [on|off]|export [path]]',
      )
      expect((await router($, 'background')).text).toContain('Background: sonnet')
    })
  }

  test('/router background <tier> <effort> sets the effort for background turns and subagents', async ($, on) => {
    world(on, HAIKU)
    const sent = captureStep(on)
    await start($)
    expect((await router($, 'background sonnet High')).text).toBe(
      'Background: sonnet · high (background notifications and subagents route to sonnet)',
    )
    expect((await router($)).text).toContain('Background: sonnet · high')
    await submit($, 'Background task finished', { kind: 'task-notification' })
    await step($)
    await step($, { model: 'claude-opus-5-5', agentId: 'sub-1' })
    expect(sent).toEqual([
      { model: 'claude-sonnet-5-5', effort: 'high' },
      { model: 'claude-sonnet-5-5', effort: 'high' },
    ])
  })

  test('/router background <tier> without an effort resets it to the tier default', async ($, on) => {
    world(on, HAIKU, { background: 'sonnet', backgroundEffort: 'xhigh' })
    await start($)
    expect((await router($, 'background')).text).toContain('Background: sonnet · xhigh')
    expect((await router($, 'background haiku')).text).toContain('Background: haiku · low')
  })

  test('a subagent at or below the background tier keeps its effort even with one set', async ($, on) => {
    world(on, HAIKU, { background: 'sonnet', backgroundEffort: 'high' })
    const sent = captureStep(on)
    await start($)
    await submit($, 'hi')
    await step($, { model: 'claude-haiku-5-5', effort: 'low', agentId: 'sub-explore-1' })
    expect(sent).toEqual([{ model: 'claude-haiku-5-5', effort: 'low' }])
  })

  for (const corrupt of ['banana', 'High', '', 3, true, ['high']]) {
    test(`a stored background effort of ${JSON.stringify(corrupt)} falls back to the tier default`, async ($, on) => {
      world(on, HAIKU, { background: 'sonnet', backgroundEffort: corrupt })
      const sent = captureStep(on)
      await start($)
      await step($, { model: 'claude-opus-5-5', agentId: 'sub-1' })
      expect(sent).toEqual([{ model: 'claude-sonnet-5-5', effort: 'medium' }])
    })
  }

  test('a background setting saved by an earlier session is honoured', async ($, on) => {
    world(on, HAIKU, { background: 'sonnet' })
    const sent = captureStep(on)
    await start($)
    await submit($, 'Background task finished', { kind: 'task-notification' })
    await step($)
    expect(sent).toEqual([{ model: 'claude-sonnet-5-5', effort: 'medium' }])
  })

  for (const corrupt of ['banana', 'Sonnet', '', 3, null, true, ['sonnet'], { tier: 'sonnet' }]) {
    test(`a stored background of ${JSON.stringify(corrupt)} is treated as session`, async ($, on) => {
      world(on, HAIKU, { background: corrupt })
      const sent = captureStep(on)
      await start($)
      await submit($, 'Background task finished', { kind: 'task-notification' })
      await step($)
      expect(sent).toEqual([{ model: STEP.model, effort: STEP.effort }])
      expect((await router($, 'background')).text).toContain('Background: session')
    })
  }
})

describe('background origin routing', () => {
  for (const origin of [
    { kind: 'task-notification' },
    { kind: 'peer' },
    { kind: 'scheduled-trigger' },
    { kind: 'unclassified' },
  ]) {
    test(`routes ${origin.kind} to sonnet @ medium when configured`, async ($, on) => {
      let calls = 0
      const statuses = world(on, () => {
        calls += 1
        return { isAnswered: true, text: HAIKU, usage: {} } as never
      }, { background: 'sonnet' })
      const sent = captureStep(on)
      await start($)
      await submit($, 'Background event occurred', origin)
      await step($)
      expect(sent).toEqual([{ model: 'claude-sonnet-5-5', effort: 'medium' }])
      expect(calls).toBe(0) // classifier is not called
      expect(statuses).toEqual(['router → sonnet · medium'])
    })

    test(`routes ${origin.kind} to haiku @ low when configured`, async ($, on) => {
      let calls = 0
      const statuses = world(on, () => {
        calls += 1
        return { isAnswered: true, text: HAIKU, usage: {} } as never
      }, { background: 'haiku' })
      const sent = captureStep(on)
      await start($)
      await submit($, 'Background event occurred', origin)
      await step($)
      expect(sent).toEqual([{ model: 'claude-haiku-5-5', effort: 'low' }])
      expect(calls).toBe(0)
      expect(statuses).toEqual(['router → haiku · low'])
    })
  }

  test('preserves provider prefix on background routed turns', async ($, on) => {
    world(on, HAIKU, { background: 'sonnet' })
    const sent = captureStep(on)
    await start($)
    await submit($, 'task update', { kind: 'task-notification' })
    await step($, { model: 'anthropic.claude-opus-5-5' })
    expect(sent).toEqual([{ model: 'anthropic.claude-sonnet-5-5', effort: 'medium' }])
  })

  test('router off leaves background origins on session model', async ($, on) => {
    world(on, HAIKU, { background: 'sonnet', enabled: false })
    const sent = captureStep(on)
    await start($)
    await submit($, 'task update', { kind: 'task-notification' })
    await step($)
    expect(sent).toEqual([{ model: STEP.model, effort: STEP.effort }])
  })
})

describe('subagent routing', () => {
  test('routes subagent requests to configured sonnet tier', async ($, on) => {
    world(on, HAIKU, { background: 'sonnet' })
    const sent = captureStep(on)
    await start($)
    await submit($, 'run background task')
    await step($, { agentId: 'sub-worker-1' })
    expect(sent).toEqual([{ model: 'claude-sonnet-5-5', effort: 'medium' }])
  })

  test('routes subagent requests to configured haiku tier', async ($, on) => {
    world(on, HAIKU, { background: 'haiku' })
    const sent = captureStep(on)
    await start($)
    await submit($, 'run background task')
    await step($, { agentId: 'sub-worker-1' })
    expect(sent).toEqual([{ model: 'claude-haiku-5-5', effort: 'low' }])
  })

  test('main thread uses classified route while subagents use background tier', async ($, on) => {
    world(on, '{"model":"opus","effort":"high","reason":"complex architecture"}', { background: 'sonnet' })
    const sent = captureStep(on)
    await start($)
    await submit($, 'design complex architecture')
    await step($) // main thread step
    await step($, { agentId: 'sub-search-1' }) // subagent step
    expect(sent).toEqual([
      { model: 'claude-opus-5-5', effort: 'high' },
      { model: 'claude-sonnet-5-5', effort: 'medium' },
    ])
  })

  test('leaves subagent requests on session model when background is session', async ($, on) => {
    world(on, HAIKU, { background: 'session' })
    const sent = captureStep(on)
    await start($)
    await submit($, 'hi')
    await step($, { agentId: 'sub-1' })
    expect(sent).toEqual([{ model: STEP.model, effort: STEP.effort }])
  })

  test('router off leaves subagent requests on session model', async ($, on) => {
    world(on, HAIKU, { background: 'sonnet', enabled: false })
    const sent = captureStep(on)
    await start($)
    await submit($, 'hi')
    await step($, { agentId: 'sub-1' })
    expect(sent).toEqual([{ model: STEP.model, effort: STEP.effort }])
  })

  test('subagents preserve provider prefixes', async ($, on) => {
    world(on, HAIKU, { background: 'sonnet' })
    const sent = captureStep(on)
    await start($)
    await submit($, 'hi')
    await step($, { model: 'us.anthropic.claude-opus-5-5', agentId: 'sub-1' })
    expect(sent).toEqual([{ model: 'us.anthropic.claude-sonnet-5-5', effort: 'medium' }])
  })

  test('a haiku subagent stays on haiku when background is sonnet', async ($, on) => {
    world(on, HAIKU, { background: 'sonnet' })
    const sent = captureStep(on)
    await start($)
    await submit($, 'hi')
    await step($, { model: 'claude-haiku-5-5', effort: 'low', agentId: 'sub-explore-1' })
    expect(sent).toEqual([{ model: 'claude-haiku-5-5', effort: 'low' }])
  })

  test('an opus subagent goes to sonnet when background is sonnet', async ($, on) => {
    world(on, HAIKU, { background: 'sonnet' })
    const sent = captureStep(on)
    await start($)
    await submit($, 'hi')
    await step($, { model: 'claude-opus-5-5', agentId: 'sub-1' })
    expect(sent).toEqual([{ model: 'claude-sonnet-5-5', effort: 'medium' }])
  })

  test('a sonnet subagent goes to haiku when background is haiku', async ($, on) => {
    world(on, HAIKU, { background: 'haiku' })
    const sent = captureStep(on)
    await start($)
    await submit($, 'hi')
    await step($, { model: 'claude-sonnet-5-5', agentId: 'sub-1' })
    expect(sent).toEqual([{ model: 'claude-haiku-5-5', effort: 'low' }])
  })

  test('a subagent already on the background tier keeps its model and effort', async ($, on) => {
    world(on, HAIKU, { background: 'sonnet' })
    const sent = captureStep(on)
    await start($)
    await submit($, 'hi')
    await step($, { model: 'us.anthropic.claude-sonnet-5-5', effort: 'high', agentId: 'sub-1' })
    expect(sent).toEqual([{ model: 'us.anthropic.claude-sonnet-5-5', effort: 'high' }])
  })

  test('subagents leave unrecognised model ids unchanged', async ($, on) => {
    world(on, HAIKU, { background: 'sonnet' })
    const sent = captureStep(on)
    await start($)
    await submit($, 'hi')
    await step($, { model: 'custom.claude-opus-5-5', agentId: 'sub-1' })
    expect(sent).toEqual([{ model: 'custom.claude-opus-5-5', effort: STEP.effort }])
  })
})

// ---- Sticky routing: a cheaper pick stays on the warm model when switching costs more.

const OPUS = '{"model":"opus","effort":"high","reason":"hard"}'
const SONNET = '{"model":"sonnet","effort":"medium","reason":"coding"}'

// The classifier answers `replies` in order, then repeats the last.
function sequence(...replies: string[]) {
  let i = 0
  return () => ({ isAnswered: true, text: replies[Math.min(i++, replies.length - 1)], usage: {} })
}

// What the plugin's fs.read of prices.json returns.
function prices(on: On, text = PRICES_JSON) {
  on('fs.read', () => ({ value: text }) as never)
}

// Like captureStep, but every response reports `prompt` tokens of usage (mostly cache
// reads) from the model the request named; `prompt` null reports no usage. `extra`'s
// fields are added to or replace that usage's.
function captureUsage(on: On, prompt: () => number | null, extra: () => Record<string, unknown> = () => ({})) {
  const sent: { model: string; effort?: unknown }[] = []
  on('turn.step', async function* (_$, e) {
    sent.push({ model: e.model, effort: e.effort })
    const n = prompt()
    const usage =
      n === null
        ? null
        : {
            model: e.model, input_tokens: 10, cache_read_input_tokens: n - 10, cache_creation_input_tokens: 0, output_tokens: 500,
            ...extra(),
          }
    return { turnId: e.turnId, index: e.index, answer: '', toolUses: [], stopReason: 'end_turn', usage } as never
  })
  return sent
}

describe('sticky routing', () => {
  test('a Haiku pick stays on warm Opus at 100K and says why', async ($, on) => {
    world(on, sequence(OPUS, HAIKU))
    prices(on)
    const sent = captureUsage(on, () => 100_000)
    await start($)
    await submit($, 'design the migration')
    await step($)
    await submit($, 'what does ls -la do')
    await step($)
    expect(sent).toEqual([
      { model: 'claude-opus-5-5', effort: 'high' },
      { model: 'claude-opus-5-5', effort: 'low' },
    ])
    const text = (await router($)).text
    expect(text).toContain('Sticky: on')
    expect(text).toContain('Last route: opus · low — trivial (kept on opus: switch to haiku would cost more, ~$0.51 vs ~$0.14 saved)')
    expect(text).toContain('This session: haiku 0, sonnet 0, opus 2')
  })

  test('the session model\'s own turn (a slash command) counts as warm', async ($, on) => {
    world(on, HAIKU)
    prices(on)
    const sent = captureUsage(on, () => 60_000)
    await start($)
    await submit($, '/review')
    await step($) // session model, claude-opus-5-5
    await submit($, 'what does ls -la do')
    await step($)
    expect(sent[1]).toEqual({ model: 'claude-opus-5-5', effort: 'low' })
  })

  test('Sonnet pick stays on warm Opus; Haiku pick stays on warm Sonnet', async ($, on) => {
    world(on, sequence(OPUS, SONNET, SONNET, HAIKU))
    prices(on)
    const sent = captureUsage(on, () => 50_000)
    await start($)
    await submit($, 'design it')
    await step($)
    await submit($, 'write it')
    await step($)
    expect(sent[1]).toEqual({ model: 'claude-opus-5-5', effort: 'medium' })
    await router($, 'sticky off')
    await submit($, 'write it') // switches to Sonnet with sticky off
    await step($)
    await router($, 'sticky on')
    await submit($, 'rename it')
    await step($)
    expect(sent.slice(2)).toEqual([
      { model: 'claude-sonnet-5-5', effort: 'medium' },
      { model: 'claude-sonnet-5-5', effort: 'low' },
    ])
  })

  test('an escalation is never held back', async ($, on) => {
    world(on, sequence(HAIKU, OPUS))
    prices(on)
    const sent = captureUsage(on, () => 150_000)
    await start($)
    await submit($, 'hi')
    await step($, { model: 'claude-haiku-5-5' })
    await submit($, 'design the migration')
    await step($, { model: 'claude-haiku-5-5' })
    expect(sent[1]).toEqual({ model: 'claude-opus-5-5', effort: 'high' })
    expect((await router($)).text).not.toContain('kept on')
  })

  test('a small context still switches down', async ($, on) => {
    world(on, sequence(OPUS, HAIKU))
    prices(on)
    const sent = captureUsage(on, () => 2_000)
    await start($)
    await submit($, 'design it')
    await step($)
    await submit($, 'hi')
    await step($)
    expect(sent[1]).toEqual({ model: 'claude-haiku-5-5', effort: 'low' })
  })

  test('the warm model is the last request\'s, so a long turn on Opus is warm at its end', async ($, on) => {
    world(on, sequence(OPUS, HAIKU))
    prices(on)
    let n = 0
    const sent = captureUsage(on, () => (n += 1) * 40_000)
    await start($)
    await submit($, 'design it')
    await step($)
    await step($, { index: 1 })
    await submit($, 'hi')
    await step($)
    expect(sent[2]).toEqual({ model: 'claude-opus-5-5', effort: 'low' })
  })

  test('a cache idle past five minutes is cold: the pick switches', async ($, on) => {
    world(on, sequence(OPUS, HAIKU, OPUS, HAIKU))
    prices(on)
    const clock = mock.clock(on, { now: 1_000_000 })
    const sent = captureUsage(on, () => 100_000)
    await start($)
    await submit($, 'design it')
    await step($)
    await clock.advance(4 * 60_000)
    await submit($, 'hi')
    await step($)
    expect(sent[1]).toEqual({ model: 'claude-opus-5-5', effort: 'low' })
    await submit($, 'design more')
    await step($)
    await clock.advance(5 * 60_000 + 1)
    await submit($, 'hi')
    await step($)
    expect(sent[3]).toEqual({ model: 'claude-haiku-5-5', effort: 'low' })
  })

  // The pick 20 or 70 minutes after an Opus request, given how that request wrote cache.
  for (const [writes, minutes, want] of [
    ['1h', 20, 'claude-opus-5-5'],
    ['5m', 20, 'claude-haiku-5-5'],
    ['1h', 70, 'claude-haiku-5-5'],
  ] as const) {
    test(`a ${writes} cache writer ${minutes} minutes later: the pick ${want.includes('opus') ? 'is kept' : 'switches'}`, async ($, on) => {
      world(on, sequence(OPUS, HAIKU))
      prices(on)
      const clock = mock.clock(on, { now: 1_000_000 })
      const split = writes === '1h'
        ? { ephemeral_1h_input_tokens: 2_000, ephemeral_5m_input_tokens: 0 }
        : { ephemeral_1h_input_tokens: 0, ephemeral_5m_input_tokens: 2_000 }
      const sent = captureUsage(on, () => 100_000, () => ({ cache_creation: split }))
      await start($)
      await submit($, 'design it')
      await step($)
      await clock.advance(minutes * 60_000)
      await submit($, 'hi')
      await step($)
      expect(sent[1]).toEqual({ model: want, effort: 'low' })
    })
  }

  test('a warm cache read after five minutes on the same model shows 1-hour cache', async ($, on) => {
    world(on, sequence(OPUS, OPUS, HAIKU))
    prices(on)
    const clock = mock.clock(on, { now: 1_000_000 })
    const sent = captureUsage(on, () => 100_000) // no raw split: only the reads tell
    await start($)
    await submit($, 'design it')
    await step($)
    await clock.advance(20 * 60_000)
    await submit($, 'design more') // reads ~100K of Opus cache 20 minutes on
    await step($)
    await clock.advance(30 * 60_000)
    await submit($, 'hi')
    await step($)
    expect(sent[2]).toEqual({ model: 'claude-opus-5-5', effort: 'low' })
  })

  test('a cold read after five minutes leaves the cache at 5 minutes', async ($, on) => {
    world(on, sequence(OPUS, OPUS, HAIKU))
    prices(on)
    const clock = mock.clock(on, { now: 1_000_000 })
    let n = 0
    // the second request reads only the ~35K shared prefix of its 100K prompt
    const sent = captureUsage(on, () => 100_000, () =>
      (n += 1) === 2 ? { cache_read_input_tokens: 35_000, cache_creation_input_tokens: 64_990 } : {},
    )
    await start($)
    await submit($, 'design it')
    await step($)
    await clock.advance(20 * 60_000)
    await submit($, 'design more')
    await step($)
    await clock.advance(30 * 60_000)
    await submit($, 'hi')
    await step($)
    expect(sent[2]).toEqual({ model: 'claude-haiku-5-5', effort: 'low' })
  })

  test('/router sticky off switches on every pick; the setting is stored', async ($, on) => {
    world(on, sequence(OPUS, HAIKU))
    prices(on)
    const sent = captureUsage(on, () => 100_000)
    await start($)
    expect((await router($, 'sticky off')).text).toBe('Sticky: off (every pick switches models, whatever the cache)')
    await submit($, 'design it')
    await step($)
    await submit($, 'hi')
    await step($)
    expect(sent[1]).toEqual({ model: 'claude-haiku-5-5', effort: 'low' })
    expect((await router($)).text).toContain('Sticky: off')
  })

  test('a sticky setting of off saved by an earlier session is honoured', async ($, on) => {
    world(on, sequence(OPUS, HAIKU), { sticky: false })
    prices(on)
    const sent = captureUsage(on, () => 100_000)
    await start($)
    await submit($, 'design it')
    await step($)
    await submit($, 'hi')
    await step($)
    expect(sent[1]).toEqual({ model: 'claude-haiku-5-5', effort: 'low' })
  })

  test('/router sticky reports the setting; a bad value is refused', async ($, on) => {
    world(on, HAIKU)
    await start($)
    expect((await router($, 'sticky')).text).toBe(
      'Sticky: on (a cheaper pick stays on the warm model when switching would cost more than it saves)',
    )
    expect((await router($, 'sticky maybe')).text).toContain('Usage: /router')
    expect((await router($, 'STICKY OFF')).text).toContain('Sticky: off')
  })

  test('an unusable prices.json: the pick switches', async ($, on) => {
    world(on, sequence(OPUS, HAIKU))
    prices(on, 'not json')
    const sent = captureUsage(on, () => 100_000)
    await start($)
    await submit($, 'design it')
    await step($)
    await submit($, 'hi')
    await step($)
    expect(sent[1]).toEqual({ model: 'claude-haiku-5-5', effort: 'low' })
  })

  test('a response with no usage leaves nothing warm', async ($, on) => {
    world(on, sequence(OPUS, HAIKU))
    prices(on)
    const sent = captureUsage(on, () => null)
    await start($)
    await submit($, 'design it')
    await step($)
    await submit($, 'hi')
    await step($)
    expect(sent[1]).toEqual({ model: 'claude-haiku-5-5', effort: 'low' })
  })

  test('a main-thread compaction leaves nothing warm; a subagent\'s does not', async ($, on) => {
    world(on, sequence(OPUS, HAIKU, OPUS, HAIKU))
    prices(on)
    const summary = [{ role: 'user' as const, text: 'summary', toolUses: [] }]
    on('session.compact', () => ({ messages: summary }) as never)
    const sent = captureUsage(on, () => 100_000)
    await start($)
    await submit($, 'design it')
    await step($)
    await $.session.compact({ trigger: 'auto', messages: summary, agentId: 'sub-1' })
    await submit($, 'hi')
    await step($)
    expect(sent[1]).toEqual({ model: 'claude-opus-5-5', effort: 'low' })
    await submit($, 'design more')
    await step($)
    await $.session.compact({ trigger: 'manual', messages: summary })
    await submit($, 'hi')
    await step($)
    expect(sent[3]).toEqual({ model: 'claude-haiku-5-5', effort: 'low' })
  })

  test('subagent requests do not set the warm model', async ($, on) => {
    world(on, sequence(SONNET, HAIKU))
    prices(on)
    const sent = captureUsage(on, () => 100_000)
    await start($)
    await submit($, 'write it')
    await step($)
    await step($, { agentId: 'sub-1', model: 'claude-opus-5-5' })
    await submit($, 'hi')
    await step($)
    expect(sent[2]).toEqual({ model: 'claude-sonnet-5-5', effort: 'low' })
  })

  test('the floor applies first: a Haiku pick floored to Sonnet still stays on warm Opus', async ($, on) => {
    world(on, sequence(OPUS, HAIKU), { floor: 'sonnet' })
    prices(on)
    const sent = captureUsage(on, () => 100_000)
    await start($)
    await submit($, 'design it')
    await step($)
    await submit($, 'hi')
    await step($)
    expect(sent[1]).toEqual({ model: 'claude-opus-5-5', effort: 'low' })
    const text = (await router($)).text
    expect(text).toContain('(raised from haiku by the floor) (kept on opus: switch to sonnet would cost more')
  })

  test('a warm model above the ceiling is never kept', async ($, on) => {
    world(on, sequence(OPUS, HAIKU))
    prices(on)
    const sent = captureUsage(on, () => 100_000)
    await start($)
    await submit($, 'design it')
    await step($)
    await router($, 'ceiling sonnet')
    await submit($, 'hi')
    await step($)
    expect(sent[1]).toEqual({ model: 'claude-haiku-5-5', effort: 'low' })
  })

  test('a capped route can still stay on a warm model at or below the ceiling', async ($, on) => {
    world(on, sequence(OPUS, HAIKU), { ceiling: 'sonnet' })
    prices(on)
    const sent = captureUsage(on, () => 100_000)
    await start($)
    await submit($, 'design it')
    await step($)
    await submit($, 'hi')
    await step($)
    expect(sent).toEqual([
      { model: 'claude-sonnet-5-5', effort: 'high' },
      { model: 'claude-sonnet-5-5', effort: 'low' },
    ])
    expect((await router($)).text).toContain('(kept on sonnet: switch to haiku would cost more')
  })

  test('background-routed and router-off turns are not held back', async ($, on) => {
    world(on, OPUS, { background: 'haiku' })
    prices(on)
    const sent = captureUsage(on, () => 100_000)
    await start($)
    await submit($, 'design it')
    await step($)
    await submit($, 'task update', { kind: 'task-notification' })
    await step($)
    expect(sent[1]).toEqual({ model: 'claude-haiku-5-5', effort: 'low' })
  })
})

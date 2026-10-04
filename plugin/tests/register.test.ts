import { describe, expect, mock, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { On } from 'claude-code'

import { failure, liveFields, steerNote, summarize } from '../hooks/register'

const SID = '0b4e7c1a-9d2f-4e55-8a3b-1c2d3e4f5a6b'
const OTHER_SID = '9f8e7d6c-5b4a-4321-8fed-cba987654321'
const HOME = '/home/u'
const DIR = `${HOME}/.local/state/bettercallgpt/${SID}`
const NOW_MS = 1_790_000_000_000
// A live call: its heartbeat refreshed `at` 5 s ago.
const LIVE = {
  phase: 'running',
  relay: 'qualified',
  ended: {},
  pid: 4242,
  at: NOW_MS / 1000 - 5,
  instance: 'call-1',
}
// What the engine beneath answers; the plugin must hand it back exactly.
const BELOW = { stopReason: 'from below' }
const PROMPT = { tool_name: 'Bash', tool_input: { command: 'touch  hello.txt\n' }, session_id: SID }
// What another mod, later in the chain, draws in the shared band.
const OTHER_BAND = 'another mod draws here'

type Write = { path: string; text: string }
type World = {
  files: Map<string, string>
  env?: Record<string, string>
  session?: { id: string }
  gate?: { held: Promise<void> | undefined }
  isWriteDenied?: boolean
}

/** The world beneath the plugin: a file system in memory, the session, env and clock. */
function world(on: On, { files, env = { HOME }, session = { id: SID }, gate, isWriteDenied }: World) {
  const writes: Write[] = []
  const reads: string[] = []
  const below: unknown[] = []
  on('fs.exists', ($, e) => ({ value: files.has(e.path) }))
  on('fs.read', async ($, e) => {
    reads.push(e.path)
    if (gate?.held !== undefined) await gate.held // a slow disk, released by the test
    const text = files.get(e.path)
    return text === undefined ? { deny: `ENOENT: ${e.path}` } : { value: text }
  })
  on('fs.write', ($, e) => {
    if (isWriteDenied) return { deny: 'EACCES' }
    writes.push({ path: e.path, text: e.text })
    files.set(e.path, e.text)
    return { value: undefined }
  })
  const toasts: string[] = []
  const store = new Map<string, unknown>()
  on('ui.toast', ($, e) => {
    toasts.push(e.text)
    return { value: undefined }
  })
  on('store.get', ($, e) => ({ value: store.get(e.key) }))
  on('store.set', ($, e) => {
    store.set(e.key, e.value)
    return { value: undefined }
  })
  on('session.id', () => ({ value: session.id }))
  on('command.register', ($, e) => ({ value: { command: e.name } }))
  on('turn.start', ($, e) => ({ turnId: e.turnId }))
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('classic.PermissionRequest', ($, e) => {
    below.push(e)
    return BELOW
  })
  on('ui.render', { component: 'AbovePrompt' }, ($, e) => {
    const { Box, Text } = $.ui.resolve(e)
    return h(Box, { key: 'other' }, h(Text, null, OTHER_BAND)) as ReturnType<typeof Box>
  })
  mock.env(on, env)
  const clock = mock.clock(on, { now: NOW_MS })
  return { writes, reads, below, clock, toasts, store }
}

const status = (value: object) => new Map([[`${DIR}/status.json`, JSON.stringify(value)]])

describe('classic.PermissionRequest', () => {
  test('passes the request through untouched and answers nothing itself', async ($: Engine, on: On) => {
    const { below } = world(on, { files: status(LIVE) })
    const result = await $.classic.PermissionRequest(PROMPT)
    expect(result).toEqual(BELOW)
    expect('decision' in result).toBe(false)
    expect(below).toHaveLength(1)
    expect(below[0]).toMatchObject(PROMPT)
  })

  test('writes nothing when no call is live', async ($: Engine, on: On) => {
    const files = new Map<string, string>()
    const { writes } = world(on, { files })
    for (const value of [
      undefined,
      { ...LIVE, phase: 'ended', ended: { reason: 'stopped by control' } },
      { ...LIVE, relay: 'probing' },
      { ...LIVE, ended: { reason: 'stream ended' } },
      { ...LIVE, at: NOW_MS / 1000 - 31 }, // a heartbeat that stopped: a dead writer
      { ...LIVE, at: NOW_MS / 1000 + 6 }, // from the future, past any clock skew
      { ...LIVE, at: '1790000000' },
      { ...LIVE, at: undefined },
    ]) {
      files.clear()
      if (value !== undefined) files.set(`${DIR}/status.json`, JSON.stringify(value))
      expect(await $.classic.PermissionRequest(PROMPT)).toEqual(BELOW)
      expect(writes).toEqual([])
    }
  })

  test('writes permission.json into the call directory when the call is live', async ($: Engine, on: On) => {
    const files = status({ ...LIVE, at: NOW_MS / 1000 + 4 }) // a little skew is still live
    const { writes } = world(on, { files })
    expect(await $.classic.PermissionRequest(PROMPT)).toEqual(BELOW)
    expect(writes).toHaveLength(1)
    expect(writes[0]?.path).toBe(`${DIR}/permission.json`)
    expect(JSON.parse(writes[0]?.text ?? '')).toEqual({
      at: NOW_MS / 1000,
      tool: 'Bash',
      summary: 'touch hello.txt',
      instance: 'call-1',
    })
  })

  test('a write that is refused still hands the request on', async ($: Engine, on: On) => {
    const { writes, below } = world(on, { files: status(LIVE), isWriteDenied: true })
    expect(await $.classic.PermissionRequest(PROMPT)).toEqual(BELOW)
    expect(writes).toEqual([])
    expect(below).toHaveLength(1)
  })

  // The launcher's order (bettercallgpt/cli.py user_state_dir + apply_defaults): an explicit
  // VOICE_LISTEN_STATE_DIR, else $XDG_STATE_HOME/bettercallgpt, else ~/.local/state/bettercallgpt.
  const EXPLICIT = `/s/${SID}`
  const XDG = `/x/bettercallgpt/${SID}`
  const everywhere = () =>
    new Map([EXPLICIT, XDG, DIR].map(dir => [`${dir}/status.json`, JSON.stringify(LIVE)]))

  test('XDG_STATE_HOME without VOICE_LISTEN_STATE_DIR: $XDG_STATE_HOME/bettercallgpt', async ($: Engine, on: On) => {
    const { writes, reads } = world(on, { files: everywhere(), env: { HOME, XDG_STATE_HOME: '/x' } })
    await $.classic.PermissionRequest(PROMPT)
    expect(reads).toEqual([`${XDG}/status.json`])
    expect(writes.map(w => w.path)).toEqual([`${XDG}/permission.json`])
  })

  test('VOICE_LISTEN_STATE_DIR wins over XDG_STATE_HOME', async ($: Engine, on: On) => {
    const env = { HOME, XDG_STATE_HOME: '/x', VOICE_LISTEN_STATE_DIR: '/s' }
    const { writes, reads } = world(on, { files: everywhere(), env })
    await $.classic.PermissionRequest(PROMPT)
    expect(reads).toEqual([`${EXPLICIT}/status.json`])
    expect(writes.map(w => w.path)).toEqual([`${EXPLICIT}/permission.json`])
  })

  test('neither set: HOME/.local/state/bettercallgpt', async ($: Engine, on: On) => {
    const { writes, reads } = world(on, { files: everywhere() })
    await $.classic.PermissionRequest(PROMPT)
    expect(reads).toEqual([`${DIR}/status.json`])
    expect(writes.map(w => w.path)).toEqual([`${DIR}/permission.json`])
  })

  test('a session id that is not a plain name is never used as a path', async ($: Engine, on: On) => {
    const { writes } = world(on, { files: status(LIVE) })
    expect(await $.classic.PermissionRequest({ ...PROMPT, session_id: '../etc' })).toEqual(BELOW)
    expect(writes).toEqual([])
  })

  test('an unreadable status leaves the request alone', async ($: Engine, on: On) => {
    const { writes } = world(on, { files: new Map([[`${DIR}/status.json`, '{"phase": "runn']]) })
    expect(await $.classic.PermissionRequest(PROMPT)).toEqual(BELOW)
    expect(writes).toEqual([])
  })
})

describe('summarize', () => {
  test('names the command, the file or the MCP tool on one line, never cut', () => {
    expect(summarize('Bash', { command: 'git status\n  && ls' })).toBe('git status && ls')
    expect(summarize('Edit', { file_path: '/repo/a.ts', old_string: 'x' })).toBe('/repo/a.ts')
    expect(summarize('NotebookEdit', { notebook_path: '/n.ipynb' })).toBe('/n.ipynb')
    expect(summarize('mcp__github__create_issue', { title: 't' })).toBe('mcp__github__create_issue')
    expect(summarize('WebFetch', { url: 'https://example.com' })).toBe('')
    // Whole up to 2000 characters, so a credential is never split by a cut...
    const long = `curl -H "Authorization: Bearer ${'k'.repeat(40)}" ${'x'.repeat(1900)}`
    expect(summarize('Bash', { command: long })).toBe(long)
    // ...and past that, nothing but the tool's name is written.
    expect(summarize('Bash', { command: 'x'.repeat(2001) })).toBe('')
  })
})

describe('the call console', () => {
  const PROPS = {
    hasSurvey: false,
    isWorking: false,
    maxRows: 10,
    bodyColumns: 100,
    scroll: { offset: 0, bodyRows: 10 },
    view: {},
  }
  const BAND_SITE = { plugin: 'bettercallgpt', component: 'AbovePrompt', props: PROPS } as const
  const start = ($: Engine) => $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })
  const BIN = '/home/u/.local/bin/bettercallgpt'

  type Spawned = { argv: readonly string[]; env?: Record<string, string> }
  /** The host's processes: `command -v`, the launcher's stop and steer, and the voice child,
   * which runs until the test ends it (or exits at once with `exit`). */
  function processes(on: On, files: Map<string, string>, options: { exit?: { code: number; stderr: string }; installed?: boolean } = {}) {
    const runs: string[][] = []
    const spawned: Spawned[] = []
    const aborted: string[] = []
    let end: (() => void) | undefined
    on('process.run', ($, e) => {
      runs.push([...e.argv])
      if (e.argv[0] === '/bin/sh') {
        const isInstalled = options.installed ?? true
        return { value: { exitCode: isInstalled ? 0 : 1, stdout: isInstalled ? `${BIN}\n` : '', stderr: '' } }
      }
      if (e.argv.at(-1) === 'stop') end?.()
      return { value: { exitCode: 0, stdout: '{}', stderr: '' } }
    })
    on('process.spawn', async function* ($, e) {
      spawned.push({ argv: e.argv, env: e.env })
      if (options.exit !== undefined) {
        yield { stream: 'stderr' as const, text: options.exit.stderr }
        return { value: { code: options.exit.code, signal: null } }
      }
      files.set(`${DIR}/status.json`, JSON.stringify(LIVE))
      await new Promise<void>(resolve => {
        end = resolve
      })
      files.delete(`${DIR}/status.json`)
      return { value: { code: 0, signal: null } }
    })
    on('turn.abort', ($, e) => {
      aborted.push(e.turnId)
      return { value: undefined }
    })
    return { runs, spawned, aborted, end: () => end?.() }
  }

  test('no call: a Call button under what other mods draw, on terminal and desktop', async ($: Engine, on: On) => {
    world(on, { files: new Map() })
    processes(on, new Map())
    await start($)
    for (const surface of ['terminal', 'desktop'] as const) {
      const ui = await $.ui.mount({ ...BAND_SITE, surface })
      expect(await ui.find({ type: 'Button', key: 'call' })).toBeDefined()
      expect(await ui.find({ type: 'Button', key: 'hangup' })).toBeUndefined()
      expect(await ui.find({ text: OTHER_BAND })).toBeDefined() // composed, not replaced
      await ui.unmount()
    }
  })

  test('Call starts the voice process as a child with a fresh nonce, and no model turn', async ($: Engine, on: On) => {
    const files = new Map<string, string>()
    const { clock } = world(on, { files })
    const { spawned, runs, end } = processes(on, files)
    await start($)
    const ui = await $.ui.mount({ ...BAND_SITE, surface: 'terminal' })
    await ui.press({ key: 'call' })
    await clock.settle()
    expect(spawned).toHaveLength(1)
    const { argv, env } = spawned[0] as Spawned
    const nonce = env?.NONCE ?? ''
    expect(nonce).toMatch(/^mod-[a-z0-9]+-[a-z0-9]+$/)
    expect(argv).toEqual([BIN, '--session', SID, '--nonce', nonce, '--mod', 'start'])
    expect((await ui.find({ type: 'Text', text: ' ◌ CONNECTING ' }))?.props.backgroundColor).toBe('yellow')

    await clock.advance(500) // status.json is live now
    expect(await ui.find({ type: 'Button', key: 'steer' })).toBeDefined()
    expect((await ui.find({ type: 'Button', key: 'hangup' }))?.props.label).toBe('📴 Hang up')

    await ui.press({ key: 'call' }).catch(() => undefined) // no Call button on a call
    expect(spawned).toHaveLength(1)

    await ui.press({ key: 'hangup' })
    await clock.settle()
    expect(runs).toContainEqual([BIN, '--session', SID, 'stop'])
    expect(await ui.find({ type: 'Button', key: 'call' })).toBeDefined()
  })

  test('without an installed bettercallgpt the pinned release runs through uvx', async ($: Engine, on: On) => {
    const files = new Map<string, string>()
    const { clock } = world(on, { files })
    const { spawned, end } = processes(on, files, { installed: false })
    await start($)
    const ui = await $.ui.mount({ ...BAND_SITE, surface: 'terminal' })
    await ui.press({ key: 'call' })
    await clock.settle()
    expect(spawned[0]?.argv.slice(0, 4)).toEqual([
      'uvx',
      '--from',
      'git+https://github.com/insta-fusion/bettercallgpt@v0.2.0',
      'bettercallgpt',
    ])
    end()
    await clock.settle()
  })

  test('a start that fails says why in the band and offers Call again', async ($: Engine, on: On) => {
    const files = new Map<string, string>()
    const { clock } = world(on, { files })
    processes(on, files, { exit: { code: 1, stderr: 'voice: audio-device busy (another voice surface is active)\n' } })
    await start($)
    const ui = await $.ui.mount({ ...BAND_SITE, surface: 'terminal' })
    await ui.press({ key: 'call' })
    await clock.settle()
    expect(await ui.find({ type: 'Button', key: 'call' })).toBeDefined()
    expect((await ui.find({ type: 'Text', text: ' ✕ CALL FAILED ' }))?.props.backgroundColor).toBe('red')
    expect(await ui.find({ text: 'audio-device busy (another voice surface is active)' })).toBeDefined()
    expect((await ui.find({ type: 'Button', key: 'call' }))?.props.label).toBe('📞 Call again')
  })

  test('a call started by /bettercallgpt:on shows the same console and goes when its heartbeat stops', async ($: Engine, on: On) => {
    const files = status(LIVE)
    const { clock } = world(on, { files })
    processes(on, files)
    await start($)
    const ui = await $.ui.mount({ ...BAND_SITE, surface: 'terminal' })
    expect(await ui.find({ type: 'Button', key: 'hangup' })).toBeDefined()
    await clock.advance(26_000) // `at` is now 31 s old: the voice process stopped writing
    expect(await ui.find({ type: 'Button', key: 'call' })).toBeDefined()
  })

  test('shows what was heard and not sent, and what waits for Claude', async ($: Engine, on: On) => {
    const files = status({ ...LIVE, unsent: { chars: 6, preview: '先跑一下测试' }, queued: ['req-3', 'req-4'] })
    const { clock } = world(on, { files })
    processes(on, files)
    await start($)
    const ui = await $.ui.mount({ ...BAND_SITE, surface: 'terminal' })
    await clock.advance(500)
    expect((await ui.find({ type: 'Text', text: ' 🎙 LIVE ' }))?.props.backgroundColor).toBe('green')
    expect((await ui.find({ type: 'Text', text: '«先跑一下测试» not sent' }))?.props.color).toBe('yellow')
    expect((await ui.find({ type: 'Text', text: '2 queued' }))?.props.color).toBe('cyan')
    expect((await ui.find({ type: 'Button', key: 'steer' }))?.props.variant).toBe('primary')
  })

  test('Steer tells the voice process to send what it heard, and ends no turn by itself', async ($: Engine, on: On) => {
    const files = status({ ...LIVE, unsent: { chars: 3, preview: '停一下' }, queued: [] })
    const { clock } = world(on, { files })
    const { runs, aborted } = processes(on, files)
    await start($)
    const ui = await $.ui.mount({ ...BAND_SITE, surface: 'terminal' })
    await ui.press({ key: 'steer' })
    await clock.settle()
    expect(runs).toContainEqual([BIN, '--session', SID, 'steer'])
    expect(aborted).toEqual([])
  })

  test('Steer with a message waiting behind a running turn ends that turn, once', async ($: Engine, on: On) => {
    const files = status({ ...LIVE, queued: ['req-7'] })
    const { clock } = world(on, { files })
    const { runs, aborted } = processes(on, files)
    await start($)
    const ui = await $.ui.mount({ ...BAND_SITE, surface: 'terminal' })
    const { turnId } = await $.turn.start({ text: 'a long task', turnId: 'turn-1' })
    await ui.press({ key: 'steer' })
    await clock.settle()
    expect(aborted).toEqual([turnId])
    await clock.advance(2000) // still queued on later ticks: no second abort
    expect(aborted).toEqual([turnId])
  })

  test('Steer with nothing unsent and nothing waiting does nothing', async ($: Engine, on: On) => {
    const files = status(LIVE)
    const { clock } = world(on, { files })
    const { runs, aborted } = processes(on, files)
    await start($)
    const ui = await $.ui.mount({ ...BAND_SITE, surface: 'terminal' })
    await $.turn.start({ text: 'a long task', turnId: 'turn-1' })
    await ui.press({ key: 'steer' })
    await clock.settle()
    expect(aborted).toEqual([])
    expect(runs).toContainEqual([BIN, '--session', SID, 'steer']) // the voice process decides what is unsent
  })

  test('with no turn running, a waiting message needs no Steer and none is kept for later', async ($: Engine, on: On) => {
    const files = status({ ...LIVE, unsent: { chars: 2, preview: '你好' }, queued: [] })
    const { clock } = world(on, { files })
    const { aborted } = processes(on, files)
    await start($)
    const ui = await $.ui.mount({ ...BAND_SITE, surface: 'terminal' })
    await ui.press({ key: 'steer' })
    await clock.settle()
    await $.turn.start({ text: 'a later task', turnId: 'turn-2' })
    files.set(`${DIR}/status.json`, JSON.stringify({ ...LIVE, queued: ['req-9'] }))
    await clock.advance(1000)
    expect(aborted).toEqual([]) // the earlier press does not end a later turn
  })

  test('yields to a survey', async ($: Engine, on: On) => {
    world(on, { files: status(LIVE) })
    processes(on, new Map())
    await start($)
    const ui = await $.ui.mount({ ...BAND_SITE, surface: 'terminal', props: { ...PROPS, hasSurvey: true } })
    expect(await ui.find({ type: 'Button' })).toBeUndefined()
  })
})

describe('reading the voice process', () => {
  test('liveFields takes the preview and the queue length, and nothing malformed', () => {
    expect(liveFields({ unsent: { chars: 3, preview: 'abc' }, queued: ['a', 'b'] })).toEqual({ unsent: 'abc', queued: 2 })
    expect(liveFields({})).toEqual({ unsent: '', queued: 0 })
    expect(liveFields({ unsent: 'x', queued: 'y' })).toEqual({ unsent: '', queued: 0 })
  })

  test('failure gives the last line, short, and a plain hint for a session with no message yet', () => {
    expect(failure('a\nvoice: claude_code: bind_no_transcript — no transcript .jsonl\n', 1)).toBe(
      'send Claude one message first, then call again',
    )
    expect(failure('', 3)).toBe('the voice process ended (exit 3)')
    expect(failure(`voice: ${'x'.repeat(300)}`, 1)).toHaveLength(160)
  })

  test('steerNote says what the voice process did with a Steer', () => {
    expect(steerNote('sent')).toBe('')
    expect(steerNote('nothing_unsent')).toBe('')
    expect(steerNote('refused:owner_lost')).toBe('steer did not send (refused:owner_lost)')
    expect(steerNote(undefined)).toBe('')
  })

  test('a Steer answer is said once, in passing', async ($: Engine, on: On) => {
    const files = status({ ...LIVE, steer: { id: 'a1', result: 'sent' } })
    const { clock, toasts } = world(on, { files })
    await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })
    const ui = await $.ui.mount({
      plugin: 'bettercallgpt',
      component: 'AbovePrompt',
      surface: 'terminal',
      props: { hasSurvey: false, isWorking: false, maxRows: 10, bodyColumns: 100, scroll: { offset: 0, bodyRows: 10 }, view: {} },
    })
    await clock.advance(500)
    await clock.advance(2000)
    expect(toasts).toEqual(['Steer: sent what you said'])
    expect(await ui.find({ type: 'Text', text: 'listening' })).toBeDefined()
  })

  test('/call-icons chooses the symbols and keeps the choice', async ($: Engine, on: On) => {
    const { store } = world(on, { files: new Map() })
    await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })
    const ui = await $.ui.mount({
      plugin: 'bettercallgpt',
      component: 'AbovePrompt',
      surface: 'terminal',
      props: { hasSurvey: false, isWorking: false, maxRows: 10, bodyColumns: 100, scroll: { offset: 0, bodyRows: 10 }, view: {} },
    })
    expect((await ui.find({ type: 'Button', key: 'call' }))?.props.label).toBe('📞 Call')
    await $.command.run({ command: 'call-icons', args: 'none' })
    expect((await ui.find({ type: 'Button', key: 'call' }))?.props.label).toBe('Call')
    expect(store.get('icons')).toBe('none')
    await $.command.run({ command: 'call-icons', args: 'nerd' })
    expect((await ui.find({ type: 'Button', key: 'call' }))?.props.label).toBe('Call')
    expect((await ui.find({ type: 'Text', text: '\uf095' }))?.props.color).toBe('green') // the handset, tinted
    await $.command.run({ command: 'call-icons', args: 'none' })
    await $.command.run({ command: 'call-icons', args: 'sparkles' })
    expect(store.get('icons')).toBe('none')
  })
})

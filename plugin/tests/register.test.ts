import { describe, expect, mock, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { On } from 'claude-code'

import { BAND, summarize } from '../hooks/register'

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
  on('session.id', () => ({ value: session.id }))
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
  return { writes, reads, below, clock }
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

describe('the band above the prompt', () => {
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

  test('shows only while this session is on a call, under what other mods draw', async ($: Engine, on: On) => {
    const files = status({ ...LIVE, relay: 'probing' })
    const { clock } = world(on, { files })
    await start($)
    for (const surface of ['terminal', 'desktop'] as const) {
      const ui = await $.ui.mount({ ...BAND_SITE, surface })
      expect(await ui.find({ text: BAND })).toBeUndefined()
      expect(await ui.find({ text: OTHER_BAND })).toBeDefined()
      await ui.unmount()
    }

    files.set(`${DIR}/status.json`, JSON.stringify(LIVE))
    await clock.advance(2000)
    for (const surface of ['terminal', 'desktop'] as const) {
      const ui = await $.ui.mount({ ...BAND_SITE, surface })
      expect((await ui.find({ type: 'Text', text: BAND }))?.props.dimColor).toBe(true)
      expect(await ui.find({ text: OTHER_BAND })).toBeDefined() // composed, not replaced
      await ui.unmount()
    }
  })

  test('a mounted band redraws when the call starts and ends', async ($: Engine, on: On) => {
    const files = new Map<string, string>()
    const { clock } = world(on, { files })
    await start($)
    const ui = await $.ui.mount({ ...BAND_SITE, surface: 'terminal' })
    expect(await ui.find({ text: BAND })).toBeUndefined()

    files.set(`${DIR}/status.json`, JSON.stringify(LIVE))
    await clock.advance(2000)
    expect(await ui.find({ text: BAND })).toBeDefined()

    files.set(`${DIR}/status.json`, JSON.stringify({ ...LIVE, phase: 'ended', ended: { reason: 'x' } }))
    await clock.advance(2000)
    expect(await ui.find({ text: BAND })).toBeUndefined()
    await ui.unmount()
  })

  test('goes away when the heartbeat stops or the status turns unreadable', async ($: Engine, on: On) => {
    const files = status(LIVE)
    const { clock } = world(on, { files })
    await start($)
    const ui = await $.ui.mount({ ...BAND_SITE, surface: 'terminal' })
    expect(await ui.find({ text: BAND })).toBeDefined()

    await clock.advance(26_000) // `at` is now 31 s old: the voice process stopped writing
    expect(await ui.find({ text: BAND })).toBeUndefined()

    files.set(`${DIR}/status.json`, JSON.stringify({ ...LIVE, at: (NOW_MS + 26_000) / 1000 }))
    await clock.advance(2000)
    expect(await ui.find({ text: BAND })).toBeDefined()

    files.set(`${DIR}/status.json`, '{"phase": "runn') // malformed: no call, not the last answer
    await clock.advance(2000)
    expect(await ui.find({ text: BAND })).toBeUndefined()
    await ui.unmount()
  })

  test('follows the session after /clear', async ($: Engine, on: On) => {
    const session = { id: SID }
    const { clock } = world(on, { files: status(LIVE), session })
    await start($)
    const ui = await $.ui.mount({ ...BAND_SITE, surface: 'terminal' })
    expect(await ui.find({ text: BAND })).toBeDefined()

    session.id = OTHER_SID // /clear: a new session id, and no call bound to it
    await clock.advance(2000)
    expect(await ui.find({ text: BAND })).toBeUndefined()
    await ui.unmount()
  })

  test('one read at a time, and a read for a session left behind is dropped', async ($: Engine, on: On) => {
    const session = { id: SID }
    const files = new Map<string, string>()
    const gate: { held: Promise<void> | undefined } = { held: undefined }
    const { clock, reads } = world(on, { files, session, gate })
    await start($) // no call yet
    const ui = await $.ui.mount({ ...BAND_SITE, surface: 'terminal' })
    expect(await ui.find({ text: BAND })).toBeUndefined()

    // SID's call goes live, but the next read of its status is slow.
    files.set(`${DIR}/status.json`, JSON.stringify(LIVE))
    let release = () => {}
    gate.held = new Promise(resolve => (release = resolve))
    await clock.advance(2000) // the read starts for SID and waits on the disk
    const started = reads.length
    expect(started).toBe(1)
    await clock.advance(2000) // ticks while it is out start no second read
    await clock.advance(2000)
    expect(reads.length).toBe(started)

    session.id = OTHER_SID // /clear while the read is out
    gate.held = undefined
    release()
    await clock.settle()
    // That read found SID's call live; the band now belongs to OTHER_SID, so it must not land.
    expect(await ui.find({ text: BAND })).toBeUndefined()
    await ui.unmount()
  })

  test('yields to a survey', async ($: Engine, on: On) => {
    world(on, { files: status(LIVE) })
    await start($)
    const ui = await $.ui.mount({ ...BAND_SITE, surface: 'terminal', props: { ...PROPS, hasSurvey: true } })
    expect(await ui.find({ text: BAND })).toBeUndefined()
    await ui.unmount()
  })
})

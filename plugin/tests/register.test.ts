import { describe, expect, mock, test } from 'claude-code/testing'
import type { Engine } from 'claude-code/testing'
import type { On } from 'claude-code'

import { BAND, summarize } from '../hooks/register'

const SID = '0b4e7c1a-9d2f-4e55-8a3b-1c2d3e4f5a6b'
const HOME = '/home/u'
const DIR = `${HOME}/.local/state/bettercallgpt/${SID}`
const NOW_MS = 1_790_000_000_000
// A live call: its heartbeat refreshed `at` 5 s ago.
const LIVE = { phase: 'running', relay: 'qualified', ended: {}, pid: 4242, at: NOW_MS / 1000 - 5 }
// What the engine beneath answers; the plugin must hand it back exactly.
const BELOW = { stopReason: 'from below' }
const PROMPT = { tool_name: 'Bash', tool_input: { command: 'touch  hello.txt\n' }, session_id: SID }

type Write = { path: string; text: string }

/** The world beneath the plugin: a file system in memory, the session, env and clock. */
function world(on: On, files: Map<string, string>, env: Record<string, string> = { HOME }) {
  const writes: Write[] = []
  const below: unknown[] = []
  on('fs.exists', ($, e) => ({ value: files.has(e.path) }))
  on('fs.read', ($, e) => {
    const text = files.get(e.path)
    return text === undefined ? { deny: `ENOENT: ${e.path}` } : { value: text }
  })
  on('fs.write', ($, e) => {
    writes.push({ path: e.path, text: e.text })
    files.set(e.path, e.text)
    return { value: undefined }
  })
  on('session.id', () => ({ value: SID }))
  on('session.start', ($, e) => ({ cwd: e.cwd }))
  on('classic.PermissionRequest', ($, e) => {
    below.push(e)
    return BELOW
  })
  on('ui.render', { component: 'AbovePrompt' }, ($, e) => {
    const { Box } = $.ui.resolve(e)
    return h(Box, { key: 'engine' }) as ReturnType<typeof Box>
  })
  mock.env(on, env)
  const clock = mock.clock(on, { now: NOW_MS })
  return { writes, below, clock }
}

const status = (value: object) => new Map([[`${DIR}/status.json`, JSON.stringify(value)]])

describe('classic.PermissionRequest', () => {
  test('passes the prompt through untouched and answers nothing itself', async ($: Engine, on: On) => {
    const { below } = world(on, status(LIVE))
    const result = await $.classic.PermissionRequest(PROMPT)
    expect(result).toEqual(BELOW)
    expect('decision' in result).toBe(false)
    expect(below).toHaveLength(1)
    expect(below[0]).toMatchObject(PROMPT)
  })

  test('writes nothing when no call is live', async ($: Engine, on: On) => {
    const files = new Map<string, string>()
    const { writes } = world(on, files)
    for (const value of [
      undefined,
      { ...LIVE, phase: 'ended', ended: { reason: 'stopped by control' } },
      { ...LIVE, relay: 'probing' },
      { ...LIVE, ended: { reason: 'stream ended' } },
      { ...LIVE, at: NOW_MS / 1000 - 31 },          // a heartbeat that stopped: a dead writer
      { ...LIVE, at: undefined },
    ]) {
      files.clear()
      if (value !== undefined) files.set(`${DIR}/status.json`, JSON.stringify(value))
      expect(await $.classic.PermissionRequest(PROMPT)).toEqual(BELOW)
      expect(writes).toEqual([])
    }
  })

  test('writes permission.json into the call directory when the call is live', async ($: Engine, on: On) => {
    const { writes } = world(on, status(LIVE))
    expect(await $.classic.PermissionRequest(PROMPT)).toEqual(BELOW)
    expect(writes).toHaveLength(1)
    expect(writes[0]?.path).toBe(`${DIR}/permission.json`)
    expect(JSON.parse(writes[0]?.text ?? '')).toEqual({
      at: NOW_MS / 1000,
      tool: 'Bash',
      summary: 'touch hello.txt',
    })
  })

  test('finds the state directory the way the launcher does', async ($: Engine, on: On) => {
    const explicit = `/s/${SID}`
    const xdg = `/x/bettercallgpt/${SID}`
    const files = new Map([
      [`${explicit}/status.json`, JSON.stringify(LIVE)],
      [`${xdg}/status.json`, JSON.stringify(LIVE)],
    ])
    const { writes } = world(on, files, { HOME, XDG_STATE_HOME: '/x', VOICE_LISTEN_STATE_DIR: '/s' })
    await $.classic.PermissionRequest(PROMPT)
    expect(writes.map(w => w.path)).toEqual([`${explicit}/permission.json`])
  })

  test('a session id that is not a plain name is never used as a path', async ($: Engine, on: On) => {
    const { writes } = world(on, status(LIVE))
    expect(await $.classic.PermissionRequest({ ...PROMPT, session_id: '../etc' })).toEqual(BELOW)
    expect(writes).toEqual([])
  })

  test('an unreadable status leaves the prompt alone', async ($: Engine, on: On) => {
    const { writes } = world(on, new Map([[`${DIR}/status.json`, '{"phase": "runn']]))
    expect(await $.classic.PermissionRequest(PROMPT)).toEqual(BELOW)
    expect(writes).toEqual([])
  })
})

describe('summarize', () => {
  test('names the command, the file or the MCP tool on one short line', () => {
    expect(summarize('Bash', { command: 'git status\n  && ls' })).toBe('git status && ls')
    expect(summarize('Edit', { file_path: '/repo/a.ts', old_string: 'x' })).toBe('/repo/a.ts')
    expect(summarize('NotebookEdit', { notebook_path: '/n.ipynb' })).toBe('/n.ipynb')
    expect(summarize('mcp__github__create_issue', { title: 't' })).toBe('mcp__github__create_issue')
    expect(summarize('WebFetch', { url: 'https://example.com' })).toBe('')
    const long = summarize('Bash', { command: 'x'.repeat(500) })
    expect(long.length).toBe(200)
    expect(long.endsWith('…')).toBe(true)
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

  test('shows only while this session is on a call', async ($: Engine, on: On) => {
    const files = status({ ...LIVE, relay: 'probing' })
    const { clock } = world(on, files)
    await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })
    for (const surface of ['terminal', 'desktop'] as const) {
      const ui = await $.ui.mount({ plugin: 'bettercallgpt', surface, component: 'AbovePrompt', props: PROPS })
      expect(await ui.find({ text: BAND })).toBeUndefined()
      expect(await ui.find({ key: 'engine' })).toBeDefined()
      await ui.unmount()
    }

    files.set(`${DIR}/status.json`, JSON.stringify(LIVE))
    await clock.advance(2000)
    for (const surface of ['terminal', 'desktop'] as const) {
      const ui = await $.ui.mount({ plugin: 'bettercallgpt', surface, component: 'AbovePrompt', props: PROPS })
      expect((await ui.find({ type: 'Text', text: BAND }))?.props.dimColor).toBe(true)
      await ui.unmount()
    }

    files.set(`${DIR}/status.json`, JSON.stringify({ ...LIVE, phase: 'ended', ended: { reason: 'x' } }))
    await clock.advance(2000)
    const ui = await $.ui.mount({ plugin: 'bettercallgpt', surface: 'terminal', component: 'AbovePrompt', props: PROPS })
    expect(await ui.find({ text: BAND })).toBeUndefined()
    await ui.unmount()
  })

  test('goes away when the heartbeat stops', async ($: Engine, on: On) => {
    const files = status(LIVE)
    const { clock } = world(on, files)
    await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })
    const mount = () =>
      $.ui.mount({ plugin: 'bettercallgpt', surface: 'terminal', component: 'AbovePrompt', props: PROPS })
    let ui = await mount()
    expect(await ui.find({ text: BAND })).toBeDefined()
    await ui.unmount()

    await clock.advance(26_000) // `at` is now 31 s old: the voice process stopped writing
    ui = await mount()
    expect(await ui.find({ text: BAND })).toBeUndefined()
    await ui.unmount()
  })

  test('yields to a survey', async ($: Engine, on: On) => {
    world(on, status(LIVE))
    await $.session.start({ cwd: '/repo', surface: 'terminal', isInteractive: true })
    const ui = await $.ui.mount({
      plugin: 'bettercallgpt',
      surface: 'terminal',
      component: 'AbovePrompt',
      props: { ...PROPS, hasSurvey: true },
    })
    expect(await ui.find({ text: BAND })).toBeUndefined()
    await ui.unmount()
  })
})

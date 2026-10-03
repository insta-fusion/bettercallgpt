// Better Call GPT's hooks module. It only observes; it never answers a permission prompt.
//
// - classic.PermissionRequest: while this session is on a call, write permission.json into the
//   call's own state directory, so the voice can say that a prompt is waiting at the keyboard.
//   The hook always returns what next(e) returns, untouched, and a failure here is swallowed.
// - ui.render AbovePrompt: while this session is on a call, one dim line above the prompt.
//
// It reads only status.json (written by the voice process) and writes only permission.json,
// both in <state root>/<session id>/, the directory the voice process created (0700).
import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { CallLive } from '../types'

const APP = 'bettercallgpt'
const POLL_MS = 2000
const SUMMARY_MAX = 200
// The session ids the launcher accepts (bettercallgpt/cli.py SESSION_ID): a name, never a path.
const SESSION_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/
const FILE_TOOLS = new Set(['Read', 'Edit', 'MultiEdit', 'Write', 'NotebookEdit'])

export const BAND = "On a call · Better Call GPT · voice can't approve, use your keyboard"

const live = atom({ plugin: 'bettercallgpt', key: 'live' } as const, false as CallLive)

/** Where the voice process keeps its state, resolved as its launcher does. */
async function stateRoot($: EngineInterface): Promise<string | undefined> {
  const absolute = (path: string | undefined) =>
    path?.startsWith('/') ? path.replace(/\/+$/, '') : undefined
  const explicit = absolute(await $.env.get('VOICE_LISTEN_STATE_DIR'))
  if (explicit !== undefined) return explicit
  const xdg = absolute(await $.env.get('XDG_STATE_HOME'))
  if (xdg !== undefined) return `${xdg}/${APP}`
  const home = absolute(await $.env.get('HOME'))
  return home === undefined ? undefined : `${home}/.local/state/${APP}`
}

/** The same rule as `bettercallgpt statusline`, less the pid probe: running, relay
 * qualified, not ended (an empty `ended` object is "not ended", as in Python). */
export function isLive(status: unknown): boolean {
  if (typeof status !== 'object' || status === null) return false
  const { phase, relay, ended } = status as Record<string, unknown>
  const hasEnded =
    typeof ended === 'object' && ended !== null ? Object.keys(ended).length > 0 : Boolean(ended)
  return phase === 'running' && relay === 'qualified' && !hasEnded
}

/** The call's state directory when this session is on a live call, else undefined. */
async function liveCallDir($: EngineInterface, sessionId: string): Promise<string | undefined> {
  if (!SESSION_ID.test(sessionId)) return undefined
  const root = await stateRoot($)
  if (root === undefined) return undefined
  const dir = `${root}/${sessionId}`
  const statusPath = `${dir}/status.json`
  if (!(await $.fs.exists(statusPath))) return undefined
  const status: unknown = JSON.parse(await $.fs.read(statusPath))
  return isLive(status) ? dir : undefined
}

/** One line naming what the prompt is about: the Bash command, the file, or the MCP tool. */
export function summarize(tool: string, input: unknown): string {
  const fields = typeof input === 'object' && input !== null ? (input as Record<string, unknown>) : {}
  const text = (value: unknown) => (typeof value === 'string' ? value : '')
  let summary = ''
  if (tool === 'Bash') summary = text(fields.command)
  else if (FILE_TOOLS.has(tool)) summary = text(fields.file_path) || text(fields.notebook_path)
  else if (tool.startsWith('mcp__')) summary = tool
  const line = summary.replace(/\s+/g, ' ').trim()
  return line.length > SUMMARY_MAX ? `${line.slice(0, SUMMARY_MAX - 1)}…` : line
}

export const register: Register = on => {
  on('classic.PermissionRequest', async ($, e, next) => {
    try {
      const dir = await liveCallDir($, e.session_id)
      if (dir !== undefined) {
        const at = (await $.clock.now()) / 1000
        const event = { at, tool: e.tool_name, summary: summarize(e.tool_name, e.tool_input) }
        await $.fs.write(`${dir}/permission.json`, JSON.stringify(event))
      }
    } catch {
      // Observing failed. The prompt is the same either way.
    }
    return next(e)
  })

  on('session.start', async ($, e, next) => {
    const poll = async () => {
      try {
        const isOnCall = (await liveCallDir($, await $.session.id())) !== undefined
        if (isOnCall !== (await read($, live))) await update($, live, () => isOnCall)
      } catch {
        // Unreadable for now: keep the last answer.
      }
    }
    await poll()
    $.clock.every(POLL_MS, () => void poll())
    return next(e)
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const isDrawn = e.surface === 'terminal' || e.surface === 'desktop'
    if (!isDrawn || e.props.hasSurvey || !(await read($, live))) return next(e)
    const { Text } = $.ui.resolve(e)
    return (
      <Text dimColor wrap="truncate-end">
        {BAND}
      </Text>
    )
  })
}

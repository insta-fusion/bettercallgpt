// Better Call GPT's hooks module. It only observes; it never answers a permission prompt.
//
// - classic.PermissionRequest: while this session is on a call, write permission.json into the
//   call's own state directory, so the voice can say Claude is asking for a permission. The event
//   is a permission REQUEST (another hook or the host may still decide it without a dialog).
//   The hook always returns what next(e) returns, untouched, and a failure here is swallowed.
// - session.start: every 2 s, read status.json to know whether this session is on a call.
// - ui.render AbovePrompt: while this session is on a call, one dim line above the prompt,
//   below whatever other mods draw there.
//
// It reads only status.json (written by the voice process) and writes only permission.json,
// both in <state root>/<session id>/, the directory the voice process created (0700).
import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { CallLive } from '../types'

const APP = 'bettercallgpt'
const POLL_MS = 2000
// Longer than this after flattening, no summary is written at all (the tool's name still is):
// a cut command could split a credential the voice process would then fail to mask.
const SUMMARY_MAX = 2000
// The voice process refreshes `at` in status.json every 10 s while the call runs; older than
// this, the status is one a killed process left behind. A little ahead is clock skew.
const STALE_AFTER_S = 30
const AHEAD_S = 5
// The session ids the launcher accepts (bettercallgpt/cli.py SESSION_ID): a name, never a path.
const SESSION_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/
const FILE_TOOLS = new Set(['Read', 'Edit', 'MultiEdit', 'Write', 'NotebookEdit'])

export const BAND = "On a call · Better Call GPT · voice can't approve, use your keyboard"

const live = atom({ plugin: 'bettercallgpt', key: 'live' } as const, false as CallLive)

/** Where the voice process keeps its state. The order mirrors bettercallgpt/cli.py
 * user_state_dir + apply_defaults, which main() applies before daemon.main in the same process. */
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

/** The rule of `bettercallgpt statusline` with a fresh heartbeat in place of its pid probe:
 * running, relay qualified, not ended (an empty `ended` object is "not ended", as in Python),
 * and a finite `at` (epoch seconds) between AHEAD_S ahead and STALE_AFTER_S behind now. */
export function isLive(status: unknown, nowSeconds: number): boolean {
  if (typeof status !== 'object' || status === null) return false
  const { phase, relay, ended, at } = status as Record<string, unknown>
  const hasEnded =
    typeof ended === 'object' && ended !== null ? Object.keys(ended).length > 0 : Boolean(ended)
  const age = typeof at === 'number' && Number.isFinite(at) ? nowSeconds - at : NaN
  const isFresh = age >= -AHEAD_S && age <= STALE_AFTER_S
  return phase === 'running' && relay === 'qualified' && !hasEnded && isFresh
}

type LiveCall = { dir: string; instance: string }

/** This session's live call (its state directory and the voice process's instance id), or
 * undefined. Rejects when status.json cannot be read or parsed. */
async function liveCall($: EngineInterface, sessionId: string): Promise<LiveCall | undefined> {
  if (!SESSION_ID.test(sessionId)) return undefined
  const root = await stateRoot($)
  if (root === undefined) return undefined
  const dir = `${root}/${sessionId}`
  const statusPath = `${dir}/status.json`
  if (!(await $.fs.exists(statusPath))) return undefined
  const status: unknown = JSON.parse(await $.fs.read(statusPath))
  if (!isLive(status, (await $.clock.now()) / 1000)) return undefined
  const { instance } = status as Record<string, unknown>
  return { dir, instance: typeof instance === 'string' ? instance : '' }
}

/** One line naming what the request is about: the Bash command, the file, or the MCP tool;
 * empty when that line is longer than SUMMARY_MAX (never a partial command). */
export function summarize(tool: string, input: unknown): string {
  const fields = typeof input === 'object' && input !== null ? (input as Record<string, unknown>) : {}
  const text = (value: unknown) => (typeof value === 'string' ? value : '')
  let summary = ''
  if (tool === 'Bash') summary = text(fields.command)
  else if (FILE_TOOLS.has(tool)) summary = text(fields.file_path) || text(fields.notebook_path)
  else if (tool.startsWith('mcp__')) summary = tool
  const line = summary.replace(/\s+/g, ' ').trim()
  return line.length <= SUMMARY_MAX ? line : ''
}

export const register: Register = on => {
  on('classic.PermissionRequest', async ($, e, next) => {
    try {
      const call = await liveCall($, e.session_id)
      if (call !== undefined) {
        const at = (await $.clock.now()) / 1000
        const summary = summarize(e.tool_name, e.tool_input)
        // `instance` ties the notice to this call: a later call in the same session ignores it.
        const event = { at, tool: e.tool_name, summary, instance: call.instance }
        await $.fs.write(`${call.dir}/permission.json`, JSON.stringify(event))
      }
    } catch {
      // Observing failed. The prompt is the same either way.
    }
    return next(e)
  })

  on('session.start', async ($, e, next) => {
    let isPolling = false
    const poll = async () => {
      if (isPolling) return // one read at a time: a slow one never lands after a newer one
      isPolling = true
      try {
        const sessionId = await $.session.id()
        // A status that cannot be read or parsed is no call.
        const isOnCall = await liveCall($, sessionId).then(
          call => call !== undefined,
          () => false,
        )
        // A /clear while reading moved to another session: this answer is for the old one.
        if (sessionId !== (await $.session.id())) return
        if (isOnCall !== (await read($, live))) await update($, live, () => isOnCall)
      } catch {
        // The session itself could not be asked: try again on the next tick.
      } finally {
        isPolling = false
      }
    }
    await poll()
    $.clock.every(POLL_MS, () => void poll())
    return next(e)
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const isDrawn = e.surface === 'terminal' || e.surface === 'desktop'
    if (!isDrawn || e.props.hasSurvey || !(await read($, live))) return next(e)
    // The band is shared: keep what the mods after this one draw (mods_interface.md, "Pick
    // where to draw"), and add one line under it.
    const below = await next(e)
    const { Box, Text } = $.ui.resolve(e)
    return (
      <Box flexDirection="column">
        {below}
        <Text dimColor wrap="truncate-end">
          {BAND}
        </Text>
      </Box>
    )
  })
}

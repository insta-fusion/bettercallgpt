// Better Call GPT's hooks module: the call console.
//
// - ui.render AbovePrompt: one band. No call: [ Call ]. On a call: what the voice heard and has
//   not handed over, how many spoken messages wait in Claude's queue, and [ Steer ] [ Hang up ].
// - Call: starts the voice process as this session's child ($.process.spawn), on the operator's
//   own press. No model turn runs and no permission prompt is involved: the press is the
//   consent to open the microphone and the paid voice connection.
// - Hang up: the voice process's own `stop` (goodbye, falling tone, device released).
// - Steer: "take what I said now". The voice process is asked to hand over what it heard; and
//   when a spoken message waits behind Claude's running turn, that turn is ended so the message
//   is read next. Nothing is ever sent twice.
// - /call, /hangup, /steer do the same as the buttons.
// - classic.PermissionRequest: while on a call, write permission.json so the voice can say
//   Claude is asking for a permission. Observed only: the hook returns what next(e) returns.
//   Approvals stay on the keyboard.
//
// It reads status.json (written by the voice process) and writes only permission.json, both in
// <state root>/<session id>/, the directory the voice process created (0700).
import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { CallView } from '../types'

const APP = 'bettercallgpt'
// The release this plugin starts when no `bettercallgpt` is installed on PATH.
const RELEASE = 'git+https://github.com/insta-fusion/bettercallgpt@v0.2.0'
const TICK_MS = 500
// With no call, status.json is read every IDLE_TICKS ticks (a call started with
// /bettercallgpt:on shows up within two seconds).
const IDLE_TICKS = 4
// A Hang up the voice process has not answered after this long ends the child directly.
const HANGUP_BOUND_MS = 8000
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
// A spoken message as it arrives in the session: its text ends with the voice tag.
const VOICE_TAG = /⟨v#[^⟩]*⟩\s*$/

// The band's colors are the terminal's own named colors, so they follow the person's theme:
// green is live and "call", red is "hang up" and failure, yellow is waiting, cyan is the queue.
const LIVE = 'green'
const WARN = 'yellow'
const QUEUE = 'cyan'
const END = 'red'
const INK = 'black'
// Below this many columns the words heard and not sent get a row of their own.
const NARROW = 80

// The symbols on the controls, chosen with /call-icons and kept in this plugin's store.
// `keys` (the default) and `nerd` draw each symbol as a small colored key before its button:
// green to call, red to hang up, as on a phone. `keys` uses characters every font has; `nerd`
// needs a Nerd Font and has a real hung-up handset and a steering wheel. `emoji` puts emoji in
// the labels; `none` is words only.
export const ICON_SETS = {
  keys: { call: '☎', steer: '⎈', hangup: '☎', live: '● ' },
  nerd: { call: '\uf095', steer: '\u{f04d4}', hangup: '\u{f03fa}', live: '\u{f036c} ' },
  emoji: { call: '📞 ', steer: '🛞 ', hangup: '📴 ', live: '🎙 ' },
  none: { call: '', steer: '', hangup: '', live: '● ' },
} as const
export type IconSet = keyof typeof ICON_SETS
const ICONS_KEY = 'icons'

export const IDLE: CallView = { phase: 'idle', unsent: '', queued: 0, working: false, note: '' }

const call = atom({ plugin: 'bettercallgpt', key: 'call' } as const, IDLE)

type Child = AsyncGenerator<unknown, unknown, unknown>
type Status = Record<string, unknown>
type LiveCall = { dir: string; instance: string; status: Status }

// This module's own memory. A reload starts it over, and a reload also ends the child.
let child: Child | undefined
let command: string[] | undefined
let mainTurn = ''
let steerWaits = false
let isStarting = false
let steerShown = ''
let icons: IconSet = 'keys'
let ticks = 0
let isPolling = false

function set($: EngineInterface, patch: Partial<CallView>) {
  return update($, call, view => ({ ...view, ...patch }))
}

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
  const { phase, relay, ended, at } = status as Status
  const hasEnded =
    typeof ended === 'object' && ended !== null ? Object.keys(ended).length > 0 : Boolean(ended)
  const age = typeof at === 'number' && Number.isFinite(at) ? nowSeconds - at : NaN
  const isFresh = age >= -AHEAD_S && age <= STALE_AFTER_S
  return phase === 'running' && relay === 'qualified' && !hasEnded && isFresh
}

/** This session's live call (its state directory, the voice process's instance id and its
 * status), or undefined. Rejects when status.json cannot be read or parsed. */
async function liveCall($: EngineInterface, sessionId: string): Promise<LiveCall | undefined> {
  if (!SESSION_ID.test(sessionId)) return undefined
  const root = await stateRoot($)
  if (root === undefined) return undefined
  const dir = `${root}/${sessionId}`
  const statusPath = `${dir}/status.json`
  if (!(await $.fs.exists(statusPath))) return undefined
  const status: unknown = JSON.parse(await $.fs.read(statusPath))
  if (!isLive(status, (await $.clock.now()) / 1000)) return undefined
  const { instance } = status as Status
  return { dir, instance: typeof instance === 'string' ? instance : '', status: status as Status }
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

/** What status.json says the band should show. */
export function liveFields(status: Status): Pick<CallView, 'unsent' | 'queued'> {
  const unsent = status.unsent
  const preview =
    typeof unsent === 'object' && unsent !== null ? (unsent as Status).preview : undefined
  const queued = Array.isArray(status.queued) ? status.queued.length : 0
  return { unsent: typeof preview === 'string' ? preview : '', queued }
}

/** The last line of what a failed start wrote, as one short note. */
export function failure(stderr: string, code: unknown): string {
  const lines = stderr.split('\n').map(line => line.trim()).filter(line => line !== '')
  const last = (lines.pop() ?? '').replace(/^voice:\s*/, '')
  if (last.includes('bind_no_transcript')) return 'send Claude one message first, then call again'
  if (last === '') return `the voice process ended (exit ${String(code)})`
  return last.length <= 160 ? last : `${last.slice(0, 159)}…`
}

/** How the voice process is run: an installed `bettercallgpt` when PATH has one (a checkout
 * installed for development), otherwise the pinned release through uvx. */
async function launcher($: EngineInterface): Promise<string[]> {
  if (command !== undefined) return command
  let found = await $.process
    .run(['/bin/sh', '-c', 'command -v bettercallgpt'])
    .then(result => (result.exitCode === 0 ? result.stdout.trim() : ''), () => '')
  if (!found.startsWith('/')) {
    // A desktop app started from the Dock has a short PATH: look where `uv tool install`
    // puts the command.
    const home = await $.env.get('HOME')
    const local = `${home ?? ''}/.local/bin/bettercallgpt`
    if (local.startsWith('/') && (await $.fs.exists(local).catch(() => false))) found = local
  }
  command = found.startsWith('/') ? [found] : ['uvx', '--from', RELEASE, 'bettercallgpt']
  return command
}

/** Read the child's output until it ends; then the call is over, whatever ended it. */
async function follow($: EngineInterface, stream: Child) {
  let stderr = ''
  let code: unknown
  try {
    for (;;) {
      const piece = await stream.next()
      if (piece.done) {
        code = (piece.value as { code?: unknown } | undefined)?.code
        break
      }
      const { stream: pipe, text } = piece.value as { stream: string; text: string }
      if (pipe === 'stderr') stderr = (stderr + text).slice(-4000)
    }
  } catch (error) {
    stderr = String(error)
  }
  if (child !== stream) return // a newer call owns the band
  child = undefined
  steerWaits = false
  try {
    const { phase } = await read($, call)
    const note = phase === 'starting' ? failure(stderr, code) : ''
    await set($, { ...IDLE, working: mainTurn !== '', note })
  } catch {
    // The module was unloaded while the child was ending: there is no band left to update.
  }
}

async function startCall($: EngineInterface): Promise<string> {
  // Two presses in a row: the flag is taken before anything is awaited, so one call starts.
  if (isStarting || child !== undefined) return 'a call is already on'
  isStarting = true
  try {
    const view = await read($, call)
    if (view.phase !== 'idle') return 'a call is already on'
    const sessionId = await $.session.id()
    if (!SESSION_ID.test(sessionId)) return 'this session has no usable id'
    const nonce = `mod-${(await $.clock.now()).toString(36)}-${Math.random().toString(36).slice(2, 10)}`
    const argv = [...(await launcher($)), '--session', sessionId, '--nonce', nonce, '--mod', 'start']
    const stream = $.process.spawn({ argv, env: { NONCE: nonce } }) as unknown as Child
    child = stream
    await set($, { ...IDLE, phase: 'starting', working: mainTurn !== '', note: '' })
    void follow($, stream)
    return 'calling'
  } finally {
    isStarting = false
  }
}

async function hangUp($: EngineInterface): Promise<string> {
  const view = await read($, call)
  if (view.phase === 'idle') return 'no call to hang up'
  const sessionId = await $.session.id()
  await set($, { phase: 'ending', note: '' })
  steerWaits = false
  const ending = child
  if (ending !== undefined) {
    // The voice process says goodbye and exits by itself; one that does not is ended here.
    // Armed before the stop is sent: a stop that hangs must not leave the call running.
    $.clock.after(HANGUP_BOUND_MS, () => {
      if (child === ending) void ending.return(undefined)
    })
  }
  await $.process
    .run([...(await launcher($)), '--session', sessionId, 'stop'])
    .catch(() => undefined)
  return 'hanging up'
}

/** End Claude's running turn when a Steer is waiting and a spoken message sits behind it.
 * The status is read again right here: a message Claude took in the meantime (it is no longer
 * queued) must not cost the turn that is now working on it. */
async function bringForward($: EngineInterface) {
  if (!steerWaits) return
  const turnId = mainTurn
  if (turnId === '') {
    steerWaits = false // idle: a message starts its own turn
    return
  }
  const sessionId = await $.session.id()
  const live = await liveCall($, sessionId).catch(() => undefined)
  if (live === undefined || liveFields(live.status).queued === 0) return
  if (!steerWaits || turnId !== mainTurn) return // answered while reading
  steerWaits = false
  try {
    await $.turn.abort({ turnId })
    $.ui.toast('Steer: Claude takes your message now')
  } catch {
    // That turn ended by itself in the meantime: the message is read next anyway.
  }
}

async function chooseIcons($: EngineInterface, wanted: string): Promise<string> {
  const name = wanted.trim()
  if (!(name in ICON_SETS)) return `icons: ${icons} (choose keys, nerd, emoji or none)`
  icons = name as IconSet
  await $.store.set(ICONS_KEY, icons)
  await update($, call, view => ({ ...view })) // draw the band again
  return `icons: ${icons}`
}

/** What the voice process answered to the last Steer, as the band's note. */
export function steerNote(result: unknown): string {
  if (typeof result !== 'string' || result === '') return ''
  if (result === 'sent') return ''
  if (result === 'nothing_unsent') return ''
  return `steer did not send (${result.slice(0, 60)})`
}

async function steer($: EngineInterface): Promise<string> {
  const view = await read($, call)
  if (view.phase !== 'live') return 'no call to steer'
  const sessionId = await $.session.id()
  // A Steer waits only for the turn it was pressed in (turn.complete clears it).
  steerWaits = mainTurn !== ''
  // The voice process sends what it heard and has not handed over, itself, as a message that
  // ends Claude's running turn. Its answer comes back in status.json (`steer`).
  await $.process
    .run([...(await launcher($)), '--session', sessionId, 'steer'])
    .catch(() => undefined)
  await bringForward($)
  return 'steering'
}

async function poll($: EngineInterface, isFirst = false) {
  if (isPolling) return // one read at a time: a slow one never lands after a newer one
  isPolling = true
  try {
    const view = await read($, call)
    ticks += 1
    if (!isFirst && view.phase === 'idle' && ticks % IDLE_TICKS !== 0) return
    const sessionId = await $.session.id()
    // A status that cannot be read or parsed is no call.
    const live = await liveCall($, sessionId).catch(() => undefined)
    // A /clear while reading moved to another session: this answer is for the old one.
    if (sessionId !== (await $.session.id())) return
    if (live === undefined) {
      // No child and no live status: the call (one started by /bettercallgpt:on) is over.
      if (child === undefined && view.phase !== 'idle') await set($, { ...IDLE, working: view.working })
      return
    }
    const fields = liveFields(live.status)
    // The voice process's answer to a Steer, shown once per press.
    const answer = (live.status.steer ?? {}) as Status
    const answerId = typeof answer.id === 'string' ? answer.id : ''
    let note: string | undefined
    if (answerId !== '' && answerId !== steerShown) {
      // What happened is said once, in passing; only a failure stays in the band.
      note = steerNote(answer.result)
      if (answer.result === 'sent') $.ui.toast('Steer: sent what you said')
      if (answer.result === 'nothing_unsent' && fields.queued === 0) $.ui.toast('Nothing to steer')
      steerShown = answerId
    }
    const isSame =
      view.phase === 'live' && view.unsent === fields.unsent && view.queued === fields.queued
    if (!isSame || note !== undefined) {
      // The phase is decided on the view as it is when written: a Hang up pressed while this
      // read was out stays "ending".
      await update($, call, now => ({
        ...now,
        ...fields,
        phase: now.phase === 'ending' ? 'ending' : 'live',
        note: note ?? now.note,
      }))
    }
    await bringForward($)
  } catch {
    // The session itself could not be asked: try again on the next tick.
  } finally {
    isPolling = false
  }
}

export const register: Register = on => {
  on('classic.PermissionRequest', async ($, e, next) => {
    try {
      const live = await liveCall($, e.session_id)
      if (live !== undefined) {
        const at = (await $.clock.now()) / 1000
        const summary = summarize(e.tool_name, e.tool_input)
        // `instance` ties the notice to this call: a later call in the same session ignores it.
        const event = { at, tool: e.tool_name, summary, instance: live.instance }
        await $.fs.write(`${live.dir}/permission.json`, JSON.stringify(event))
      }
    } catch {
      // Observing failed. The prompt is the same either way.
    }
    return next(e)
  })

  on('session.start', async ($, e, next) => {
    const kept = await $.store.get(ICONS_KEY).catch(() => undefined)
    if (typeof kept === 'string' && kept in ICON_SETS) icons = kept as IconSet
    try {
      await $.command.register({
        name: 'call-icons',
        description: 'Symbols on the call controls: keys, nerd (Nerd Font), emoji or none',
        argumentHint: '[keys|nerd|emoji|none]',
        immediate: true,
      })
      await $.command.register({
        name: 'call',
        description: 'Start a voice call in this session',
        immediate: true,
      })
      await $.command.register({ name: 'hangup', description: 'End the voice call', immediate: true })
      await $.command.register({
        name: 'steer',
        description: 'Have Claude take what you just said now',
        immediate: true,
      })
    } catch {
      // A host that takes no commands still gets the band and its buttons.
    }
    await poll($, true)
    $.clock.every(TICK_MS, () => void poll($))
    return next(e)
  })

  on('session.end', async ($, e, next) => {
    // The call belongs to the session that started it: a /clear or an exit ends it.
    await hangUp($) // also a call /bettercallgpt:on started; with no call it does nothing
    return next(e)
  })

  on('command.run', { command: 'call' }, async $ => ({ text: await startCall($) }))
  on('command.run', { command: 'call-icons' }, async ($, e) => ({ text: await chooseIcons($, e.args) }))
  on('command.run', { command: 'hangup' }, async $ => ({ text: await hangUp($) }))
  on('command.run', { command: 'steer' }, async $ => ({ text: await steer($) }))

  on('turn.start', async ($, e, next) => {
    mainTurn = e.turnId
    await set($, { working: true })
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    // A subagent's turn completes here too; only the main turn's own end clears it.
    if (e.agentId === undefined && e.turnId === mainTurn) {
      mainTurn = ''
      steerWaits = false // a Steer is about the turn it was pressed in
      await set($, { working: false })
    }
    return next(e)
  })

  on('prompt.submit', async ($, e, next) => {
    // A spoken message was just read by Claude: a Steer that was waiting for that is done.
    if (e.origin.kind === 'peer' && VOICE_TAG.test(e.text)) steerWaits = false
    return next(e)
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const isDrawn = e.surface === 'terminal' || e.surface === 'desktop'
    if (!isDrawn || e.props.hasSurvey) return next(e)
    const view = await read($, call)
    // The band is shared: keep what the mods after this one draw (mods_interface.md, "Pick
    // where to draw"), and add one line under it.
    const below = await next(e)
    const { Box, Button, Text } = $.ui.resolve(e)
    const icon = ICON_SETS[icons]
    // Emoji carry their own colors, inside the label. The other symbols are one-color
    // characters: each is drawn before its button as a key in the control's color.
    const isKeyed = icons === 'keys' || icons === 'nerd'
    const inLabel = (symbol: string) => (isKeyed ? '' : symbol)
    const tint = (symbol: string, color: string) =>
      isKeyed ? (
        <Text backgroundColor={color} color={INK} bold>
          {` ${symbol} `}
        </Text>
      ) : null
    const isNarrow = e.props.bodyColumns < NARROW
    if (view.phase === 'idle') {
      const hasFailed = view.note !== ''
      return (
        <Box flexDirection="column">
          {below}
          <Box gap={1}>
            {hasFailed ? (
              <Text backgroundColor={END} color={INK} bold>
                {' ✕ CALL FAILED '}
              </Text>
            ) : null}
            {tint(icon.call, LIVE)}
            <Button
              key="call"
              label={`${inLabel(icon.call)}${hasFailed ? 'Call again' : 'Call'}`}
              hotkey="c"
              variant="primary"
              onPress={() => void startCall($)}
            />
            {hasFailed ? (
              <Text wrap="truncate-end">{view.note}</Text>
            ) : (
              <Text wrap="truncate-end">
                <Text bold>Better Call GPT</Text>
                <Text dimColor>{isNarrow ? '' : ' · talk to this session by voice'}</Text>
              </Text>
            )}
          </Box>
        </Box>
      )
    }
    if (view.phase === 'starting' || view.phase === 'ending') {
      return (
        <Box flexDirection="column">
          {below}
          <Box gap={1}>
            <Text backgroundColor={WARN} color={INK} bold>
              {view.phase === 'starting' ? ' ◌ CONNECTING ' : ' ◌ HANGING UP '}
            </Text>
            <Text dimColor wrap="truncate-end">
              {view.phase === 'starting' ? 'opening the microphone and the voice…' : 'saying goodbye…'}
            </Text>
            {view.phase === 'starting' ? tint(icon.hangup, END) : null}
            {view.phase === 'starting' ? (
              <Button key="hangup" label={`${inLabel(icon.hangup)}Hang up`} hotkey="h" onPress={() => void hangUp($)} />
            ) : null}
          </Box>
        </Box>
      )
    }
    const canSteer = view.unsent !== '' || view.queued > 0
    const quote =
      view.unsent === '' ? null : (
        <Text color={WARN} italic wrap="truncate-start">
          «{view.unsent}» not sent
        </Text>
      )
    return (
      <Box flexDirection="column">
        {below}
        <Box gap={1}>
          <Text backgroundColor={LIVE} color={INK} bold>
            {` ${icon.live}LIVE `}
          </Text>
          <Box flexGrow={1} flexShrink={1} gap={1} overflow="hidden">
            {canSteer ? null : <Text dimColor>{view.note === '' ? 'listening' : view.note}</Text>}
            {isNarrow ? null : quote}
            {view.queued === 0 ? null : (
              <Text color={QUEUE} bold>
                {view.queued} queued
              </Text>
            )}
            {canSteer && view.note !== '' ? <Text dimColor>{view.note}</Text> : null}
          </Box>
          <Box flexShrink={0} gap={1}>
            {tint(icon.steer, QUEUE)}
            <Button
              key="steer"
              label={`${inLabel(icon.steer)}Steer`}
              hotkey="s"
              variant={canSteer ? 'primary' : 'secondary'}
              dimColor={!canSteer}
              onPress={() => void steer($)}
            />
            {tint(icon.hangup, END)}
            <Button
              key="hangup"
              label={`${inLabel(icon.hangup)}Hang up`}
              hotkey="h"
              variant="secondary"
              onPress={() => void hangUp($)}
            />
          </Box>
        </Box>
        {isNarrow ? quote : null}
      </Box>
    )
  })
}

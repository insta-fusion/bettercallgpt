// The values this plugin keeps in $.state for the session.
// `call`: what the band above the prompt draws.
//   phase   idle (no call), starting (the voice process is coming up), live, ending
//   unsent  the tail of what the voice heard and has not handed to Claude yet ('' when none)
//   queued  how many spoken messages wait in Claude's queue behind the running turn
//   working whether Claude is in a turn right now
//   note    one line about the last thing that happened (a failed start, a Steer's result)

export type CallPhase = 'idle' | 'starting' | 'live' | 'ending'

export type CallView = {
  phase: CallPhase
  unsent: string
  queued: number
  working: boolean
  note: string
}

declare module 'claude-code' {
  interface PluginState {
    bettercallgpt: { call: CallView }
  }
}

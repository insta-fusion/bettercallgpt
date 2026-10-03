// The values this plugin keeps in $.state for the session.
// `live`: whether this session is on a call right now (status.json, polled).

export type CallLive = boolean

declare module 'claude-code' {
  interface PluginState {
    bettercallgpt: { live: CallLive }
  }
}

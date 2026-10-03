# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.0.0/); versions follow
[SemVer](https://semver.org/) with a pre-1.0 caveat: minor bumps may break.

## [Unreleased]

### Added
- The plugin carries one hooks module (`plugin/hooks/register.tsx`) that only observes. While this
  session is on a call it writes `permission.json` into the call's own state directory when a
  permission prompt opens, and the call tells you a prompt is waiting at your keyboard, in any
  terminal, not only in an Orca pane. It also draws one dim line above the prompt during a call.
  It never answers or changes a prompt; turn it off with `disableAllHooks` or `/plugin`.
- The voice process reads `permission.json` (deduped, never a previous call's) and announces it
  with no options, so a spoken word can never approve it. On an Orca pane the screen and the hook
  announce one prompt once.

## [0.1.1] - 2026-10-02

### Added
- In an Orca pane, the call now uses Orca's own agent-wait signal (`orca terminal show --json`,
  field `agentWait`) to tell you when Claude is waiting on a permission prompt. It only adds: when
  Orca reports no wait, the screen reading still decides, so a prompt is never hidden.

### Changed
- Plugin README: where it works (Claude Code on macOS, CLI and desktop Code tab), why the voice key
  stays in a local file, and that the project is not affiliated with OpenAI, Microsoft or Anthropic.
- README: the support table names the verified surfaces and voice services, and links a request form
  for other agents, CLIs and voice models.

## [0.1.0] - 2026-10-01 (first public release)

A full-duplex voice call for a Claude Code session, powered by GPT Realtime (Azure Voice Live
by default; OpenAI Realtime experimental).

### Added
- `/bettercallgpt:on`, `:status`, `:off` (Claude Code plugin). The commands run the tagged release
  through `uvx --from git+…@v0.1.0`: no separate install; on macOS no `brew install portaudio`.
- One-line install: `npx skills add insta-fusion/bettercallgpt -g` installs a setup-only skill that
  checks uv and the platform, installs the plugin, creates an empty key file (mode 600) for you to
  fill and runs `doctor`. It never starts a call and never handles a key.
- Zero-keystroke session binding (screenless proof: process ancestry + transcript + nonce), and in
  an Orca pane a read-only pane binding that can tell you when a permission prompt is waiting.
- Voice provider session relay: reconnects after a drop and renews before the provider limit
  without restarting the agent session.
- `bettercallgpt doctor`, `status`, `stop`, `statusline`; `README.zh-CN.md`.

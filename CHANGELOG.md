# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.0.0/); versions follow
[SemVer](https://semver.org/) with a pre-1.0 caveat: minor bumps may break.

## [0.1.0] - Unreleased (first public release)

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

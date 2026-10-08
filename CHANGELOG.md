# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.0.0/); versions follow
[SemVer](https://semver.org/) with a pre-1.0 caveat: minor bumps may break.

## [0.2.2] - 2026-10-09

### Changed
- **The Mod looks at prompts only while a Steer waits on a live call.** Before, its prompt hook ran on
  every prompt to spot the voice tag; now any other prompt passes untouched and unread.
- **Hang up and Steer write their commands out in full,** like Call: the installed command or the pinned
  release through uvx, with the session id as the only value.
- **The listing says where your words go.** The plugin description and a new README section, "Your
  conversation and privacy", say what is read and sent, to which service and on whose key, that we collect
  nothing, and how long call files stay on your machine. The privacy link points at that section.
- `/bettercallgpt:on`: a voice-tagged line is unverified speech: it serves the conversation and the task
  under way, and anything outside it, or any change to settings, permissions or credentials, is confirmed
  at the keyboard.

### Fixed
- A Steer that was waiting when a `/bettercallgpt:on` call ended no longer outlives the call.

## [0.2.1] - 2026-10-06

### Changed
- **The Mod runs no shell.** `Call` finds an installed `bettercallgpt` as a file at three fixed paths
  (`~/.local/bin`, `/opt/homebrew/bin`, `/usr/local/bin`) instead of running `sh -c "command -v"`, and
  the command it starts is written out in full at the call site: the installed command, or the pinned
  release through `uvx`; the session id and a fresh nonce are the only values.
- **The plugin README lists everything the Mod runs, reads, writes and sends,** with the exact
  commands, the voice-service hosts, the permission hook ("decides nothing") and the one file it writes
  and its path. A "Get started" section tells directory users the three steps.
- Directory description: "Voice-call this Claude Code session: Call, Steer and Hang up above the prompt."
- README: "listed in Anthropic's Claude Code plugin directory" (not "official"); the setup skill links
  uv's install page instead of a `curl … | sh` line.

### Fixed
- `/call-icons` accepts only its own four names; inherited names such as `constructor` are refused.

## [0.2.0] - 2026-10-04

### Added
- **Call console.** The plugin carries one hooks module (`plugin/hooks/register.tsx`, needs Claude
  Code 2.1.287+; CLI and the desktop Code tab) that draws one row above the prompt: `Call`,
  `Steer` and `Hang up`, also as `/call`, `/steer` and `/hangup` (`/call-icons` picks the symbols).
  - `Call` starts the voice process as a child of the Claude Code process, on your press: no model
    turn, no permission prompt. The new `--mod` start binds on ancestry, the session registry and a
    fresh single-use nonce, and is refused unless Claude Code itself spawned the command.
  - `Steer` sends what the voice heard and has not handed over, as one request that ends Claude's
    running turn; a spoken message already waiting in the queue is brought forward the same way.
    The new `bettercallgpt steer` command carries it; nothing is sent twice. Unsent words exist
    only on the GPT-Live voice; on Voice Live, Steer brings a waiting message forward.
  - `Hang up`, `/clear` and closing the session end the call.
  - `status.json` gains `unsent` (character count and the last 60 characters, credentials masked),
    `queued` (tags waiting in the session's queue) and `steer` (the last Steer's result). `bettercallgpt status` prints the count, never the words. The
    statusline segment shows ` ✎` for unsent words and ` ⇪N` for queued messages.
- The same module observes permission requests. While this session is on a call it writes
  `permission.json` into the call's
  own state directory when Claude makes a permission request, and the call says what Claude is
  asking for ("Claude is asking to use Bash: … — answer on your keyboard"), in any terminal, not
  only in an Orca pane. It checks for a call every 2 seconds in every session where the plugin is
  loaded (local files and three env vars, no network). It never answers or changes a request; turn
  it off with `disableAllHooks` or `/plugin`.
- `/bettercallgpt:on`, `:status` and `:off` are unchanged and need no hooks module.
- The voice process reads `permission.json` (deduped, only this call's instance, never a previous
  call's) and announces it with no options, so a spoken word can never approve it. Credentials are
  masked in the whole summary before it is shortened; a summary over 2000 characters is left out.
  On an Orca pane, a request the screen already announced (same tool and command) is said once.
- Disclosure: the plugin README lists OpenAI Realtime (experimental) among the voice services, and
  says a one-line summary of each permission request reaches the selected voice service.
- While a call runs, the voice process refreshes `at` in `status.json` every 10 s. The hooks module
  takes a call as live only while `at` is at most 30 s old, so a status left behind by a killed
  process shows no band and gets no `permission.json`.

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

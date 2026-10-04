# Better Call GPT

Put this Claude Code session on a full-duplex voice call. You talk while it codes, interrupt it at any
time, and hear results read back. A spoken "yes" never approves anything: permission prompts stay on
your keyboard. macOS only.

## Commands

- `/bettercallgpt:on` starts a voice call bound to this session. Claude Code asks you to approve the start.
- `/bettercallgpt:status` shows whether the call is live.
- `/bettercallgpt:off` ends the call.

On Claude Code 2.1.287+ (CLI and the desktop Code tab) a row above the prompt does the same with one
press: **Call**, **Steer** (send what you just said now; Claude's running turn makes way) and
**Hang up**. `/call`, `/steer` and `/hangup` are the same actions; `/call-icons` picks the symbols.

## What it runs, sends and fetches

- **Runs:** the `bettercallgpt` command from this repository's tagged release, through `uvx`, as a local
  process that lives only during a call.
- **Sends:** your microphone audio and the text of the call to the realtime voice service you configure,
  using your own key: Azure OpenAI GPT Realtime through Azure Voice Live (default), the Azure GPT-Live API,
  or the OpenAI Realtime API (experimental). When Claude makes a permission request during a call, a
  one-line summary of it (the tool's name and the Bash command, file path or MCP tool name) is spoken, so
  it is sent to that voice service too. It goes nowhere else. Nothing is sent when no call is running.
- **Reads:** your key from `~/.config/bettercallgpt/.env`, which you fill yourself; setup never asks for it.
  It reads this session's own transcript and terminal pane to relay results and to notice permission prompts.
- **Writes:** a conversation ledger and status files for each call in a per-user state directory
  that only you can open (0700). They stay on your machine.
- **Fetches:** the tagged release from GitHub, on first use.
- **Mod (one hooks module, `hooks/register.tsx`; needs Claude Code 2.1.287+, tested with 2.1.287 and
  2.1.288):** runs inside Claude Code and draws the call row above the prompt.
  - **Call** starts the `bettercallgpt` command as a child of your Claude Code process: the installed
    command when there is one, otherwise the tagged release through `uvx`. Your press is the consent;
    no model turn runs and no permission prompt is shown. The voice process refuses this kind of
    start unless Claude Code itself spawned it.
  - **Steer** asks the voice process to send what you said and it has not handed over yet, and ends
    Claude's running turn so that message is read next. Only your press does this.
  - **Hang up**, `/clear` and closing the session end the call.
  - It reads `VOICE_LISTEN_STATE_DIR`, `XDG_STATE_HOME` and `HOME` to find the state directory and
    reads `status.json` there every 2 seconds (twice a second during a call). During a call that file
    holds the last 60 characters you said that are not sent yet (credentials masked), which the row
    shows. It keeps your symbol choice in the plugin's own store. It makes no network request itself.
  - During a call, when Claude makes a permission request, it writes `permission.json` (the tool's
    name and a one-line summary) into that state directory, so the call can say what Claude is asking
    for. That can include a request another hook or Claude Code then decides without asking you;
    sandbox network prompts are not covered. It never answers or changes a request.
  - Turn it off with `"disableAllHooks": true` in your settings, or disable the plugin. The three
    commands above keep working without it.

## Where it works

Claude Code on macOS: the CLI in any terminal, and the Code tab of the Claude desktop app. It needs a
Mac microphone, so it does nothing in Claude chat, Cowork, or Claude Code on the web.

## Why the key stays in a file

The voice service key lives in `~/.config/bettercallgpt/.env` rather than in plugin settings so that it
never enters a prompt, a command line or the model's context; only the local voice process reads it.

"GPT" names the voice model the call runs on. This project is not affiliated with OpenAI, Microsoft
or Anthropic.

Full guide, security notes and source: https://github.com/insta-fusion/bettercallgpt

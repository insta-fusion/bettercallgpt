# Better Call GPT

Put this Claude Code session on a full-duplex voice call. You talk while it codes, interrupt it at any
time, and hear results read back. A spoken "yes" never approves anything: permission prompts stay on
your keyboard. macOS only.

## Commands

- `/bettercallgpt:on` starts a voice call bound to this session. Claude Code asks you to approve the start.
- `/bettercallgpt:status` shows whether the call is live.
- `/bettercallgpt:off` ends the call.

## What it runs, sends and fetches

- **Runs:** the `bettercallgpt` command from this repository's tagged release, through `uvx`, as a local
  process that lives only during a call.
- **Sends:** your microphone audio and the text of the call to the realtime voice service you configure,
  using your own key: Azure OpenAI GPT Realtime through Azure Voice Live (default) or the Azure GPT-Live API.
  It goes nowhere else. Nothing is sent when no call is running.
- **Reads:** your key from `~/.config/bettercallgpt/.env`, which you fill yourself; setup never asks for it.
  It reads this session's own transcript and terminal pane to relay results and to notice permission prompts.
- **Writes:** a conversation ledger and status files for each call in a per-user state directory
  that only you can open (0700). They stay on your machine.
- **Fetches:** the tagged release from GitHub, on first use.
- **Mod (one hooks module, `hooks/register.tsx`):** runs inside Claude Code and only observes. While
  this session is on a call it reads the call's `status.json`, writes `permission.json` (the tool's
  name and a one-line summary) into that same state directory when a permission prompt opens, so the
  call can say a prompt is waiting, and draws one dim line above the prompt. It never answers or
  changes a prompt and sends nothing anywhere; with no call it writes nothing. Turn it off with
  `"disableAllHooks": true` in your settings, or disable the plugin.

## Where it works

Claude Code on macOS: the CLI in any terminal, and the Code tab of the Claude desktop app. It needs a
Mac microphone, so it does nothing in Claude chat, Cowork, or Claude Code on the web.

## Why the key stays in a file

The voice service key lives in `~/.config/bettercallgpt/.env` rather than in plugin settings so that it
never enters a prompt, a command line or the model's context; only the local voice process reads it.

"GPT" names the voice model the call runs on. This project is not affiliated with OpenAI, Microsoft
or Anthropic.

Full guide, security notes and source: https://github.com/insta-fusion/bettercallgpt

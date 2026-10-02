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

Full guide, security notes and source: https://github.com/insta-fusion/bettercallgpt

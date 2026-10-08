# Better Call GPT

Put this Claude Code session on a full-duplex voice call. You talk while it codes, interrupt it at any
time, and hear results read back. A spoken "yes" never approves anything: permission prompts stay on
your keyboard. macOS only.

Open source (MIT). We run no server and collect nothing: the call runs on your own key, with the voice
provider you choose.

## Commands

- `/bettercallgpt:on` starts a voice call bound to this session. Claude Code asks you to approve the start.
- `/bettercallgpt:status` shows whether the call is live.
- `/bettercallgpt:off` ends the call.

On Claude Code 2.1.287+ (CLI and the desktop Code tab) a row above the prompt does the same with one
press: **Call**, **Steer** (send what you just said now; Claude's running turn makes way; unsent words
exist only on the GPT-Live voice, on Voice Live Steer brings a waiting message forward) and
**Hang up** (`/clear` and closing the session end the call too). `/call`, `/steer` and `/hangup` are the same actions; `/call-icons` picks the symbols.

## Get started

1. Install [uv](https://docs.astral.sh/uv/) (the voice process runs through it).
2. Copy [`.env.example`](https://github.com/insta-fusion/bettercallgpt/blob/main/.env.example) to
   `~/.config/bettercallgpt/.env` and paste your voice key into that file, not into the chat. Keys come
   from Azure (Voice Live or GPT-Live) or OpenAI (Realtime).
3. Press **Call** above the prompt, or type `/bettercallgpt:on`. A rising tone means you are live.

## What it runs, sends and fetches

Everything below is the whole list. The plugin is one hooks module (the Mod, `hooks/register.tsx`) and
three commands; the voice itself is a separate program, `bettercallgpt`, from this repository.

### The Mod runs one program

- **Which:** `bettercallgpt`, the voice process. Nothing else.
- **How, exactly:** when you press **Call**, the Mod starts it as a child of your Claude Code process
  with one of these two commands, no shell and no inline script:
  - an installed command, if one is found as a file at `~/.local/bin/bettercallgpt`,
    `/opt/homebrew/bin/bettercallgpt` or `/usr/local/bin/bettercallgpt` (checked as files, in that
    order; the Mod runs it once with the single fixed argument `--help` to see that it knows the
    console, and passes it over otherwise):
    `bettercallgpt --session <session id> --nonce <nonce> --mod start`
  - otherwise the pinned release through uv:
    `uvx --from git+https://github.com/insta-fusion/bettercallgpt@v0.2.2 bettercallgpt --session <session id> --nonce <nonce> --mod start`
  - `<session id>` is this Claude Code session's id; `<nonce>` is a fresh single-use token the Mod
    makes for that start. No other value is read into the command.
- **Hang up** and **Steer** run the same program, written out the same two ways, with the fixed
  arguments `--session <session id> stop` and `--session <session id> steer`.
- Your press is the consent: no model turn runs and no permission prompt is shown. The voice process
  refuses this kind of start unless Claude Code itself spawned it.

### What leaves your machine, and where

- **The Mod itself sends nothing.** It makes no network request and opens no socket.
- **The voice process** sends your microphone audio and the text of the call to the one realtime voice
  service you configured, with your own key: Azure Voice Live (`*.cognitiveservices.azure.com`, the
  default), the Azure GPT-Live API (`*.openai.azure.com`), or the OpenAI Realtime API
  (`api.openai.com`). When Claude makes a permission request during a call, a one-line summary of it
  (the tool's name and the Bash command, file path or MCP tool name) is spoken, so it goes to that
  service too. Nothing goes anywhere else, and nothing is sent when no call is running.
- **Fetches:** the tagged release from GitHub, on first use, through uv.

### Your conversation and privacy

- **What is read:** at **Call**, the voice process reads this session's transcript once, on your
  machine, only to learn where the session stands (whether Claude is working, whether a message is
  waiting). None of that earlier history is sent. After that it follows only what is written in this
  session during the call.
- **What is sent:** Claude's reply text, anything you type during the call, your speech, and the
  one-line permission summaries above. Tool output and files are not forwarded.
- **Where and on whose account:** only to the voice service you configured, with your own key and under
  your own account with that provider. Its retention is set by your agreement with that provider.
- **What we collect:** nothing. There is no server of ours, no analytics and no telemetry.
- **What stays on your machine:** the voice process's call files (the ledger and status files described
  below), in a directory only you can open. They are not deleted automatically; delete
  `~/.local/state/bettercallgpt` to remove them.
- **When it stops:** at **Hang up**, `/bettercallgpt:off`, `/clear`, or when the session closes.

### What the Mod reads

- The environment variables `HOME`, `XDG_STATE_HOME` and `VOICE_LISTEN_STATE_DIR`, to find the state
  directory: `~/.local/state/bettercallgpt` unless one of the last two is set.
- `status.json` in `<state directory>/<session id>/`, every 2 seconds (twice a second during a call).
  During a call that file holds the last 60 characters you said that are not sent yet (credentials
  masked), which the call row shows.
- This session's id, to name that directory. The Mod reads no transcript and no other file.
- While a **Steer** waits during a call, each prompt Claude takes is checked for the call's voice tag, so
  the row knows your spoken message arrived. At any other time the Mod does not look at prompts. Nothing
  from a prompt is stored or sent.
- Its own symbol choice (`/call-icons`), from the plugin's store.

The **voice process** reads your key from `~/.config/bettercallgpt/.env` (you fill it yourself; setup
never asks for it), and this session's own transcript and terminal pane, to relay results and to notice
permission prompts.

### The permission hook: what it decides and when

- **When:** on every permission request Claude Code makes (`classic.PermissionRequest`), only while a
  call is live in this session. With no call it does nothing.
- **What it decides:** nothing. It returns the request unchanged (`next(e)`), so the outcome is
  whatever your permission settings and your own hooks decide. It never allows, denies or rewords a
  request, and never changes what a dialog shows. Approvals stay on your keyboard; a spoken "yes"
  never approves anything.
- **What it does instead:** writes the one file below, so the voice can say what Claude is asking for.
  That can include a request another hook or Claude Code then decides without asking you; sandbox
  network prompts are not covered.

### The one file the Mod writes

- **Path:** `<state directory>/<session id>/permission.json`, that is
  `~/.local/state/bettercallgpt/<session id>/permission.json` by default.
- **Content:** the time, the tool's name, a one-line summary and the call's instance id.
- **Who reads it:** the voice process of this call, to speak it once. No other tool runs or obeys it.
- The voice process writes its own files in that directory (a conversation ledger and status files);
  it is per-user and only you can open it (0700). They stay on your machine.

### Off switch

`"disableAllHooks": true` in your settings turns the Mod off (with all your hooks), or disable the
plugin in `/plugin`. The three commands keep working without it.

## Where it works

Claude Code on macOS: the CLI in any terminal, and the Code tab of the Claude desktop app. It needs a
Mac microphone, so it does nothing in Claude chat, Cowork, or Claude Code on the web.

## Why the key stays in a file

The voice service key lives in `~/.config/bettercallgpt/.env` rather than in plugin settings so that it
never enters a prompt, a command line or the model's context; only the local voice process reads it.

"GPT" names the voice model the call runs on. This project is not affiliated with OpenAI, Microsoft
or Anthropic.

Full guide, security notes and source: https://github.com/insta-fusion/bettercallgpt

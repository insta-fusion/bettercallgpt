---
name: bettercallgpt
description: Set up bettercallgpt, a full-duplex voice call for Claude Code — check uv and the platform, install the Claude Code plugin, prepare an empty key file, run the readiness check. Use when the user asks to set up, install, update or troubleshoot bettercallgpt or "voice" for their coding agent. Never starts a call.
---

# Set up bettercallgpt

The user wants to talk to their Claude Code session. Get them to the point where they type
`/bettercallgpt:on`, then stop. Work through the steps in order; when one fails, say what failed
and what the user can do, and stop — do not work around it.

## Rules

- **Never ask for, read out, echo or commit an API key**, and never open the key file after the
  user has filled it. You create it with empty values; the user types the key.
- **Never start a call, by any command.** Starting opens the microphone and a paid connection:
  only the user starts it, by typing `/bettercallgpt:on`.
- **Never allow-list** anything that could match the start: no `bettercallgpt` wildcard and no
  broad `uvx` rule (such as `Bash(uvx:*)`) in any settings file.
- Run each command below exactly as written, as its own Bash call, so the user sees and
  approves it.

## 1. uv

```sh
uv --version
```

If uv is missing, show the user the official installer and let them run it
(`curl -LsSf https://astral.sh/uv/install.sh | sh`; other methods: https://docs.astral.sh/uv/),
then continue. Nothing else needs installing: the next command fetches bettercallgpt and its
audio library.

## 2. Platform and config path

```sh
uvx --from git+https://github.com/insta-fusion/bettercallgpt@v0.1.1 bettercallgpt doctor
```

It prints JSON and starts nothing; exit code 1 only means "not ready yet" — read the JSON. If
`backend_problem` says the platform is unsupported (today only macOS runs the Claude Code
call), tell the user and stop. Note `env_file`: that is the key file's path.

## 3. The Claude Code plugin

```sh
claude plugin marketplace add insta-fusion/bettercallgpt
claude plugin install bettercallgpt@bettercallgpt
```

Updating instead? Run `claude plugin marketplace update bettercallgpt`, then
`claude plugin update bettercallgpt@bettercallgpt`. The commands `/bettercallgpt:on`, `:status` and
`:off` appear in **new** sessions (the desktop app needs a restart).

## 4. The key file

If the file at `env_file` does not exist, ask which voice service the user has — **Azure Voice
Live** (default, proven live, works on speakers) or **OpenAI Realtime** (experimental; use
headphones) — then create it readable only by the user, with empty values (replace `ENV_FILE`
with the path from step 2):

```sh
mkdir -p "$(dirname "ENV_FILE")" && (umask 077 && set -C && cat > "ENV_FILE" <<'EOF'
# Azure Voice Live (default)
AZURE_OPENAI_ENDPOINT=
AZURE_OPENAI_API_KEY=
# or OpenAI Realtime (experimental, headphones): uncomment both, leave the Azure lines empty
# VOICE_LIVE_PROVIDER=openai
# OPENAI_API_KEY=
EOF
)
```

(`set -C` makes the command refuse an existing file: never overwrite one.) Tell the user the
path and the lines to fill, and wait until they say it is done.

## 5. Check

Run the step 2 command again. Continue only when `"ready": true`; otherwise report the fields
that are not ready (missing credential *names*, audio devices) and stop.

## Done

Report in one short list: `doctor` ready or not, and how to start — the user types
`/bettercallgpt:on` in a new Claude Code session and approves the start (with OpenAI Realtime:
headphones). Permission prompts are answered on the keyboard; a spoken "yes" never approves
anything.

## Optional extras (offer, do not push)

- **Status line (terminal only).** `uvx --from git+https://github.com/insta-fusion/bettercallgpt@v0.1.1 bettercallgpt statusline`
  prints `🎙 voice` while this session's call is live. If
  `~/.claude/settings.json` has no `statusLine`, offer to set it to that command; if it has
  one, show how to append its output. After an update, the version in it must be updated too.
- **After `/compact`.** Offer to add the block in
  https://github.com/insta-fusion/bettercallgpt/blob/main/docs/HOST-INSTRUCTIONS.md to the
  user's `CLAUDE.md`, so voice turns stay understood.

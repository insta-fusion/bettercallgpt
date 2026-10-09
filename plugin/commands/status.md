---
description: "Show the voice call of this session: phase, relay"
disable-model-invocation: true
allowed-tools: Bash(uvx --from git+https://github.com/insta-fusion/bettercallgpt@v0.2.2 bettercallgpt status)
---

Run `uvx --from git+https://github.com/insta-fusion/bettercallgpt@v0.2.2 bettercallgpt status` once with your Bash tool (it exits 1 and prints `"phase": "absent"`
when no call has run in this session — that is an answer, not an error). Report `phase`
and `relay` in one line (and "reconnecting" when `reconnecting` is true). Run nothing else.

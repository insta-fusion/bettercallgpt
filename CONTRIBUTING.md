# Contributing

## Where code changes go

Everything lives in this repository and takes pull requests directly:

- `voice/` — the voice daemon (audio, the voice-model wire, the relay into your agent session)
  and its prompts in `voice/prompts/`. Changes here need a maintainer's review (CODEOWNERS).
- `bettercallgpt/` — the launcher, `plugin/` and `skills/` — the Claude Code plugin and setup skill.
- `tests/fixtures/` — recorded wire sessions the `voice/` suite replays; they carry no personal
  paths.

Changing a prompt's wording? Update the golden prompts in `tests/fixtures/voice-prompt-monolith-*.md`
in the same pull request; `voice/tests/test_prompts.py` compares against them.

## Running the tests

```sh
python -m unittest discover -s voice/tests -t . -p 'test_*.py'   # voice/ suite (POSIX)
python -m unittest discover -s tests -t . -p 'test_*.py'         # launcher
```

Both are offline and silent. One live smoke test (a real `codex exec`) runs only with
`VOICE_LIVE_TESTS=1`.

## Pull requests

One concern per PR, tests with the change, and a note on how you verified anything that
touches audio (the suites never open a device).

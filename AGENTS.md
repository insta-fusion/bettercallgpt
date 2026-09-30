# bettercallgpt — for coding agents

## A user asked you to set up bettercallgpt

Follow [skills/bettercallgpt/SKILL.md](skills/bettercallgpt/SKILL.md) — the same steps the
`npx skills add insta-fusion/bettercallgpt -g` skill carries. In short: check uv, install the
Claude Code plugin, create the key file with empty values for the user to fill, run
`doctor`. Never handle an API key and never start a call: the user types `/bettercallgpt:on`.

## Working on this repository

Everything is edited here (see [CONTRIBUTING.md](CONTRIBUTING.md)). The voice persona lives in
`voice/prompts/`; a wording change also updates the golden prompts in `tests/fixtures/`. The release tag in `plugin/commands/*.md` and `skills/bettercallgpt/SKILL.md`
(`@v<version>`) must match `pyproject.toml`; the tests check it.

```sh
python -m unittest discover -s voice/tests -t . -p 'test_*.py'
python -m unittest discover -s tests -t . -p 'test_*.py'
```

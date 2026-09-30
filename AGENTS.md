# bettercallgpt — for coding agents

## A user asked you to set up bettercallgpt

Follow [skills/bettercallgpt/SKILL.md](skills/bettercallgpt/SKILL.md) — the same steps the
`npx skills add insta-fusion/bettercallgpt -g` skill carries. In short: check uv, install the
Claude Code plugin, create the key file with empty values for the user to fill, run
`doctor`. Never handle an API key and never start a call: the user types `/bettercallgpt:on`.

## Working on this repository

`voice/` and `tests/fixtures/` are synced byte-for-byte from upstream: do not edit them here
(see [CONTRIBUTING.md](CONTRIBUTING.md)). Everything else — packaging, plugin, skill, docs — is
edited here. The release tag in `plugin/commands/*.md` and `skills/bettercallgpt/SKILL.md`
(`@v<version>`) must match `pyproject.toml`; the tests check it.

```sh
python -m unittest discover -s voice/tests -t . -p 'test_*.py'
python -m unittest discover -s tests -t . -p 'test_*.py'
python tools/sync_upstream.py check
```

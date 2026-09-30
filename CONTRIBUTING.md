# Contributing

## Where code changes go

`voice/` is **kept in sync with the maintainers' upstream repository**: its files are
byte-identical to the commit pinned in `UPSTREAM.json`; `tests/fixtures/` are the same files
after the declared scrubs in `tools/sync_upstream.py`. CI checks the tree against that
manifest (integrity, not provenance — a pin update must come from a maintainer's sync, which
CODEOWNERS enforces once branch protection requires code-owner review). Pull requests that touch `voice/` are welcome here: open them as usual; a
maintainer lands the change upstream and re-syncs, and your PR is closed with a link to the
synced commit (credit kept). Maintainers sync with:

```sh
python tools/sync_upstream.py sync --from /path/to/upstream --commit <sha>
python tools/sync_upstream.py check
```

Everything else — `bettercallgpt/` (the launcher), `tests/test_launcher.py`, docs, packaging
and CI — lives here and takes pull requests directly.

## Running the tests

```sh
python -m unittest discover -s voice/tests -t . -p 'test_*.py'   # upstream suite (POSIX)
python -m unittest discover -s tests -t . -p 'test_*.py'         # launcher + parity
```

Both are offline and silent. One live smoke test (a real `codex exec`) runs only with
`VOICE_LIVE_TESTS=1`.

## Pull requests

One concern per PR, tests with the change, and a note on how you verified anything that
touches audio (the suites never open a device).

# Fixtures

Every file here is opened by a test under `voice/tests/`; nothing else lives in this directory.

**Publishable by construction.** The recorded wires keep what the tests depend on — event order,
timing, fragment boundaries, id equality — and nothing that identifies a person or an account:
provider ids are remapped to synthetic ones (`*_SYN000001`, equality preserved everywhere),
deployment names are generic, the spike sessions were voiced from audio files, and wherever
the words were a real person's (the live GPT-Live session, the R0 spike's lines) they are
replaced by synthetic lines re-split into the same number of fragments/deltas — timestamps,
ids and every recognised-vs-interpreted mismatch the tests rely on untouched.

- `pane-raw.json` — the shape of an `orca terminal read --json` capture of a Claude Code pane:
  synthetic prose, with the structure the pane parser reads (glyphs, the running-command block
  with the nonce as its first token, the status line) as recorded
  (`test_backend_claude_code_pane.py`).
- `orca-terminal-show.json` — `orca terminal show --json` answers keyed as Orca renders them
  (shape measured on Orca 1.4.200, values synthetic): `agentWait` from each source, an evaluated
  `null`, the field absent (an older Orca), malformed values, and failed CLI calls
  (`test_backend_claude_code_pane.py`).
- `voice-rt-R0.jsonl`, `voice-rt-R2.jsonl`, `voice-rt-R6.jsonl`, `voice-rt-R11-D-mixed.jsonl` —
  recorded Azure Realtime spike sessions replayed by
  `test_wire_replay.py` (the strategy contract).
- `voice-gl-live-0923.jsonl`, `voice-gl-S2.jsonl`, `voice-gl-S5.jsonl`, `voice-gl-S6.jsonl` —
  recorded GPT-Live (`gpt-live-1`) sessions, audio stripped to byte counts: one live session
  with seven delegations (synthetic text) and the spike scenarios S2/S5/S6. Replayed by
  `test_wire_gpt_live.py` (span partition, barge-in, loop dispatch).
- `voice-rt-R4.jsonl` — the "unrelated 对" session; `test_core_broker.py` proves the consent
  broker never treats it as approval.
- `voice-prompt-monolith-realtime.md`, `voice-prompt-monolith-gpt-live.md` — the two persona prompts as
  single files before the modular split; `test_prompts.py` pins `voice.prompts.compose` to them by line set.

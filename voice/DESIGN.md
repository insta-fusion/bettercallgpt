# voice/ — design notes

The reasons behind the code, in one place. Module docstrings point here by section name
(`DESIGN.md §Timers`, `DESIGN.md §Acceptance C2`, …).

## Architecture

```
mic ─► LiveSession ─► Strategy ─► AgentLoop ─► Backend ─► the agent harness
        voice/live/     voice/live/   voice/agent/   voice/backend/
spk ◄─ AudioSink ◄──── VoicePort ◄──────┘   ▲
        voice/audio/    voice/live/port.py  └── Broker (voice/agent/broker.py): consent only
```

Every arrow is a protocol. `voice/app/daemon.py` is the only module that chooses concrete
parts (provider registry `voice/live/providers.py`, backend registry
`voice/backend/registry.py`, OS differences `voice/platform.py`). The voice model owns meaning;
the persona prompt lives in `voice/prompts/`. Code decides nothing about what a sentence
means — except consent (§Consent).

## Conversation model

- **The turn is the identity.** A request is one function call (or, on GPT-Live, one
  delegation) joined to exactly one committed input item and to a response the provider
  started for that item (`user` origin). A call with no unique candidate item is refused and
  the model is asked to repeat. Response origin comes from the provider's own attribution
  (a serialized create queue), never from arrival order alone.
- **Dispatch waits for the evidence.** A request is dispatched only after that input item's
  transcript completed. The backend receives both texts, labelled and unedited: `transcript`
  (raw ASR, evidence) and `interpretation` (the model's reading).
- **`priority`.** `now` interrupts the running turn where the harness supports it; `next` is a
  durable FIFO owned by the loop, released only when the working turn completes, and recovered
  as `queued` (never auto-released) after a crash.
- **Ack is not dispatch.** A spoken acknowledgement records no task; only a call/delegation does.
- **Results always enter.** Every backend result is injected into the conversation; whether an
  older result is still relevant is the model's judgement, not a harness revision counter.
  Backend progress enters as silent context (it never makes the voice speak by itself).
- **GPT-Live span rule.** `offset_ms` is a cursor, not a completeness claim: a transcript
  fragment belongs to the delegation whose `offset_ms` is the first cursor ≥ the fragment's
  start. The span is handed over verbatim as both `transcript` and `interpretation`; no second
  model writes an interpretation.

## Consent

The one deterministic gate. Measured reason: on the best-behaved provider the model approved
`rm -rf build/` on an unrelated 「对」 in four runs out of five.

1. **Armed by the harness, never the model.** An effect exists only when the backend reports an
   open dialog with an occurrence id, the harness's own prompt, and at least two options it can
   name by position. Nothing is parsed out of the wording. There is no consent tool.
2. **Spoken, and proven spoken.** The challenge reads the prompt verbatim and the options in
   rendered order, each with the phrase that picks it by POSITION (「确认选择第一个」,
   「确认选择第二个」…; 「确认取消选择」 cancels). Delivery needs all three for the challenge's
   response id: the response finished; its output transcript contains the full challenge text
   after normalization; local playback of that response reached its last frame with the audio
   epoch unchanged. Paraphrased, cut or cancelled → not delivered.
3. **The next utterance consumes the arm.** It executes only if that item's completed transcript,
   normalized, equals one of the armed phrases (each ≥ 4 syllables), the occurrence is
   unchanged and the effect id unused — and then presses the option at that position. Anything
   else refuses.
4. **Terminal-only** where delivery cannot be proven (a provider without response identity, such
   as GPT-Live) — and today for every backend: no backend has a key-press surface, so approvals
   stay on the keyboard.

## Audio epoch

- **Realtime family:** on the operator's speech start the strategy cancels the response; the
  epoch advances keyed by the cancelled response id, and no delta carrying that id reaches the
  speaker. `conversation.item.truncate` is sent only when exactly one audio item of that
  response rendered frames, with `audio_end_ms` = frames actually rendered.
- **GPT-Live:** no cancel on the wire. Interruption is local: the queued lead is flushed per
  speech segment on the server timeline; audio the model keeps generating after the flush is its
  own stop latency, reported rather than hidden.

## Timers

No timer participates in a semantic decision. The only durations are lifecycle ports declared in
`voice/app/daemon.py` and injected into the modules that use them: connection
open/close/reconnect (with the reconnect backoff and the provider-session rollover, which may only
open a fresh provider session), the audio-lock bound and cadence, pane read, relay connect, process
probe, the operator's session cap and idle close, the farewell bound, the dialog echo window
(one permission prompt seen by the pane and reported by the hooks module is said once; it decides
nothing else), the status heartbeat (`at` in status.json refreshed while the call runs, so a
reader can tell a live call from a dead writer's leftover), and the portable file-watch cadence (`voice/platform.py`, Linux/Windows). The rollover's QUIET
moment is not a timer: the loop reports it from events (`AgentLoop.until_quiet`: nobody speaking,
no unanswered call or delegation, no open response, the speaker drained). Forbidden, and grep-tested
(`test_core_timers.py`): fragment grace, confirmation windows, pause caps, response deadlines.

## Backend split

A backend observes one harness and actuates it; it decides no meaning.

- `claude_code/`: the **transcript** tailer reports what structurally happened (turn
  opened/ended, a tagged item was consumed); the **relay** writes the operator's words to the
  session's messaging socket after proving the peer against the session registry, per frame.
  Ownership is proven one of two ways, zero keystrokes either way:
  - **with an Orca pane** (`--terminal`): the pane is read (never typed into) and the launch
    nonce must appear inside a RUNNING tool-call block; dialogs become observable. Orca's own
    `agentWait` (`orca terminal show --json`) only ADDS: a wait it reports is heard even when
    the screen shows nothing enumerable (reported with no options, so announced and never
    armed); its "no wait" never removes a dialog the screen found. Wording and options come
    only from the screen; without a wait from Orca the screen decides alone.
  - **without a screen** (any terminal; the Claude desktop app is unverified): the launcher
    descends from the claude process the session registry names (alive, start time matching);
    the session's own transcript holds exactly one tool call carrying the nonce as a whole
    token — a main-thread `Bash` call written by the current claude process. What ties that
    call to THIS process is the ancestry, the fresh nonce and its single use (claimed once,
    atomically: an `O_EXCL` file under a fixed `~/.local/state/voice-launch-claims/<session>/`,
    whichever launcher or state root is used). The process's own environment (`NONCE=<n>`) and
    argv (`--nonce <n>`, parsed as `start` by the daemon's own parser) must agree with it —
    that catches mis-launches; it is not itself a proof. Command text is never parsed as shell.
    No dialog is observed on screen, so consent wording is terminal-only.
  - **Prompts reported by the session itself** (either binding): the plugin's hooks module
    (`plugin/hooks/register.tsx`) observes `classic.PermissionRequest` and, while the call is
    live, writes `permission.json` (`at`, `tool`, one-line `summary`) into the call's state
    directory. The control watcher reads it (deduped by `at`, never one older than the call, a
    half-written file read again when the write finishes; the file is rewritten in place, so it
    is watched itself, not only its directory) and the adapter announces it as a dialog `open`
    with NO options: narrated, never armable. With a pane, the screen is read first and the
    prompt is said once (`DIALOG_ECHO_S`). The hook only observes; nothing here answers it.
  - **Trust boundary.** Both proofs answer "which session started me", not "is this process
    friendly": any process descending from the session's claude (a hook, an MCP server) already
    holds the relay token. The proofs stop mis-binding, replay and cross-session binding.
  Classifying lines as "status" or "answer" was removed from this layer; presentation belongs to
  the voice model.
- `process.py`: any CLI agent that takes a prompt and emits JSON lines (`codex exec --json` is
  the shipped dialect).

## Acceptance

Wire-observable invariants, each held by named tests.

| id | invariant | tests |
|---|---|---|
| A1 | a dispatch carries input item → response → call identity, exactly one candidate | test_wire_replay, test_core_log |
| A2 | dispatch only after the item's transcript completed; both texts passed unedited | test_wire_strategy, test_core_log |
| A3 | every user turn ends in exactly one tool call (no silent acceptance) | test_wire_strategy |
| A4 | `next` does not interrupt; FIFO; recovered as queued after a crash | test_core_loop |
| A5 | a stop is `request(now)` and the relay frame carries `priority: now` | test_core_loop, test_backend_claude_code_relay |
| B1 | no delta of a cancelled response reaches the speaker | test_wire_replay, test_wire_realtime |
| B2 | truncate only with exactly one rendered audio item, at rendered frames | test_wire_strategy |
| B3 | no timer outside the declared lifecycle ports | test_core_timers |
| C1 | effects are armed only from harness dialogs | test_core_broker |
| C2 | delivery needs done + full challenge in the transcript + playback to the end | test_core_broker |
| C3 | the next item consumes the arm; only an exact phrase executes | test_core_broker |
| C4 | once per effect; occurrence change or reset refuses | test_core_broker |
| C5 | an unrelated 「对」 never executes (recorded wire) | test_core_broker |
| C6 | no response identity → terminal-only | test_core_broker, test_wire_gpt_live |
| D3 | an acknowledgement with no call records no task | test_core_log, test_core_loop |
| E1 | the nonce proves ownership with zero keystrokes (pane, or transcript without a screen) | test_backend_claude_code_pane, _screenless |
| E2 | a dialog reappearing is a new occurrence | test_backend_claude_code_pane, _adapter |
| E3 | receipts attribute to the consuming turn; results pass whole | test_backend_claude_code_adapter, _transcript |
| E4 | one writer, FIFO per tag on the relay | test_backend_claude_code_relay |
| E5 | transcript rotation survives; an op without outcome is `uncertain`, never replayed | test_backend_claude_code_transcript, test_backend_ledger |
| E6 | after owner loss the next actuation is refused | test_backend_claude_code_adapter, _pane, _relay |
| E7 | `process.py` drives a CLI child: send, progress, result, cancel | test_backend_process |
| E8 | a hook-reported prompt is read once, announced with no options (never armed), said once with a pane | test_app_daemon, test_backend_claude_code_permission |

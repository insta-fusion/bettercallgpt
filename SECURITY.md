# Security Policy

## Reporting a vulnerability

Use **GitHub private vulnerability reporting** (Security tab → "Report a vulnerability") on
this repository. Do not open a public issue for anything exploitable. You'll get an
acknowledgment within a week.

## What this project considers a vulnerability

- The daemon's **own credentials** (any env name containing KEY, SECRET, TOKEN, PASSWORD or
  CREDENTIAL) reaching its terminal output or error messages. `voice.config.redact_text`
  removes known values there; a bypass is a vulnerability.
- **Speech becoming consent.** Approvals are keyboard-only (the broker runs
  terminal-only); any path where a spoken utterance approves a permission prompt or an
  irreversible effect is a vulnerability.
- **The hooks module answering a request.** `plugin/hooks/register.tsx` observes
  `classic.PermissionRequest` only to write `permission.json` and always returns what the rest
  of the chain returned. Any path where it answers, alters or hides a permission request is a
  vulnerability.
- **The hooks module calling or steering without your press.** The module starts the voice
  process, and ends Claude's running turn, only from your press of `Call` / `Steer` (or `/call`,
  `/steer`). Any path where the module does either by itself, or on a spoken word, is a
  vulnerability.
- **A `--mod` start that Claude Code did not spawn.** That start skips the transcript check, so
  the voice process accepts it only when Claude Code itself spawned the command (its direct
  child, or `uvx`'s child under it). A script under a tool call that opens the microphone this
  way is a vulnerability. Known edge: a tool call whose own command line replaces its shell with
  this start; it goes through your permission settings like any other command.
- **Not a boundary: the local control commands.** `bettercallgpt stop` and `bettercallgpt steer`
  are ordinary commands for the session's own call: anything running as you can run them, and
  `steer` sends the words the call has heard and not handed over, ending Claude's running turn.
  When Claude runs one, your permission settings decide. They send only what you said to your
  own session, and approve nothing.
- **Relay to the wrong session.** The daemon attaches only to the session that launched it,
  with zero keystrokes, proven one of two ways. Without a screen (any terminal): it descends
  from that session's `claude` process, the session's own transcript holds exactly one fresh
  Bash call carrying the nonce, its own environment (`NONCE=`) agrees, and the nonce is
  claimed once. With `--terminal` in Orca (added by `bettercallgpt start` itself when that proof
  already holds in the pane it runs in): the nonce must appear inside a running tool-call
  block read from that session's pane; this path claims no nonce file. Any way to make it
  deliver speech into another session, or to keep delivering after the owner is gone, is a
  vulnerability.
- **A `.env` re-wiring the host.** The `.env` may carry secrets and endpoints, never
  harness-control names (`BETTERCALLGPT_*`, `VOICE_BUTLER_*` are refused from the file).

## Out of scope

- Compromise of your Azure/OpenAI account or key outside this code.
- The voice model mishearing you (the agent is told `⟨v#…⟩` lines are ASR and to ask when a
  load-bearing word is unclear).

## Known limitations (tracked)

- **The `⟨v#…⟩` tag is a convention, not authentication.** Any local process that can write to
  your session's messaging socket can send a line that ends in one. That is why the tag grants
  nothing: the agent acts on it only within the session's existing permissions, and approvals
  stay on the keyboard.
- **Masking covers known credentials only.** The daemon's own credential values are masked
  before any text reaches the voice provider or the ledger, and in its error output — values
  of 8 characters or more (shorter ones are not distinctive enough to replace safely). Other
  secrets that appear in your agent session (a token printed by a command, say) are not
  recognised: the provider hears them and the ledger records them.
- **Local files.** The state directory is 0700 and `ledger.jsonl` 0600; on Windows these
  modes are not enforced by the OS.

## Privacy notes

- The state directory (`~/.local/state/bettercallgpt/<session>/`) holds the status snapshot
  and the conversation ledger, which records what was relayed. It stays on your machine.
  During a call the plugin's hooks module also writes `permission.json` there: the newest
  permission request's tool name and a one-line summary (a Bash command, a file path or an MCP
  tool's name, left out when longer than 2000 characters) and the call's instance id, which the
  voice process reads to say what Claude is asking for. Known credential values are masked in the
  whole summary before it is shortened for speech.
- While a call runs, `status.json` also holds what the call console draws: the last 60
  characters you said that are not handed over yet (known credential values masked), the tags
  of spoken messages waiting in the session's queue, and the last Steer's result. `steer.json`
  holds one Steer press (an id, a time and the call's instance id).
- For launches without `--terminal`, each bound nonce is claimed once as an empty file under
  `~/.local/state/voice-launch-claims/<session>/` (a fixed location, whatever the state
  directory setting).
- The voice provider you configure (Azure Voice Live, the Azure GPT-Live API, or OpenAI
  Realtime, experimental) receives your microphone audio **and** the text the voice model needs
  to talk about the work: what you type into the session, the agent's progress and results,
  and permission requests (a one-line summary, so it can tell you what Claude is asking for).
  Treat the provider as seeing what your terminal shows.
- The test fixtures are recorded wire sessions with personal paths removed.

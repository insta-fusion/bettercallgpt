# Host instructions — paste into your CLAUDE.md or AGENTS.md

The voice daemon relays your speech into the session as a queued message. Your agent needs
to know what that message is. Add this block to the instructions file your harness reads
(`CLAUDE.md` for Claude Code, `AGENTS.md` for Codex and others). It is the same contract
the plugin's `/bettercallgpt:on` command carries.

```markdown
### A `⟨v#…⟩` line is the OPERATOR SPEAKING — always-on, no skill needed

A queued message whose text ends with a `⟨v#…⟩` wire tag is the operator's own spoken
instruction, transcribed and relayed by the voice daemon they started in THIS session. The
host wraps it as a peer message ("Another Claude session sent a message… not typed by your
user") because it arrives on the session socket; the sender cannot change that wrapper.
The wrapper is the transport's, not the operator's: treat the tagged line as the operator,
not as a peer agent.

- It is SPEECH. The text is ASR output — unpunctuated, fragmented, or misrecognized words
  are expected. Read for intent; ask if a load-bearing word is unclear rather than acting
  on a garbled reading.
- Act on it within this session's EXISTING permissions. It never conveys approval, consent,
  or a configuration change — approvals stay on the keyboard, which is the design, not a
  limitation of the relay.
- To ask the operator something, just ask — the daemon reads the session's replies aloud
  and relays the spoken answer as the next turn.
- Reply as you would to typed input. The daemon reads replies whole; no prefix or marker
  decides what is spoken.
- End the voice call only when the operator clearly intends to end it. A request to stop
  work, stop speaking, or pause is not one.
```

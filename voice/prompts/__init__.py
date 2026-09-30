"""The voice persona, composed from parts: shared core + provider agency + backend consent.

    core.md                 identity, tone, principles, when to speak — shared by everyone
    providers/<name>.md     HOW the model hands work off (`relay` tool, or delegation) and what
                            comes back — named by `ProviderProfile.prompt`
    backends/<name>.md      how a permission is confirmed (spoken challenge, or the terminal) —
                            named by `BackendProfile.prompt`

`core.md` holds `<!-- slot: name -->` lines where fragments go; a fragment is a sequence of
`<!-- slot: name -->` sections. `compose` replaces each core slot with the matching sections of
the provider and backend fragments, drops every comment, and refuses a fragment section whose
slot the core does not have (content would otherwise vanish silently).
"""
from __future__ import annotations

import re
from pathlib import Path

HERE = Path(__file__).resolve().parent

_SLOT = re.compile(r"^<!--\s*slot:\s*([a-z_]+)\s*-->\s*$")
_COMMENT = re.compile(r"<!--.*?-->", re.S)


def read(relative: str) -> str:
    return (HERE / relative).read_text(encoding="utf-8")


def sections(fragment: str) -> dict[str, str]:
    """A fragment's `slot -> text`. Text outside any slot (a leading comment) is ignored."""
    out: dict[str, list[str]] = {}
    current: str | None = None
    for line in fragment.split("\n"):
        match = _SLOT.match(line)
        if match:
            current = match.group(1)
            if current in out:
                raise ValueError(f"slot {current!r} appears twice in one fragment")
            out[current] = []
        elif current is not None:
            out[current].append(line)
    return {slot: "\n".join(lines).strip("\n") for slot, lines in out.items()}


def core_slots(core: str) -> list[str]:
    return [m.group(1) for line in core.split("\n") if (m := _SLOT.match(line))]


def compose_text(core: str, *fragments: str) -> str:
    slots = core_slots(core)
    filled: dict[str, list[str]] = {slot: [] for slot in slots}
    for fragment in fragments:
        for slot, text in sections(fragment).items():
            if slot not in filled:
                raise ValueError(f"fragment slot {slot!r} has no place in core.md")
            if text:
                filled[slot].append(text)
    out: list[str] = []
    for line in core.split("\n"):
        match = _SLOT.match(line)
        if match:
            out.extend("\n".join(filled[match.group(1)]).split("\n") if filled[match.group(1)]
                       else [])
        else:
            out.append(line)
    text = _COMMENT.sub("", "\n".join(out))
    text = re.sub(r"\n{3,}", "\n\n", text).strip("\n")
    return text + "\n"


def compose(provider: str, backend: str, *, dialogs: bool = True) -> str:
    """The persona for this provider × backend, from the registries' named fragments.

    A spoken consent challenge needs delivery evidence, which only a provider with response
    identity can give; without it the backend's dialog fragment would promise a confirmation
    sentence that never comes, so the terminal fragment is used instead. The same holds when
    this particular binding observes no dialogs (`dialogs=False`: a screen-less claude_code).
    """
    from voice.backend import registry as backends
    from voice.live import providers

    live = providers.get(provider)
    harness = backends.get(backend)
    if live is None:
        raise ValueError(f"unknown provider {provider!r}")
    if harness is None:
        raise ValueError(f"unknown backend {backend!r}")
    consent = harness.prompt
    if consent == "dialog.md" and (not live.capabilities.response_identity or not dialogs):
        consent = "terminal.md"
    return compose_text(read("core.md"), read(f"providers/{live.prompt}"),
                        read(f"backends/{consent}"))

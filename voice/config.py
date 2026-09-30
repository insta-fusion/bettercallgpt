"""Configuration: the environment snapshot every voice component reads, and redaction.

Two jobs and nothing else. (1) `.env` beside the bundle is read ONCE into a snapshot that never
shadows the real environment — the same secrets-only contract `agent_drivers.py` and
`gemini_native.py` use: `.env` may carry secrets and endpoint config, never policy or safety
gates, and harness-control names (`AGENT_DRIVERS_*`, `VOICE_BUTLER_*`) are refused from the file
so a checked-out file can never re-wire a host. (2) Anything derived from a key name is redacted
before it can reach a log, a transcript or a prompt.

No component reads `os.environ` directly; they call `cfg`. That is what makes the snapshot a
single testable surface instead of ambient state.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

# Names loaded from the file, keyed by name. Populated once by `load_env_file`; a name already
# present in the real environment is never overwritten by the file.
_ENV_FILE: dict[str, str] = {}
_LOADED = False

# Prefixes the file may not set: these steer the harness itself, not a provider.
_REFUSED_PREFIXES = ("AGENT_DRIVERS_", "VOICE_BUTLER_")

# Names whose VALUE is a secret. Anything matching is redacted wherever it would be printed.
_SECRET_MARKERS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "CREDENTIAL")

_REDACTED = "<redacted>"


def env_file_path() -> Path:
    """Where the snapshot is read from. `AGENT_DRIVERS_ENV_FILE` wins; otherwise the bundle root
    (this file's parent's parent), so a checkout and a deploy each read their own."""
    override = os.environ.get("AGENT_DRIVERS_ENV_FILE")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / ".env"


def load_env_file(path: str | Path | None = None, *, force: bool = False) -> dict[str, str]:
    """Read the `.env` snapshot. Idempotent: later calls are no-ops unless `force`.

    Parsing is deliberately dumb — `KEY=VALUE`, `#` comments, an inline `  #` comment stripped,
    one layer of surrounding quotes removed. No interpolation, no export syntax, no multi-line
    values: a config file that can compute is a config file that can surprise.
    """
    global _LOADED
    if _LOADED and not force:
        return _ENV_FILE
    if force:
        _ENV_FILE.clear()
    target = Path(path) if path is not None else env_file_path()
    try:
        text = target.read_text(encoding="utf-8")
    except Exception:
        # A missing or unreadable .env is the normal case on a machine configured by real
        # environment variables. It is never an error.
        _LOADED = True
        return _ENV_FILE
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = (part.strip() for part in line.split("=", 1))
        if not name or name.startswith(_REFUSED_PREFIXES):
            continue
        if "  #" in value:
            value = value.split("  #", 1)[0].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if name not in os.environ:
            _ENV_FILE[name] = value
    _LOADED = True
    return _ENV_FILE


def cfg(name: str, default: str = "") -> str:
    """The value of `name`: real environment first, then the file snapshot, then `default`."""
    load_env_file()
    return os.environ.get(name) or _ENV_FILE.get(name) or default


def redact_tree(value: Any) -> Any:
    """`redact_text` over every string leaf of a JSON-shaped value (dicts, lists, strings).
    Keys and non-string leaves pass unchanged. For records that leave the process or persist."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact_tree(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_tree(v) for v in value]
    return value


def is_secret_name(name: str) -> bool:
    """Whether a name's VALUE must never be printed. Name-based, not value-based: a heuristic on
    the value would leak the one key that happens not to look like one."""
    upper = name.upper()
    return any(marker in upper for marker in _SECRET_MARKERS)


def redact(name: str, value: str) -> str:
    """The printable form of one setting."""
    if not value:
        return ""
    return _REDACTED if is_secret_name(name) else value


def redact_text(text: str) -> str:
    """Remove every known secret VALUE from free text before it is logged or spoken.

    Values, not names: a stack trace or a provider error can echo a key without naming it.
    """
    load_env_file()
    values = set()
    for name in set(list(_ENV_FILE) + list(os.environ)):
        if not is_secret_name(name):
            continue
        value = os.environ.get(name) or _ENV_FILE.get(name) or ""
        # Short values are not distinctive enough to substring-replace safely.
        if len(value) >= 8 and value in text:
            values.add(value)
    if not values:
        return text
    # ONE pass, longest first: a secret that is a prefix of another must not be replaced
    # first and leave the longer one's tail behind.
    pattern = "|".join(re.escape(v) for v in sorted(values, key=len, reverse=True))
    return re.sub(pattern, _REDACTED, text)


def snapshot(names: tuple[str, ...]) -> dict[str, str]:
    """A printable view of the named settings — secrets already redacted. What a diagnostic
    prints, so no caller has to remember which names are sensitive."""
    return {name: redact(name, cfg(name)) for name in names}

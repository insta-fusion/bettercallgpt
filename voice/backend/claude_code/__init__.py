"""The Claude Code backend adapter: pane, transcript tailer, relay socket, translator."""
from __future__ import annotations

from .adapter import ClaudeCodeBackend, mint_tag, render_tag

__all__ = ["ClaudeCodeBackend", "mint_tag", "render_tag"]

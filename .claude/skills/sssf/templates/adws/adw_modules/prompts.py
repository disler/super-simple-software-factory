"""Prompt rendering: load system/user refs from config, replace {{placeholders}}."""

from __future__ import annotations

from pathlib import Path

from .diagnostics import sanitize_text


def render(template_path: str | Path, variables: dict[str, str]) -> str:
    text = Path(template_path).read_text()
    for key, value in variables.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def save(directory: str | Path, name: str, content: str) -> Path:
    """Save a redacted audit copy of a prompt without changing the sent prompt."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(sanitize_text(content))
    return path

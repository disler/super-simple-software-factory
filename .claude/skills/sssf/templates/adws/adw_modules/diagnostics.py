"""Sanitize diagnostic records without mutating the live protocol objects."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping

REDACTED = "[REDACTED]"
SHORT_SECRET_LENGTH = 5
SENSITIVE_ENV_KEY = re.compile(
    r"token|key|secret|password|passwd|auth|credential|cookie", re.I)
RICH_TAG = re.compile(r"(\[/?[a-z][^\]]*\])", re.I)
BEARER_VALUE = re.compile(r"\bbearer\s+[A-Za-z0-9._~+/=-]{6,}", re.I)
AUTHORIZATION = re.compile(
    r"\b(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+", re.I)
ASSIGNMENT = re.compile(
    r"\b((?:api[_-]?key|access[_-]?token|token|secret|password|passwd|"
    r"authorization|auth|credential|cookie)\s*[:=]\s*)(?:['\"])?[^\s,'\";]+", re.I)
URL_CREDENTIALS = re.compile(r"([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^@\s/]+@", re.I)
API_TOKEN = re.compile(
    r"\b(?:sk-(?:proj-)?[A-Za-z0-9_-]{8,}|sk-ant-[A-Za-z0-9_-]{8,}|"
    r"(?:gh[pousr][_-]|glpat-)[A-Za-z0-9_-]{8,}|xox[baprs]-[A-Za-z0-9-]{8,}|"
    r"(?:AKIA|ASIA)[A-Z0-9]{12,}|(?:sk|pk)_(?:live|test)_[A-Za-z0-9]{8,})\b")


def sanitize_diagnostic(value: object) -> object:
    """Return a recursively redacted diagnostic copy.

    Example: ``sanitize_diagnostic({"API_TOKEN": "x"})`` hides the value.
    """
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, dict):
        return _sanitize_mapping(value)
    if isinstance(value, list):
        return [sanitize_diagnostic(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_diagnostic(item) for item in value)
    return value


def sanitize_text(text: str) -> str:
    """Redact operator secrets from free-form diagnostic text.

    Example: ``sanitize_text("Authorization: Bearer value")`` hides the credential.
    """
    redacted = _redact_environment_values(text)
    redacted = URL_CREDENTIALS.sub(r"\1" + REDACTED + "@", redacted)
    redacted = AUTHORIZATION.sub(r"\1" + REDACTED, redacted)
    redacted = BEARER_VALUE.sub("Bearer " + REDACTED, redacted)
    redacted = ASSIGNMENT.sub(r"\1" + REDACTED, redacted)
    return API_TOKEN.sub(REDACTED, redacted)


def sanitize_json_text(text: str) -> str:
    """Redact a serialized JSON diagnostic, or treat malformed input as free text."""
    try:
        return json.dumps(sanitize_diagnostic(json.loads(text)), ensure_ascii=False)
    except json.JSONDecodeError:
        return sanitize_text(text)


def _sanitize_mapping(value: Mapping[object, object]) -> dict[str, object]:
    sanitized: dict[str, object] = {}
    for key, item in value.items():
        label = str(key)
        sanitized[label] = (REDACTED if _is_secret_entry(label, item)
                            else sanitize_diagnostic(item))
    return sanitized


def _is_secret_entry(key: str, value: object) -> bool:
    return isinstance(value, str) and bool(SENSITIVE_ENV_KEY.search(key))


def _redact_environment_values(text: str) -> str:
    redacted = text
    for secret in _environment_secrets():
        redacted = _redact_environment_value(redacted, secret)
    return redacted


def _environment_secrets() -> tuple[str, ...]:
    values = [value for key, value in os.environ.items()
              if _is_secret_entry(key, value) and value]
    unique = set(values)
    return tuple(sorted(unique, key=len, reverse=True))


def _redact_environment_value(text: str, secret: str) -> str:
    if len(secret) > SHORT_SECRET_LENGTH:
        return text.replace(secret, REDACTED)
    return _redact_short_environment_value(text, secret)


def _redact_short_environment_value(text: str, secret: str) -> str:
    pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(secret)}(?![A-Za-z0-9_])")
    parts = RICH_TAG.split(text)
    return "".join(part if RICH_TAG.fullmatch(part) else pattern.sub(REDACTED, part)
                   for part in parts)

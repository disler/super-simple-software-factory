"""Portable capability validation and native tool translation."""

from __future__ import annotations

from typing import cast

from .harness_types import (HarnessConfigError, HarnessName, HarnessProfile,
                            PortableCapability, ResolvedCapabilities,
                            SandboxAccess)

LEGACY_ALIASES = {
    "bash": "shell", "grep": "search", "find": "search", "ls": "search",
}
CLAUDE_TOOLS: dict[PortableCapability, tuple[str, ...]] = {
    "read": ("Read",),
    "search": ("Grep", "Glob"),
    "shell": ("Bash",),
    "write": ("Write",),
    "edit": ("Edit",),
}
CODEX_INSPECTION: frozenset[PortableCapability] = frozenset({"read", "search", "shell"})
CODEX_MUTATION: frozenset[PortableCapability] = frozenset(
    {"read", "search", "shell", "write", "edit"},
)
CAPABILITY_ORDER: tuple[PortableCapability, ...] = (
    "read", "search", "shell", "write", "edit",
)
PORTABLE_CAPABILITIES: frozenset[PortableCapability] = frozenset(CAPABILITY_ORDER)
PI_TOOLS: dict[PortableCapability, tuple[str, ...]] = {
    "read": ("read",),
    "search": ("grep", "find", "ls"),
    "shell": ("bash",),
    "write": ("write",),
    "edit": ("edit",),
}


def infer_sandbox(writes: list[str] | None) -> SandboxAccess:
    """Infer repository sandbox access from the configured write allowance.

    Example: ``infer_sandbox([])`` returns ``"read-only"``.
    """
    return "read-only" if writes == [] else "workspace-write"


def resolve_capabilities(profile: HarnessProfile) -> ResolvedCapabilities:
    """Validate a portable profile and return its native tool allowlist.

    Example: ``resolve_capabilities(profile).sandbox`` returns its access mode.
    """
    portable, extras = _resolve_tools(profile)
    _validate_profile(profile.harness, portable, profile.extensions)
    native_tools = _native_tools(profile.harness, portable, extras)
    return ResolvedCapabilities(portable=portable, native_tools=native_tools,
                                sandbox=_resolved_sandbox(profile, portable))


def _resolve_tools(
    profile: HarnessProfile,
) -> tuple[frozenset[PortableCapability], tuple[str, ...]]:
    if profile.tools is None:
        return frozenset(PORTABLE_CAPABILITIES), ()
    portable: set[PortableCapability] = set()
    extras: list[str] = []
    for tool in profile.tools:
        _add_tool(profile, tool, portable, extras)
    return frozenset(portable), tuple(extras)


def _add_tool(profile: HarnessProfile, tool: str, portable: set[PortableCapability],
              extras: list[str]) -> None:
    normalized = LEGACY_ALIASES.get(tool, tool)
    if normalized in PORTABLE_CAPABILITIES:
        portable.add(cast(PortableCapability, normalized))
        return
    if profile.harness == "pi" and profile.extensions:
        extras.append(tool)
        return
    raise HarnessConfigError(profile.harness, tool, "a portable capability or Pi extension tool")


def _native_tools(harness: HarnessName, portable: frozenset[PortableCapability],
                  extras: tuple[str, ...]) -> tuple[str, ...]:
    if harness == "claude_code":
        return _flatten_tools(portable, CLAUDE_TOOLS)
    if harness == "pi":
        return _flatten_tools(portable, PI_TOOLS) + extras
    return ()


def _flatten_tools(portable: frozenset[PortableCapability],
                   mapping: dict[PortableCapability, tuple[str, ...]]) -> tuple[str, ...]:
    names: list[str] = []
    for capability in CAPABILITY_ORDER:
        if capability in portable:
            names.extend(mapping[capability])
    return tuple(dict.fromkeys(names))


def _validate_profile(harness: HarnessName, portable: frozenset[PortableCapability],
                      extensions: list[str]) -> None:
    if harness == "pi":
        return
    if extensions:
        raise HarnessConfigError(harness, extensions, "no extension files")
    if harness == "codex" and portable not in (CODEX_INSPECTION, CODEX_MUTATION):
        raise HarnessConfigError(
            harness, sorted(portable), "inspection or mutation capability profile",
        )


def _resolved_sandbox(profile: HarnessProfile,
                      portable: frozenset[PortableCapability]) -> SandboxAccess:
    if profile.harness != "codex":
        return infer_sandbox(profile.writes)
    return _codex_sandbox(profile, portable)


def _codex_sandbox(profile: HarnessProfile,
                   portable: frozenset[PortableCapability]) -> SandboxAccess:
    if portable == CODEX_INSPECTION and profile.writes == []:
        return "read-only"
    if portable == CODEX_MUTATION and profile.writes != []:
        return "workspace-write"
    value = {"tools": sorted(portable), "writes": profile.writes}
    expected = ("Codex inspection tools [read, search, shell] with writes: [], "
                "or mutation tools [read, search, shell, write, edit] with writes omitted "
                "or a non-empty path list")
    raise HarnessConfigError("codex", value, expected)

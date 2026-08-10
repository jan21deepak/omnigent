"""Agent-declared harness settings extracted from :class:`AgentSpec`."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import TypeAlias, cast

from omnigent.spec.types import AgentSpec

_Settings: TypeAlias = dict[str, object]

# Claude Code ``settings.json`` keys owned by Omnigent at launch time.
CLAUDE_FRAMEWORK_OWNED_KEYS: frozenset[str] = frozenset({"hooks", "statusLine", "apiKeyHelper"})


def harness_settings_from_spec(spec: AgentSpec) -> _Settings | None:
    """Return agent-declared harness settings from ``executor.config``."""
    raw = spec.executor.config.get("harness_settings")
    if not isinstance(raw, dict):
        return None
    return cast(_Settings, raw)


def serialize_harness_settings(settings: Mapping[str, object]) -> str:
    """JSON-encode harness settings for spawn-env threading."""
    return json.dumps(settings, separators=(",", ":"))


def _deep_merge_settings(base: Mapping[str, object], overlay: Mapping[str, object]) -> _Settings:
    merged = dict(base)
    for key, value in overlay.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _deep_merge_settings(existing, value)
        else:
            merged[key] = value
    return merged


def merge_claude_settings(
    agent_settings: Mapping[str, object] | None,
    framework_settings: Mapping[str, object],
) -> _Settings:
    """
    Merge agent-declared Claude settings under Omnigent framework keys.

    Agent settings provide portable defaults; framework-owned keys
    (``hooks``, ``statusLine``, ``apiKeyHelper``) and other framework
    values win on conflict.
    """
    merged: _Settings = dict(agent_settings) if agent_settings else {}
    for key, value in framework_settings.items():
        if key in CLAUDE_FRAMEWORK_OWNED_KEYS:
            merged[key] = value
        elif isinstance(merged.get(key), dict) and isinstance(value, dict):
            merged[key] = _deep_merge_settings(
                cast(Mapping[str, object], merged[key]),
                value,
            )
        else:
            merged[key] = value
    return merged

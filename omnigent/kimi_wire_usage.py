"""Parse Kimi Code ``wire.jsonl`` usage and model records (schema 0.34.0).

Kimi Code persists per-step ``usage.record`` deltas and ``llm.request`` model
metadata to an append-only ``wire.jsonl`` under ``$KIMI_CODE_HOME/sessions/``.
Neither the headless ``stream-json`` stdout protocol nor the kimi-native
forwarder's conversation mirror reads these records today, so Omnigent sessions
never accumulate tokens or model attribution.

This module is the shared parser/accumulator used by
:mod:`omnigent.kimi_native_forwarder` (``external_session_usage`` /
``external_model_change`` posts) and :mod:`omnigent.inner.kimi_executor``
(``TurnComplete.usage`` from the wire tail). All paths are best-effort: missing
files, malformed lines, and schema drift log once and emit nothing — never fail
a turn.
"""

from __future__ import annotations

import contextlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omnigent.kimi_native_credentials import resolve_user_kimi_home

_logger = logging.getLogger(__name__)

# Wire schema pinned to Kimi Code 0.34.0 (see test fixtures).
_USAGE_RECORD_TYPE = "usage.record"
_LLM_REQUEST_TYPE = "llm.request"
_TURN_USAGE_SCOPE = "turn"
_KIMI_CODE_MODEL_PREFIX = "kimi-code/"


def normalize_kimi_model(model: str) -> str:
    """Strip the ``kimi-code/`` catalog prefix when present."""
    if model.startswith(_KIMI_CODE_MODEL_PREFIX):
        return model[len(_KIMI_CODE_MODEL_PREFIX) :]
    return model


def _non_negative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return 0


@dataclass
class KimiUsageDelta:
    """One parsed ``usage.record`` delta."""

    input_other: int = 0
    output: int = 0
    input_cache_read: int = 0
    input_cache_creation: int = 0
    model: str | None = None

    @property
    def has_tokens(self) -> bool:
        return bool(
            self.input_other or self.output or self.input_cache_read or self.input_cache_creation
        )

    @property
    def context_tokens(self) -> int:
        """Current context occupancy proxy for the step."""
        return self.input_other + self.input_cache_read + self.input_cache_creation


@dataclass
class KimiUsageAccumulator:
    """Running session totals for native ``external_session_usage`` posts."""

    cumulative_input_tokens: int = 0
    cumulative_output_tokens: int = 0
    cumulative_cache_read_input_tokens: int = 0
    latest_context_tokens: int = 0
    latest_model: str | None = None
    _warned_parse: bool = field(default=False, repr=False)

    def ingest(self, row: dict[str, object]) -> KimiUsageDelta | None:
        """Apply one wire row when it is a turn-scoped usage record."""
        delta = parse_usage_record(row)
        if delta is None:
            return None
        # Issue contract: cumulative_input_tokens = Σ(inputOther + inputCacheCreation).
        self.cumulative_input_tokens += delta.input_other + delta.input_cache_creation
        self.cumulative_output_tokens += delta.output
        self.cumulative_cache_read_input_tokens += delta.input_cache_read
        if delta.context_tokens > 0:
            self.latest_context_tokens = delta.context_tokens
        if delta.model:
            self.latest_model = delta.model
        return delta

    def external_session_usage_payload(self) -> dict[str, object] | None:
        """Build an ``external_session_usage`` body, or ``None`` when empty."""
        if not (
            self.cumulative_input_tokens
            or self.cumulative_output_tokens
            or self.cumulative_cache_read_input_tokens
        ):
            return None
        payload: dict[str, object] = {
            "cumulative_input_tokens": self.cumulative_input_tokens,
            "cumulative_output_tokens": self.cumulative_output_tokens,
        }
        if self.cumulative_cache_read_input_tokens:
            payload["cumulative_cache_read_input_tokens"] = self.cumulative_cache_read_input_tokens
        if self.latest_context_tokens > 0:
            payload["context_tokens"] = self.latest_context_tokens
        if self.latest_model:
            payload["model"] = self.latest_model
            with contextlib.suppress(Exception):
                from omnigent.llms.context_window import get_model_context_window

                payload["context_window"] = get_model_context_window(self.latest_model)
        return payload


def parse_usage_record(row: dict[str, object]) -> KimiUsageDelta | None:
    """Parse one ``usage.record`` wire row into a delta, or ``None`` to skip."""
    if row.get("type") != _USAGE_RECORD_TYPE:
        return None
    usage_scope = row.get("usageScope")
    if usage_scope is not None and usage_scope != _TURN_USAGE_SCOPE:
        return None
    usage_raw = row.get("usage")
    if not isinstance(usage_raw, dict):
        return None
    delta = KimiUsageDelta(
        input_other=_non_negative_int(usage_raw.get("inputOther")),
        output=_non_negative_int(usage_raw.get("output")),
        input_cache_read=_non_negative_int(usage_raw.get("inputCacheRead")),
        input_cache_creation=_non_negative_int(usage_raw.get("inputCacheCreation")),
    )
    raw_model = row.get("model")
    if isinstance(raw_model, str) and raw_model.strip():
        delta.model = normalize_kimi_model(raw_model.strip())
    if not delta.has_tokens:
        return None
    return delta


def parse_llm_request_model(row: dict[str, object]) -> str | None:
    """Return the provider-resolved model from an ``llm.request`` row."""
    if row.get("type") != _LLM_REQUEST_TYPE:
        return None
    raw_model = row.get("model")
    if isinstance(raw_model, str) and raw_model.strip():
        return normalize_kimi_model(raw_model.strip())
    return None


def find_kimi_wire_path(kimi_home: Path, session_id: str) -> Path | None:
    """Locate ``wire.jsonl`` for a kimi session id such as ``session_<uuid>``."""
    sessions_root = kimi_home / "sessions"
    if not sessions_root.is_dir():
        return None
    matches = [
        path
        for path in sessions_root.glob("*/session_*/agents/main/wire.jsonl")
        if path.is_file() and path.parent.parent.parent.name == session_id
    ]
    if not matches:
        return None
    if len(matches) > 1:
        _logger.warning(
            "kimi wire usage: session %s is ambiguous across workspaces; using newest",
            session_id,
        )
        matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0]


def read_wire_rows_from_byte_offset(
    wire_path: Path,
    byte_offset: int,
) -> tuple[list[dict[str, object]], int]:
    """Read complete JSONL rows from *byte_offset*, returning the new offset."""
    if byte_offset < 0:
        byte_offset = 0
    try:
        raw = wire_path.read_bytes()
    except OSError:
        return [], byte_offset
    if byte_offset > len(raw):
        byte_offset = 0
    chunk = raw[byte_offset:]
    if not chunk:
        return [], byte_offset
    rows: list[dict[str, object]] = []
    consumed = 0
    while True:
        newline = chunk.find(b"\n", consumed)
        if newline < 0:
            break
        line = chunk[consumed:newline].strip()
        consumed = newline + 1
        if not line or not line.startswith(b"{"):
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
    return rows, byte_offset + consumed


def sum_turn_usage_from_rows(
    rows: list[dict[str, object]],
    *,
    fallback_model: str | None = None,
) -> dict[str, Any] | None:
    """Sum turn-scoped ``usage.record`` deltas into a ``TurnComplete.usage`` dict."""
    input_tokens = 0
    output_tokens = 0
    cache_read = 0
    cache_creation = 0
    latest_context = 0
    latest_model: str | None = None
    for row in rows:
        delta = parse_usage_record(row)
        if delta is None:
            continue
        input_tokens += delta.input_other
        output_tokens += delta.output
        cache_read += delta.input_cache_read
        cache_creation += delta.input_cache_creation
        if delta.context_tokens > 0:
            latest_context = delta.context_tokens
        if delta.model:
            latest_model = delta.model
    if not (input_tokens or output_tokens or cache_read or cache_creation):
        return None
    total = input_tokens + output_tokens + cache_read + cache_creation
    usage: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_creation,
    }
    if latest_context > 0:
        usage["context_tokens"] = latest_context
    model = latest_model or fallback_model
    if model:
        usage["model"] = model
    return usage


def read_turn_usage_since_checkpoint(
    session_id: str,
    byte_offset: int,
    *,
    kimi_home: Path | None = None,
    fallback_model: str | None = None,
) -> tuple[dict[str, Any] | None, int]:
    """Best-effort turn usage from new ``wire.jsonl`` bytes since *byte_offset*."""
    home = kimi_home or resolve_user_kimi_home()
    wire_path = find_kimi_wire_path(home, session_id)
    if wire_path is None:
        return None, byte_offset
    rows, new_offset = read_wire_rows_from_byte_offset(wire_path, byte_offset)
    usage = sum_turn_usage_from_rows(rows, fallback_model=fallback_model)
    return usage, new_offset

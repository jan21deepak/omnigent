"""Unit tests for Kimi Code wire.jsonl usage/model parsing (schema 0.34.0)."""

from __future__ import annotations

import json
from pathlib import Path

from omnigent.kimi_wire_usage import (
    KimiUsageAccumulator,
    find_kimi_wire_path,
    normalize_kimi_model,
    parse_llm_request_model,
    parse_usage_record,
    read_turn_usage_since_checkpoint,
    read_wire_rows_from_byte_offset,
    sum_turn_usage_from_rows,
)

# Fixture rows pinned to Kimi Code 0.34.0 wire schema.
_USAGE_STEP_1 = {
    "type": "usage.record",
    "model": "kimi-code/system.ai.kimi-k3",
    "usageScope": "turn",
    "usage": {
        "inputOther": 1200,
        "output": 42,
        "inputCacheRead": 800,
        "inputCacheCreation": 0,
    },
}
_USAGE_STEP_2 = {
    "type": "usage.record",
    "model": "kimi-code/system.ai.kimi-k3",
    "usageScope": "turn",
    "usage": {
        "inputOther": 300,
        "output": 18,
        "inputCacheRead": 1200,
        "inputCacheCreation": 50,
    },
}
_LLM_REQUEST = {
    "type": "llm.request",
    "model": "system.ai.kimi-k3",
    "modelAlias": "kimi-k3",
    "provider": "system",
    "maxTokens": 8192,
}
_SESSION_SCOPE = {
    "type": "usage.record",
    "usageScope": "session",
    "usage": {"inputOther": 999, "output": 999, "inputCacheRead": 0, "inputCacheCreation": 0},
}


class TestParseUsageRecord:
    def test_parses_turn_scoped_delta(self) -> None:
        delta = parse_usage_record(_USAGE_STEP_1)
        assert delta is not None
        assert delta.input_other == 1200
        assert delta.output == 42
        assert delta.input_cache_read == 800
        assert delta.model == "system.ai.kimi-k3"

    def test_strips_kimi_code_model_prefix(self) -> None:
        delta = parse_usage_record(_USAGE_STEP_1)
        assert delta is not None
        assert delta.model == normalize_kimi_model("kimi-code/system.ai.kimi-k3")

    def test_ignores_session_scope(self) -> None:
        assert parse_usage_record(_SESSION_SCOPE) is None

    def test_ignores_zero_usage(self) -> None:
        row = {
            "type": "usage.record",
            "usageScope": "turn",
            "usage": {
                "inputOther": 0,
                "output": 0,
                "inputCacheRead": 0,
                "inputCacheCreation": 0,
            },
        }
        assert parse_usage_record(row) is None


class TestParseLlmRequest:
    def test_returns_model(self) -> None:
        assert parse_llm_request_model(_LLM_REQUEST) == "system.ai.kimi-k3"

    def test_ignores_other_rows(self) -> None:
        assert parse_llm_request_model({"type": "turn.prompt"}) is None


class TestUsageAccumulator:
    def test_cumulative_and_context_payload(self) -> None:
        acc = KimiUsageAccumulator()
        acc.ingest(_USAGE_STEP_1)
        acc.ingest(_USAGE_STEP_2)
        payload = acc.external_session_usage_payload()
        assert payload is not None
        # Σ(inputOther + inputCacheCreation) = (1200+0) + (300+50)
        assert payload["cumulative_input_tokens"] == 1550
        assert payload["cumulative_output_tokens"] == 60
        assert payload["cumulative_cache_read_input_tokens"] == 2000
        assert payload["context_tokens"] == 1550  # latest step occupancy
        assert payload["model"] == "system.ai.kimi-k3"


class TestSumTurnUsage:
    def test_sums_turn_deltas(self) -> None:
        usage = sum_turn_usage_from_rows([_USAGE_STEP_1, _USAGE_STEP_2])
        assert usage is not None
        assert usage["input_tokens"] == 1500
        assert usage["output_tokens"] == 60
        assert usage["cache_read_input_tokens"] == 2000
        assert usage["cache_creation_input_tokens"] == 50
        assert usage["context_tokens"] == 1550
        assert usage["model"] == "system.ai.kimi-k3"


class TestWireTail:
    def _wire(self, tmp_path: Path, session_id: str) -> Path:
        wire = tmp_path / "sessions" / "wd_test" / session_id / "agents" / "main" / "wire.jsonl"
        wire.parent.mkdir(parents=True, exist_ok=True)
        rows = [_LLM_REQUEST, _USAGE_STEP_1, _USAGE_STEP_2]
        wire.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        return wire

    def test_find_wire_by_session_id(self, tmp_path: Path) -> None:
        session_id = "session_abc123"
        wire = self._wire(tmp_path, session_id)
        found = find_kimi_wire_path(tmp_path, session_id)
        assert found == wire

    def test_byte_offset_is_resume_safe(self, tmp_path: Path) -> None:
        session_id = "session_resume"
        wire = self._wire(tmp_path, session_id)
        first_rows, offset1 = read_wire_rows_from_byte_offset(wire, 0)
        assert len(first_rows) == 3
        second_rows, offset2 = read_wire_rows_from_byte_offset(wire, offset1)
        assert second_rows == []
        assert offset2 == offset1
        wire.write_text(
            wire.read_text(encoding="utf-8") + json.dumps(_USAGE_STEP_1) + "\n",
            encoding="utf-8",
        )
        appended, _offset3 = read_wire_rows_from_byte_offset(wire, offset2)
        assert len(appended) == 1

    def test_read_turn_usage_since_checkpoint(self, tmp_path: Path) -> None:
        session_id = "session_turn"
        self._wire(tmp_path, session_id)
        usage, new_offset = read_turn_usage_since_checkpoint(
            session_id,
            0,
            kimi_home=tmp_path,
        )
        assert usage is not None
        assert usage["output_tokens"] == 60
        assert new_offset > 0
        # Second read with updated offset yields nothing new.
        usage2, _ = read_turn_usage_since_checkpoint(
            session_id,
            new_offset,
            kimi_home=tmp_path,
        )
        assert usage2 is None

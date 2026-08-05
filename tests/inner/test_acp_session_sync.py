"""Tests for ACP external-turn reconciliation.

Two layers:

* **Unit** — the mapping store, replay folding, the missed-turn delta (cursor +
  dedupe), and the rendered backfill block.
* **Hermetic e2e** — a fake ACP agent that keeps a persistent, file-backed
  session and supports ``session/load``. It stands in for a Gateway session that
  other clients keep driving: turns are appended to its transcript while
  Omnigent is "offline", and a fresh executor (a restarted Omnigent process)
  re-opens the session and must surface exactly those turns, exactly once.
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest

from omnigent.inner.acp_executor import AcpAgentConfig, AcpExecutor
from omnigent.inner.acp_session_sync import (
    DIVERGENCE_NOTICE,
    AcpSessionStore,
    ExternalTurn,
    HistoryReplay,
    SessionRecord,
    default_store_path,
    format_backfill,
    missed_turns,
)
from omnigent.inner.executor import TextChunk, TurnComplete

# ---------------------------------------------------------------------------
# Session store
# ---------------------------------------------------------------------------


def test_store_roundtrip(tmp_path: Path) -> None:
    store = AcpSessionStore(tmp_path / "sessions.json")
    store.save("conv-1", SessionRecord(session_id="gw-1", command="openclaw acp", cwd="/w"))
    record = store.load("conv-1")
    assert record is not None
    assert record.session_id == "gw-1"
    assert record.cursor == 0
    assert record.updated_at > 0


def test_store_scopes_record_to_command_and_cwd(tmp_path: Path) -> None:
    store = AcpSessionStore(tmp_path / "sessions.json")
    store.save("conv-1", SessionRecord(session_id="gw-1", command="openclaw acp", cwd="/w"))
    record = store.load("conv-1")
    assert record is not None
    assert record.matches(command="openclaw acp", cwd="/w")
    assert not record.matches(command="goose acp", cwd="/w")
    assert not record.matches(command="openclaw acp", cwd="/other")


def test_store_delete_and_missing_key(tmp_path: Path) -> None:
    store = AcpSessionStore(tmp_path / "sessions.json")
    store.save("conv-1", SessionRecord(session_id="gw-1", command="c", cwd="/w"))
    store.delete("conv-1")
    store.delete("conv-1")  # idempotent
    assert store.load("conv-1") is None
    assert store.load("never-written") is None


def test_store_survives_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "sessions.json"
    path.write_text("{not json", encoding="utf-8")
    store = AcpSessionStore(path)
    assert store.load("conv-1") is None
    store.save("conv-1", SessionRecord(session_id="gw-1", command="c", cwd="/w"))
    assert store.load("conv-1") is not None


def test_store_ignores_malformed_entry(tmp_path: Path) -> None:
    path = tmp_path / "sessions.json"
    path.write_text(json.dumps({"conv-1": {"command": "c", "cwd": "/w"}}), encoding="utf-8")
    assert AcpSessionStore(path).load("conv-1") is None


def test_default_store_path_honors_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HARNESS_ACP_SESSION_STORE", str(tmp_path / "custom.json"))
    assert default_store_path() == tmp_path / "custom.json"


def test_default_store_path_under_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("HARNESS_ACP_SESSION_STORE", raising=False)
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    assert default_store_path() == tmp_path / "acp" / "sessions.json"


# ---------------------------------------------------------------------------
# Replay folding
# ---------------------------------------------------------------------------


def _chunk(kind: str, text: str, **extra: object) -> dict[str, object]:
    return {"sessionUpdate": kind, "content": {"type": "text", "text": text}, **extra}


def test_replay_folds_consecutive_chunks_into_turns() -> None:
    replay = HistoryReplay()
    replay.add_update(_chunk("user_message_chunk", "what "))
    replay.add_update(_chunk("user_message_chunk", "time?"))
    replay.add_update(_chunk("agent_message_chunk", "noon"))
    replay.add_update(_chunk("agent_thought_chunk", "thinking"))  # not transcript content
    assert replay.turns() == (
        ExternalTurn(role="user", text="what time?"),
        ExternalTurn(role="assistant", text="noon"),
    )


def test_replay_reads_attribution_from_meta() -> None:
    replay = HistoryReplay()
    replay.add_update(_chunk("user_message_chunk", "hi", _meta={"source": "OpenClaw Control UI"}))
    assert replay.turns() == (ExternalTurn(role="user", text="hi", source="OpenClaw Control UI"),)


def test_replay_ignores_empty_and_unknown_updates() -> None:
    replay = HistoryReplay()
    replay.add_update({"sessionUpdate": "usage_update", "size": 1000})
    replay.add_update(_chunk("user_message_chunk", ""))
    assert replay.turns() == ()


# ---------------------------------------------------------------------------
# Missed-turn delta
# ---------------------------------------------------------------------------


def _turns(*pairs: tuple[str, str]) -> list[ExternalTurn]:
    return [ExternalTurn(role=role, text=text) for role, text in pairs]


def test_missed_turns_skips_turns_already_in_omnigent() -> None:
    replayed = _turns(("user", "hi"), ("assistant", "hello"), ("user", "asked elsewhere"))
    local = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    missed, cursor = missed_turns(replayed, local)
    assert missed == (ExternalTurn(role="user", text="asked elsewhere"),)
    assert cursor == 3


def test_missed_turns_matches_prefixed_first_user_turn() -> None:
    """Omnigent folds the system prompt into the first prompt it sends."""
    replayed = _turns(("user", "you are a bot\n\nhi"), ("assistant", "hello"))
    local = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    assert missed_turns(replayed, local)[0] == ()


def test_missed_turns_respects_cursor() -> None:
    replayed = _turns(("user", "external"), ("assistant", "reply"))
    missed, cursor = missed_turns(replayed, [], cursor=2)
    assert missed == ()
    assert cursor == 2


def test_missed_turns_keeps_order_and_repeated_text() -> None:
    replayed = _turns(("user", "ping"), ("user", "ping"), ("assistant", "pong"))
    local = [{"role": "user", "content": "ping"}]
    missed, _ = missed_turns(replayed, local)
    assert missed == (
        ExternalTurn(role="user", text="ping"),
        ExternalTurn(role="assistant", text="pong"),
    )


def test_missed_turns_reads_block_content() -> None:
    replayed = _turns(("user", "hi"))
    local = [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}]
    assert missed_turns(replayed, local)[0] == ()


# ---------------------------------------------------------------------------
# Backfill rendering
# ---------------------------------------------------------------------------


def test_format_backfill_attributes_each_turn() -> None:
    rendered = format_backfill(
        [
            ExternalTurn(role="user", text="deploy?", source="OpenClaw Control UI"),
            ExternalTurn(role="assistant", text="done"),
        ]
    )
    assert "Synced 2 turns" in rendered
    assert "**user · OpenClaw Control UI:** deploy?" in rendered
    assert "**assistant · external:** done" in rendered


def test_format_backfill_empty_is_blank() -> None:
    assert format_backfill([]) == ""


def test_format_backfill_truncates_long_turns() -> None:
    rendered = format_backfill([ExternalTurn(role="assistant", text="x" * 5000)])
    assert "…" in rendered
    assert len(rendered) < 2500


# ---------------------------------------------------------------------------
# Executor wiring (mocked transport)
# ---------------------------------------------------------------------------


def _executor(tmp_path: Path, **kwargs: object) -> AcpExecutor:
    ex = AcpExecutor(
        AcpAgentConfig(command="openclaw acp", name="OpenClaw", **kwargs),  # type: ignore[arg-type]
        cwd=str(tmp_path),
    )
    ex._session_store = AcpSessionStore(tmp_path / "sessions.json")
    ex._conversation_id = "conv-1"
    return ex


@pytest.mark.asyncio
async def test_session_new_persists_the_mapping(tmp_path: Path) -> None:
    ex = _executor(tmp_path)

    async def fake_rpc(method, params, timeout=30.0):
        return {"result": {"sessionId": "gw-1"}}

    ex._rpc = fake_rpc  # type: ignore[assignment]
    assert await ex._ensure_session() == "gw-1"
    record = ex._session_store.load("conv-1")
    assert record is not None and record.session_id == "gw-1"


@pytest.mark.asyncio
async def test_reconnect_loads_the_stored_session(tmp_path: Path) -> None:
    ex = _executor(tmp_path)
    ex._load_session_supported = True
    ex._session_store.save(
        "conv-1", SessionRecord(session_id="gw-1", command="openclaw acp", cwd=str(tmp_path))
    )
    calls: list[tuple[str, dict]] = []

    async def fake_rpc(method, params, timeout=30.0):
        calls.append((method, params))
        return {"result": None}

    ex._rpc = fake_rpc  # type: ignore[assignment]
    assert await ex._ensure_session() == "gw-1"
    assert [method for method, _ in calls] == ["session/load"]
    assert calls[0][1]["sessionId"] == "gw-1"
    # The re-opened session still holds the context, so nothing is replayed.
    assert ex._system_prompt_sent is True


@pytest.mark.asyncio
async def test_rejected_load_falls_back_to_a_new_session(tmp_path: Path) -> None:
    ex = _executor(tmp_path)
    ex._load_session_supported = True
    ex._session_store.save(
        "conv-1", SessionRecord(session_id="gone", command="openclaw acp", cwd=str(tmp_path))
    )
    methods: list[str] = []

    async def fake_rpc(method, params, timeout=30.0):
        methods.append(method)
        if method == "session/load":
            return {"error": {"message": "Session not found"}}
        return {"result": {"sessionId": "gw-2"}}

    ex._rpc = fake_rpc  # type: ignore[assignment]
    assert await ex._ensure_session() == "gw-2"
    assert methods == ["session/load", "session/new"]
    record = ex._session_store.load("conv-1")
    assert record is not None and record.session_id == "gw-2"


@pytest.mark.asyncio
async def test_agent_without_load_support_flags_divergence(tmp_path: Path) -> None:
    ex = _executor(tmp_path)
    ex._load_session_supported = False
    ex._session_store.save(
        "conv-1", SessionRecord(session_id="gw-1", command="openclaw acp", cwd=str(tmp_path))
    )

    async def fake_rpc(method, params, timeout=30.0):
        assert method == "session/new"
        return {"result": {"sessionId": "gw-2"}}

    ex._rpc = fake_rpc  # type: ignore[assignment]
    assert await ex._ensure_session() == "gw-2"
    assert ex._divergence_pending is True
    assert DIVERGENCE_NOTICE in ex._reconcile_external_turns([])
    # Surfaced once, not on every later turn.
    assert ex._reconcile_external_turns([]) == ""


@pytest.mark.asyncio
async def test_reconciliation_disabled_never_loads_or_persists(tmp_path: Path) -> None:
    ex = _executor(tmp_path, reconcile_external_turns=False)
    ex._load_session_supported = True
    ex._session_store.save(
        "conv-1", SessionRecord(session_id="gw-1", command="openclaw acp", cwd=str(tmp_path))
    )
    methods: list[str] = []

    async def fake_rpc(method, params, timeout=30.0):
        methods.append(method)
        return {"result": {"sessionId": "gw-2"}}

    ex._rpc = fake_rpc  # type: ignore[assignment]
    assert await ex._ensure_session() == "gw-2"
    assert methods == ["session/new"]
    record = ex._session_store.load("conv-1")
    assert record is not None and record.session_id == "gw-1"  # untouched


def test_reconcile_marks_turns_reconciled_once(tmp_path: Path) -> None:
    ex = _executor(tmp_path)
    ex._session_id = "gw-1"
    ex._replayed_turns = (ExternalTurn(role="user", text="from the control ui"),)
    rendered = ex._reconcile_external_turns([])
    assert "from the control ui" in rendered
    assert ex._reconcile_external_turns([]) == ""
    record = ex._session_store.load("conv-1")
    assert record is not None and record.cursor == 1


def test_forget_session_drops_the_mapping(tmp_path: Path) -> None:
    ex = _executor(tmp_path)
    ex._session_store.save(
        "conv-1", SessionRecord(session_id="gw-1", command="openclaw acp", cwd=str(tmp_path))
    )
    ex._session_id = "gw-1"
    ex._forget_session()
    assert ex._session_id is None
    assert ex._session_store.load("conv-1") is None


def test_harness_wrap_reconcile_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnigent.inner import acp_harness

    monkeypatch.setenv("HARNESS_ACP_COMMAND", "openclaw acp")
    monkeypatch.delenv("HARNESS_ACP_RECONCILE", raising=False)
    assert acp_harness._build_acp_executor()._config.reconcile_external_turns is True  # type: ignore[attr-defined]
    monkeypatch.setenv("HARNESS_ACP_RECONCILE", "0")
    assert acp_harness._build_acp_executor()._config.reconcile_external_turns is False  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Hermetic end-to-end: a persistent, file-backed ACP session other clients drive
# ---------------------------------------------------------------------------

# The transcript lives in a JSON file, so it survives the agent process the same
# way a Gateway session survives Omnigent — and a test can append the turns
# another client made while Omnigent was disconnected.
_FAKE_GATEWAY_AGENT = r"""
import sys, json
from pathlib import Path

transcript = Path(sys.argv[1])

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

def read():
    return json.loads(transcript.read_text()) if transcript.exists() else []

def append(role, text):
    entries = read()
    entries.append({"role": role, "text": text})
    transcript.write_text(json.dumps(entries))

def chunk(sid, kind, text):
    send({"jsonrpc": "2.0", "method": "session/update",
          "params": {"sessionId": sid, "update": {
              "sessionUpdate": kind, "content": {"type": "text", "text": text}}}})

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    mid, method = msg.get("id"), msg.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": 1,
            "agentCapabilities": {"loadSession": True,
                                  "promptCapabilities": {"image": False}},
        }})
    elif method == "session/new":
        if not transcript.exists():
            transcript.write_text("[]")
        send({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": "gw-1"}})
    elif method == "session/load":
        sid = msg["params"]["sessionId"]
        for entry in read():
            kind = "user_message_chunk" if entry["role"] == "user" else "agent_message_chunk"
            chunk(sid, kind, entry["text"])
        send({"jsonrpc": "2.0", "id": mid, "result": None})
    elif method == "session/prompt":
        sid = msg["params"]["sessionId"]
        text = "".join(b.get("text", "") for b in msg["params"]["prompt"])
        append("user", text)
        reply = "reply to " + text.splitlines()[-1]
        append("assistant", reply)
        chunk(sid, "agent_message_chunk", reply)
        send({"jsonrpc": "2.0", "id": mid, "result": {"stopReason": "end_turn"}})
"""


async def _run(ex: AcpExecutor, prompt: str, history: list[dict]) -> str:
    """Drive one turn, returning the concatenated assistant text."""
    messages = [*history, {"role": "user", "content": prompt}]
    text: list[str] = []
    completed = False
    async for event in ex.run_turn(messages, [], "you are a bot"):
        if isinstance(event, TextChunk):
            text.append(event.text)
        completed = completed or isinstance(event, TurnComplete)
    assert completed
    return "".join(text)


@pytest.mark.asyncio
async def test_external_turns_are_backfilled_once_across_a_restart(tmp_path: Path) -> None:
    agent_path = tmp_path / "fake_gateway_agent.py"
    agent_path.write_text(_FAKE_GATEWAY_AGENT)
    transcript = tmp_path / "transcript.json"
    command = shlex.join([sys.executable, str(agent_path), str(transcript)])
    store_path = tmp_path / "sessions.json"

    def new_executor() -> AcpExecutor:
        ex = AcpExecutor(AcpAgentConfig(command=command, name="OpenClaw"), cwd=str(tmp_path))
        ex._session_store = AcpSessionStore(store_path)
        ex._conversation_id = "conv-1"
        return ex

    # 1. Start the Gateway-backed conversation from Omnigent.
    first = new_executor()
    try:
        assert await _run(first, "hello", []) == "reply to hello"
    finally:
        await first.close()
    history = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "reply to hello"},
    ]

    # 2-3. While Omnigent is down, another client adds a turn and gets a reply.
    entries = json.loads(transcript.read_text())
    entries += [
        {"role": "user", "text": "what did you change?"},
        {"role": "assistant", "text": "renamed the module"},
    ]
    transcript.write_text(json.dumps(entries))

    # 4. Reconnect: the mapping survived, so the same session is re-opened and
    #    both missed turns are surfaced exactly once, in order.
    second = new_executor()
    try:
        text = await _run(second, "and then?", history)
    finally:
        await second.close()
    assert text.index("what did you change?") < text.index("renamed the module")
    assert text.count("what did you change?") == 1
    assert "Synced 2 turns" in text  # the Omnigent-originated turns are not repeated
    assert text.endswith("reply to and then?")

    # 5-6. A later reconnect neither re-surfaces them nor starts a new session.
    history += [
        {"role": "assistant", "content": text},
        {"role": "user", "content": "and then?"},
    ]
    third = new_executor()
    try:
        final = await _run(third, "anything else?", history)
    finally:
        await third.close()
    assert "what did you change?" not in final
    assert final == "reply to anything else?"
    record = AcpSessionStore(store_path).load("conv-1")
    assert record is not None and record.session_id == "gw-1"

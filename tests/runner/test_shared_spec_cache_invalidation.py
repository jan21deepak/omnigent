"""The runner-wide agent spec cache must not publish a superseded bundle.

``_spec_cache`` in ``omnigent/runner/app.py`` is keyed by agent ID and shared by
every conversation on a runner app. A resolution that fetched an old bundle can
finish after ``/agent-cache/reset`` removed the entry; an unconditional write
then reinstalls the stale bundle for every later reader, because the eager MCP
path reads the shared cache before the session cache.

Invariant under test: once an invalidation of agent A completes, no later read
of the shared cache returns a bundle resolved before that invalidation.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from omnigent.runner import create_runner_app
from omnigent.spec.types import AgentSpec, ExecutorSpec, MCPServerConfig
from tests.runner.conftest import (
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
    _sse,
)
from tests.runner.helpers import NullServerClient

AGENT_ID = "ag_shared"
CONV_SLOW = "conv_slow"
CONV_LATER = "conv_later"


def _old_spec() -> AgentSpec:
    """Bundle version 1: carries an MCP server, so a reader issues ``tools/list``."""
    return AgentSpec(
        spec_version=1,
        name="v1",
        executor=ExecutorSpec(type="omnigent", config={"harness": "runner-test-default"}),
        mcp_servers=[MCPServerConfig(name="old", url="https://mcp.example.com/sse")],
    )


def _new_spec() -> AgentSpec:
    """Bundle version 2: the MCP server was removed by the session edit."""
    return AgentSpec(
        spec_version=1,
        name="v2",
        executor=ExecutorSpec(type="omnigent", config={"harness": "runner-test-default"}),
    )


class _McpRecordingServer(NullServerClient):
    """Records which sessions issued an MCP ``tools/list`` through the proxy."""

    def __init__(self) -> None:
        self.mcp_sessions: list[str] = []

    async def post(self, url: str, **kwargs: Any) -> Any:
        if url.endswith("/mcp"):
            self.mcp_sessions.append(url.split("/v1/sessions/")[1].removesuffix("/mcp"))

            class _Resp(NullServerClient._Response):
                def json(self) -> dict[str, Any]:
                    return {"result": {"tools": []}}

            return _Resp()
        return await super().post(url, **kwargs)


@pytest.mark.asyncio
async def test_late_resolution_does_not_republish_invalidated_spec() -> None:
    """A resolution in flight during a reset must not repopulate the shared cache."""
    calls: list[str] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del session_id
        assert agent_id == AGENT_ID
        calls.append(agent_id)
        if len(calls) == 1:
            return _old_spec()
        if len(calls) == 2:
            # The eager turn-spec resolution: it fetched version 1 and is still
            # in flight while the bundle is edited and the cache is reset.
            entered.set()
            await release.wait()
            return _old_spec()
        return _new_spec()

    def _frames() -> list[str]:
        return [
            _sse({"type": "response.created", "response": {"id": "r1"}}),
            _sse({"type": "response.completed", "response": {"id": "r1"}}),
        ]

    server = _McpRecordingServer()
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient(_frames())),  # type: ignore[arg-type]
        spec_resolver=resolver,
        server_client=server,  # type: ignore[arg-type]
    )

    def _turn_body(text: str) -> dict[str, Any]:
        return {
            "type": "message",
            "role": "user",
            "agent_id": AGENT_ID,
            "has_mcp_servers": True,
            "content": [{"type": "input_text", "text": text}],
        }

    async with _runner_client(app) as client:

        async def _slow_turn() -> None:
            resp = await client.post(
                f"/v1/sessions/{CONV_SLOW}/events",
                params={"stream": "true"},
                json=_turn_body("hi"),
            )
            assert resp.status_code == 200, f"{resp.status_code} {resp.text}"
            _ = resp.text

        turn = asyncio.create_task(_slow_turn())
        await asyncio.wait_for(entered.wait(), timeout=10)

        reset = await client.post(
            f"/v1/sessions/{CONV_SLOW}/agent-cache/reset",
            json={"agent_id": AGENT_ID},
        )
        assert reset.status_code == 200, reset.text

        release.set()
        await asyncio.wait_for(turn, timeout=10)

        later = await client.post(
            f"/v1/sessions/{CONV_LATER}/events",
            params={"stream": "true"},
            json=_turn_body("hi again"),
        )
        assert later.status_code == 200, later.text
        _ = later.text

    assert CONV_LATER not in server.mcp_sessions, (
        "the conversation after the reset used the version-1 bundle (it asked "
        "the MCP proxy for tools of a server the edit removed); the late write "
        "republished a bundle from before the invalidation"
    )

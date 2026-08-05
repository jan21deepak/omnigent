"""
Tests for classifying a managed sandbox by the built-in agent it runs.

The name resolved here reaches the infrastructure (the Kubernetes launcher
stamps it on the runner Pod), so the interesting cases are the ones a caller
could try to forge: a session-scoped agent named after a built-in, and a
global row whose id is not the name-derived built-in id.
"""

from __future__ import annotations

from dataclasses import replace
from typing import cast

from omnigent.db.utils import builtin_agent_id, generate_agent_id
from omnigent.entities import Agent
from omnigent.entities.conversation import Conversation
from omnigent.server.routes._sessions.helpers import _builtin_agent_name_for_session
from omnigent.stores import AgentStore


class _StubAgentStore:
    """Agent store stub answering ``get`` from a fixed row."""

    def __init__(self, agent: Agent | None) -> None:
        self._agent = agent

    def get(self, agent_id: str) -> Agent | None:
        """
        Return the stubbed row when its id matches.

        :param agent_id: The looked-up agent id.
        :returns: The stubbed agent, or ``None``.
        """
        if self._agent is None or self._agent.id != agent_id:
            return None
        return self._agent


def _builtin(name: str) -> Agent:
    """
    Build a seeded built-in agent row.

    :param name: The built-in's name, e.g. ``"polly"``.
    :returns: A global agent row with the name-derived id.
    """
    return Agent(
        id=builtin_agent_id(name),
        created_at=0,
        name=name,
        bundle_location=f"{name}/bundle",
    )


def _conv(agent_id: str | None) -> Conversation:
    """
    Build a session row bound to *agent_id*.

    :param agent_id: The bound agent's id, or ``None`` for no agent.
    :returns: The session row.
    """
    return Conversation(
        id="conv_abc123",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_abc123",
        agent_id=agent_id,
    )


def _store(agent: Agent | None) -> AgentStore:
    """
    Adapt the stub to the store type the resolver takes.

    :param agent: The row the stub answers ``get`` with.
    :returns: The stub, typed as an :class:`AgentStore`.
    """
    return cast(AgentStore, _StubAgentStore(agent))


def test_builtin_agent_is_resolved_by_name() -> None:
    """A session bound to a seeded built-in classifies as that built-in."""
    agent = _builtin("polly")
    assert _builtin_agent_name_for_session(_conv(agent.id), _store(agent)) == "polly"


def test_session_scoped_agent_named_after_a_builtin_is_not_resolved() -> None:
    """The classifier is not forgeable by naming a session agent after a built-in."""
    agent = replace(_builtin("polly"), session_id="conv_abc123")
    assert _builtin_agent_name_for_session(_conv(agent.id), _store(agent)) is None


def test_global_agent_without_the_name_derived_id_is_not_resolved() -> None:
    """A global row whose id is not the built-in id for its name yields nothing."""
    agent = replace(_builtin("polly"), id=generate_agent_id())
    assert _builtin_agent_name_for_session(_conv(agent.id), _store(agent)) is None


def test_missing_session_agent_or_store_yields_no_classifier() -> None:
    """Nothing to resolve from → no classifier, rather than a guess."""
    agent = _builtin("polly")
    assert _builtin_agent_name_for_session(None, _store(agent)) is None
    assert _builtin_agent_name_for_session(_conv(None), _store(agent)) is None
    assert _builtin_agent_name_for_session(_conv(agent.id), None) is None
    assert _builtin_agent_name_for_session(_conv("ag_unknown"), _store(agent)) is None

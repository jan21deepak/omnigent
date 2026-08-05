"""Reconcile externally-driven ACP session turns into an Omnigent conversation.

A persistent ACP session (e.g. an OpenClaw Gateway session driven through
``openclaw acp``) outlives the Omnigent process and can be driven by other
clients — OpenClaw's Control UI, a chat channel — while Omnigent is
disconnected. Those turns never reach the Omnigent transcript, so the two views
diverge and a later reply can depend on context the user cannot see.

The catch-up primitive ACP does expose is ``session/load``: an agent that
advertises ``agentCapabilities.loadSession`` re-opens a known session id and
replays its whole transcript as ``session/update`` notifications
(``user_message_chunk`` / ``agent_message_chunk``) before answering. This module
holds the pieces built on top of it:

* :class:`AcpSessionStore` — durable Omnigent-conversation → ACP-session-id
  mapping (plus a replay cursor), so the mapping survives a process restart.
* :class:`HistoryReplay` — folds replayed chunk notifications back into turns.
* :func:`missed_turns` — the delta between the replayed transcript and what
  Omnigent already shows, deduplicated and in order.
* :func:`format_backfill` / :data:`DIVERGENCE_NOTICE` — how that delta (or the
  absence of a reliable catch-up path) is surfaced in the conversation.

Agents without ``loadSession`` get no reliable reconciliation; the caller
surfaces :data:`DIVERGENCE_NOTICE` and starts a fresh session rather than
silently continuing against a transcript that may already have diverged.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Env override for the mapping file (tests and isolated data dirs).
SESSION_STORE_ENV = "HARNESS_ACP_SESSION_STORE"

# session/update discriminators that carry transcript content during a replay.
_UPDATE_USER_MESSAGE_CHUNK = "user_message_chunk"
_UPDATE_AGENT_MESSAGE_CHUNK = "agent_message_chunk"

_ROLE_BY_UPDATE = {
    _UPDATE_USER_MESSAGE_CHUNK: "user",
    _UPDATE_AGENT_MESSAGE_CHUNK: "assistant",
}

# Backfill rendering caps: a long-running external session can hold far more
# text than is useful to replay into a single Omnigent turn.
_MAX_BACKFILL_TURNS = 50
_MAX_BACKFILL_CHARS = 2000

# Shown when a stored mapping exists but the agent cannot replay the session.
DIVERGENCE_NOTICE = (
    "_This agent cannot replay its session history (`session/load` is not supported), "
    "so turns added from other clients while Omnigent was disconnected cannot be "
    "recovered. Starting a fresh session; the transcripts may have diverged._"
)

_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class ExternalTurn:
    """One replayed transcript turn.

    :param role: ``"user"`` or ``"assistant"``.
    :param text: The turn's folded text content.
    :param source: Where the turn came from when the agent says so (an ACP
        ``_meta`` client label, e.g. an OpenClaw channel); ``None`` when the
        agent does not attribute it.
    """

    role: str
    text: str
    source: str | None = None


@dataclass(frozen=True)
class SessionRecord:
    """A persisted Omnigent-conversation → ACP-session mapping.

    :param session_id: The agent-side session id to re-open with ``session/load``.
    :param command: Agent command the id belongs to; a different command must
        not adopt it.
    :param cwd: Working directory the session was created in.
    :param cursor: How many replayed transcript turns have already been
        reconciled, so a later reconnect does not surface them twice.
    :param updated_at: Unix timestamp of the last write.
    """

    session_id: str
    command: str
    cwd: str
    cursor: int = 0
    updated_at: float = 0.0

    def matches(self, *, command: str, cwd: str) -> bool:
        """Is this record usable for the given agent command and cwd?"""
        return self.command == command and self.cwd == cwd

    def to_json(self) -> dict[str, Any]:  # type: ignore[explicit-any]
        """Serialize for the on-disk store."""
        return {
            "session_id": self.session_id,
            "command": self.command,
            "cwd": self.cwd,
            "cursor": self.cursor,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, object]) -> SessionRecord | None:
        """Rebuild a record, returning ``None`` for a malformed entry."""
        session_id = payload.get("session_id")
        command = payload.get("command")
        cwd = payload.get("cwd")
        if not isinstance(session_id, str) or not session_id:
            return None
        if not isinstance(command, str) or not isinstance(cwd, str):
            return None
        cursor = payload.get("cursor")
        updated_at = payload.get("updated_at")
        return cls(
            session_id=session_id,
            command=command,
            cwd=cwd,
            cursor=cursor if isinstance(cursor, int) and cursor >= 0 else 0,
            updated_at=float(updated_at) if isinstance(updated_at, (int, float)) else 0.0,
        )


def default_store_path() -> Path:
    """Return the mapping file path (``HARNESS_ACP_SESSION_STORE`` wins)."""
    override = os.environ.get(SESSION_STORE_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    from omnigent.process_logging import data_dir

    return data_dir() / "acp" / "sessions.json"


class AcpSessionStore:
    """A small JSON file mapping conversation keys to ACP sessions.

    Best-effort by design: a corrupt or unwritable store degrades to "no
    mapping" (a fresh session) rather than failing a turn.
    """

    def __init__(self, path: Path | None = None) -> None:
        """:param path: Mapping file; defaults to :func:`default_store_path`."""
        self._path = path or default_store_path()

    @property
    def path(self) -> Path:
        """The backing file path."""
        return self._path

    def _read_all(self) -> dict[str, dict[str, object]]:
        try:
            raw = self._path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return {}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("acp session store %s is not valid JSON; ignoring", self._path)
            return {}
        if not isinstance(payload, dict):
            return {}
        return {k: v for k, v in payload.items() if isinstance(v, dict)}

    def _write_all(self, entries: Mapping[str, dict[str, object]]) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(entries, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(self._path)
        except OSError as exc:
            logger.debug("could not persist acp session store %s: %s", self._path, exc)

    def load(self, key: str) -> SessionRecord | None:
        """Return the record stored under *key*, if any."""
        entry = self._read_all().get(key)
        return SessionRecord.from_json(entry) if entry is not None else None

    def save(self, key: str, record: SessionRecord) -> None:
        """Store *record* under *key*, stamping ``updated_at``."""
        entries = self._read_all()
        entries[key] = SessionRecord(
            session_id=record.session_id,
            command=record.command,
            cwd=record.cwd,
            cursor=record.cursor,
            updated_at=time.time(),
        ).to_json()
        self._write_all(entries)

    def delete(self, key: str) -> None:
        """Drop the mapping stored under *key* (no-op when absent)."""
        entries = self._read_all()
        if entries.pop(key, None) is not None:
            self._write_all(entries)


class HistoryReplay:
    """Folds ``session/load`` replay notifications back into whole turns.

    Consecutive chunks with the same role belong to one turn, so they are
    concatenated; a role change closes the previous turn.
    """

    def __init__(self) -> None:
        self._turns: list[ExternalTurn] = []
        self._role: str | None = None
        self._parts: list[str] = []
        self._source: str | None = None

    def add_update(self, update: Mapping[str, object]) -> None:
        """Consume one ``session/update`` payload; ignores non-transcript ones."""
        role = _ROLE_BY_UPDATE.get(str(update.get("sessionUpdate", "")))
        if role is None:
            return
        text = _content_text(update.get("content"))
        if not text:
            return
        source = _source_label(update)
        if role != self._role or (source is not None and source != self._source):
            self._flush()
            self._role = role
            self._source = source
        elif self._source is None:
            self._source = source
        self._parts.append(text)

    def _flush(self) -> None:
        if self._role is not None and self._parts:
            self._turns.append(
                ExternalTurn(role=self._role, text="".join(self._parts), source=self._source)
            )
        self._role = None
        self._parts = []
        self._source = None

    def turns(self) -> tuple[ExternalTurn, ...]:
        """Return the replayed turns in order."""
        self._flush()
        return tuple(self._turns)


def _content_text(content: object) -> str:
    """Extract text from an ACP content block (or list of blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, Mapping):
        text = content.get("text")
        return text if isinstance(text, str) else ""
    if isinstance(content, list):
        return "".join(_content_text(block) for block in content)
    return ""


def _source_label(update: Mapping[str, object]) -> str | None:
    """Read a client attribution label out of an update's ``_meta``, if present.

    ACP does not standardize turn attribution; agents that do attribute a turn
    carry it in the extensible ``_meta`` bag.
    """
    meta = update.get("_meta")
    if not isinstance(meta, Mapping):
        return None
    for key in ("source", "client", "origin", "channel"):
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, Mapping):
            nested = value.get("name")
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
    return None


def _normalize(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def _local_texts(messages: Iterable[object]) -> list[tuple[str, str]]:
    """Return ``(role, normalized_text)`` for the local transcript's turns."""
    out: list[tuple[str, str]] = []
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        role = str(message.get("role", ""))
        if role not in ("user", "assistant"):
            continue
        text = _normalize(_message_text(message.get("content")))
        if text:
            out.append((role, text))
    return out


def _message_text(content: object) -> str:
    """Fold an Omnigent message's content (string or block list) into text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, Mapping):
                continue
            if block.get("type") in ("input_text", "output_text", "text"):
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


def missed_turns(
    replayed: Sequence[ExternalTurn],
    local_messages: Sequence[object],
    cursor: int = 0,
) -> tuple[tuple[ExternalTurn, ...], int]:
    """Return ``(turns_missing_from_Omnigent, new_cursor)``.

    Two independent guards keep a turn from being surfaced twice:

    * *cursor* — replayed turns already reconciled by an earlier reconnect are
      skipped outright (the agent's transcript is append-only, so the index is
      stable). Turns backfilled before are folded into an assistant message and
      would no longer match by text.
    * text matching — a replayed turn whose text is already in the Omnigent
      transcript came from Omnigent itself. Matching is exact first (each local
      turn accounts for at most one replayed turn), then a suffix match either
      way: Omnigent prefixes the system prompt or a history replay onto the
      first user text it sends, and prefixes an earlier backfill block onto the
      assistant text it stores.

    :param replayed: Turns the agent replayed, oldest first.
    :param local_messages: The Omnigent transcript for this conversation.
    :param cursor: Replayed turns already reconciled previously.
    :returns: The missing turns in order, plus the cursor to persist.
    """
    start = max(0, min(cursor, len(replayed)))
    unmatched = _local_texts(local_messages)

    missed: list[ExternalTurn] = []
    for turn in replayed[start:]:
        text = _normalize(turn.text)
        if not text:
            continue
        match = _match_index(unmatched, turn.role, text)
        if match is None:
            missed.append(turn)
        else:
            # Each local turn accounts for at most one replayed turn, so a
            # genuinely repeated external turn still surfaces.
            unmatched.pop(match)
    return tuple(missed), len(replayed)


def _match_index(local: Sequence[tuple[str, str]], role: str, text: str) -> int | None:
    """Index of the local turn *text* was sent from, or ``None``."""
    for index, (local_role, local_text) in enumerate(local):
        if local_role == role and local_text == text:
            return index
    for index, (local_role, local_text) in enumerate(local):
        if local_role != role:
            continue
        if text.endswith(local_text) or local_text.endswith(text):
            return index
    return None


def format_backfill(turns: Sequence[ExternalTurn]) -> str:
    """Render missed external turns as a transcript block for the conversation.

    Each turn is attributed to the client the agent named, or generically to
    "outside Omnigent" when it named none. Empty input renders as ``""``.
    """
    if not turns:
        return ""
    shown = list(turns[:_MAX_BACKFILL_TURNS])
    dropped = len(turns) - len(shown)
    count = len(turns)
    header = (
        f"_Synced {count} turn{'s' if count != 1 else ''} added to this session outside Omnigent:_"
    )
    lines = [header, ""]
    for turn in shown:
        text = turn.text.strip()
        if len(text) > _MAX_BACKFILL_CHARS:
            text = text[:_MAX_BACKFILL_CHARS] + "…"
        attribution = f"{turn.role} · {turn.source}" if turn.source else f"{turn.role} · external"
        body = text.replace("\n", "\n> ")
        lines.append(f"> **{attribution}:** {body}")
        lines.append(">")
    if dropped:
        lines.append(f"> _…and {dropped} earlier turn(s) not shown._")
    lines.append("")
    return "\n".join(lines)

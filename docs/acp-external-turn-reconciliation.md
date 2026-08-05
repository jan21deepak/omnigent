# ACP: reconciling externally-driven session turns

The generic `acp` harness drives a persistent agent session — for example an
OpenClaw Gateway session started with `openclaw acp`. That session outlives the
Omnigent process and can keep being driven from OpenClaw's Control UI or another
OpenClaw channel while Omnigent is disconnected. Without reconciliation the two
transcripts diverge: the agent keeps the external turns as context, Omnigent
never shows them, and a later reply depends on something the user cannot see.

## What Omnigent does

The catch-up primitive ACP exposes is `session/load`, which an agent advertises
as `agentCapabilities.loadSession`. Omnigent uses it as follows:

1. **Persist the mapping.** The Omnigent conversation id → agent session id
   mapping (plus a replay cursor) is stored in
   `<data dir>/acp/sessions.json`, so it survives an Omnigent restart. The
   conversation id comes from the harness event path (`Executor.bind_conversation`),
   so it is stable regardless of telemetry. Records are scoped to the agent
   command and working directory, and writes take a file lock so concurrent
   conversations do not drop each other's entries.
2. **Re-open, don't replace.** When a conversation already owns a session, the
   next turn sends `session/load` instead of `session/new`. The agent replays
   its transcript as `user_message_chunk` / `agent_message_chunk` updates.
3. **Backfill the delta.** Replayed turns that Omnigent already shows are
   dropped; the rest are rendered, in order, as a quoted block at the top of the
   next reply, attributed to the client the agent named (or generically to
   "external"). The replay cursor and per-turn text matching keep a turn from
   being surfaced twice across reconnects.
4. **Fail visibly.** If the agent cannot replay (`loadSession` unsupported) or
   the Gateway has dropped the session, Omnigent starts a fresh session and says
   so in the conversation rather than silently continuing on a transcript that
   may already have diverged.

## Configuration

| Env var | Default | Meaning |
| --- | --- | --- |
| `HARNESS_ACP_RECONCILE` | on | `0` disables re-opening and backfill; every turn then starts from a new session. |
| `HARNESS_ACP_SESSION_STORE` | `<data dir>/acp/sessions.json` | Path of the mapping file. |

## Known limits

* Turns added by another client **while Omnigent is connected** are not streamed
  live; they arrive at the next reconnect. ACP has no subscription for updates
  outside the active client's own prompt.
* Only user and assistant text is reconciled. Tool calls an external client
  triggered are context for the agent but are not rendered as Omnigent tool
  cards.
* Attribution depends on the agent: ACP does not standardize it, so a turn is
  labelled with the client name only when the agent supplies one in the update's
  `_meta`.

# Kimi usage and cost tracking design

Kimi Code 0.34.0 persists token usage and model metadata to each session's
append-only `wire.jsonl`. Omnigent's headless `kimi` harness and `kimi-native`
interactive harness previously ignored those records, leaving sessions without
model attribution, token counts, or priced cost — which also caused
`cost_budget` policies to hard-DENY when `total_cost_usd` was never populated.

## Wire schema (0.34.0)

Relevant record types in `sessions/<wd>/<session_…>/agents/main/wire.jsonl`:

- `usage.record` — `{ model, usage: { inputOther, output, inputCacheRead, inputCacheCreation }, usageScope: "turn" }`
- `llm.request` — `{ model, modelAlias, maxTokens, provider, … }`

Kimi emits no monetary fields; Omnigent prices tokens via the model catalog (same
as codex-native).

## Contracts

Both paths use the existing Sessions API event surface — no server changes.

### kimi-native forwarder

- Tail the same `wire.jsonl` already used for conversation mirroring.
- Map `usage.record` (turn scope only) into coalesced `external_session_usage`
  posts:
  - `cumulative_input_tokens` = Σ (`inputOther` + `inputCacheCreation`)
  - `cumulative_cache_read_input_tokens` = Σ `inputCacheRead`
  - `cumulative_output_tokens` = Σ `output`
  - `context_tokens` from the latest record's occupancy (`inputOther +
    inputCacheRead + inputCacheCreation`)
  - `context_window` from Omnigent's model catalog (never `llm.request.maxTokens`)
  - `model` on every post when known
- Map `llm.request` → deduped `external_model_change` (not seeded at spawn).
- Persist cumulative totals + `posted_model` in `kimi_forwarder.json`.

### Headless `kimi` executor

- After each turn, locate `wire.jsonl` via the captured `session.resume_hint` id.
- Keep a per-session byte-offset checkpoint (resume-safe).
- Sum new turn-scoped `usage.record` deltas into `TurnComplete.usage`.

## Error handling

Strictly best-effort: missing/malformed wire logs or schema drift log once and
emit nothing; never fail a turn.

## Follow-ups (out of scope)

- Upstream structured usage in Kimi stream-json stdout (then delete wire tail read).
- Kimi/Moonshot pricing catalog entries when absent — tokens persist but stay
  unpriced until catalog coverage lands.

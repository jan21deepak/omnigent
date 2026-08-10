# Agent guidance

Guidance for AI agents (Claude Code, Copilot, Cursor, etc.) working in this
repository. See `CONTRIBUTING.md` for the full contributor workflow.

## Committing

Run the `pre-commit` hook before committing (`pre-commit run --all-files`, or
let it run on staged files via `git commit`). Fix any issues it reports so the
commit lands clean — CI runs the same checks.

## Local development shortcuts

Use `just` for common tasks; run `just --list` for grouped recipes.

- `just ensure` — install/check prerequisites
- `just run-ios` / `just run-android` — build/run mobile apps
- `just dev` / `just dev-mobile` — start the omnigent dev pod
- `just electron-dev` / `just electron-build` — Electron desktop shell
- `just lint` / `just lint-all` — run pre-commit
- `just normalize-locks` — rewrite lockfile registries to PyPI/npmjs.org

## Pull requests

When you open a pull request, fill in the repo's PR template at
`.github/pull_request_template.md` (case-sensitive on Linux — note the lowercase
filename). Keep every section and checkbox row so reviewers can skim them.

- **Summary** — what changed and why.
- **Test Plan** — how you verified it.
- **Demo** — a **video or images** showing the change. Expected on contributor
  PRs for UI / frontend changes (check the "UI / frontend change" box under
  *Type of change*) so reviewers can see the new behaviour without checking out
  the branch. Use `N/A` for non-visual changes.
- **Type of change** / **Test coverage** — check all that apply (at least one
  each).
- **Coverage notes** — required if you checked "Manual verification completed"
  or "Not applicable".

Generate the description from the actual diff and this session's context — lead
with the motivation, then the change. Don't pass a `--body` that skips these
sections.

## Finishing a task

When you finish a task, print instructions to the user on how to test it: the
commands to run, the inputs to provide, or the steps to reproduce so they can
verify the result themselves. Don't leave the user guessing how to confirm the
work — tell them exactly what to do.

## Deprecating features

When deprecating a feature, note the version in which it is expected to be
removed so we can clean it up when that version ships. Call out the deprecation
version in code (e.g. a `@deprecated` tag or comment naming the target release)
and in the PR/commit description, so there's a clear marker to act on later.

## Code comments

Keep comments short and focused on the code, not on the change history.

- **Keep them brief** — prefer one or two lines. Avoid comments longer than
  three lines; if you need more, the code likely needs refactoring or a doc
  string, not a wall of inline commentary.
- **Describe the scenario, not the PR** — explain *what* the code handles or
  *why* it exists, in terms a future reader needs. Don't reference PR numbers,
  issue numbers, or ticket IDs (e.g. `#1646`, `fixes JIRA-123`); the scenario
  should be clear without chasing external links.

## Framework-owned instructions

Keep runtime lifecycle and metadata instructions separate from portable agent
instructions:

- Agent-spec and per-request instructions are user-authored. Framework-owned
  instructions are additive runtime behavior and are appended after them in
  `omnigent/runtime/prompt.py`.
- Keep the canonical instruction text and lifecycle gate in the owning framework
  module. Harness adapters should only transport the composed instructions; do
  not duplicate policy across adapters or add lifecycle metadata to `AgentSpec`.
- If framework instructions grow beyond a small ordered list, introduce a
 structured `FrameworkInstructions` value at the prompt-composition boundary.

## Cursor Cloud specific instructions

Environment is refreshed on startup by the update script (`uv sync --extra all
--extra dev` then `pnpm --dir web install`). `uv`, `bubblewrap` (`bwrap`),
`node`/`pnpm`, and `cargo` are already present on the VM; `uv` is symlinked into
`/usr/local/bin` so it resolves in non-login shells. You do NOT need to
reinstall these. Standard dev/test/lint commands live in `CONTRIBUTING.md`, the
`justfile`, and `web/package.json` — reference those rather than duplicating.

Running the product (all long-lived processes must run in the background, e.g.
`tmux`; none are auto-started):

- Server + host + web are three separate processes — start them in three
  background sessions, not one:
  1. `uv run omnigent server` (API + bundled web UI on `:6767`)
  2. `uv run omnigent host --server http://localhost:6767` (registers this
     machine so the web UI can start sessions; without it the UI is
     read/continue-only)
  3. `cd web && pnpm run dev` (Vite dev server; it prints its own URL and
     proxies `/v1/*` to `:6767`). Do not assume the Vite port is `5173`.
- `omnidev` (Rust supervisor, `cargo install --path dev/omnidev`) runs all three
  with isolated per-checkout state, but it is a foreground TUI, so it is
  awkward under automation — prefer the three-process flow above here.

Model credentials / testing an agent turn without real API keys: no provider
keys are configured by default, so a live agent turn needs a model. For
credential-free end-to-end testing, use the bundled mock LLM
(`tests/server/integration/mock_llm_server.py <port>`, an OpenAI/Anthropic-
compatible fake). Wire it by giving an agent an `openai-agents` executor whose
`auth`/`connection` point at `http://127.0.0.1:<port>/v1` (see
`.claude/skills/polly-e2e-dev/polly_cuj.py` `_mock_polly_bundle` for the exact
spec shape) and export `OPENAI_BASE_URL=http://127.0.0.1:<port>/v1` +
`OPENAI_API_KEY=mock-key` on the server, host, and any `omnigent run` process.
`POST /mock/set_fallback` (or `/mock/configure`) sets the reply. To make a
custom agent appear in the web UI's New Session picker, launch the server with
`OMNIGENT_BUILTIN_AGENT_DIRS=<bundle-dir>` (each path is a single bundle dir
containing `config.yaml`; the agent is named after the dir).

Fast credential-free checks: `.venv/bin/python
.claude/skills/polly-e2e-dev/polly_cuj.py --scenario all` exercises a real agent
turn + tool dispatch through the runtime against the mock. `tests/e2e/` is
skipped by default and needs real model creds (see `tests/e2e/AGENTS.md`).

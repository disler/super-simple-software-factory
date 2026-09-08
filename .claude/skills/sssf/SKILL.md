---
name: sssf
description: Super Simple Software Factory — deploy and operate repeatable agents+code workflows (ADWs) in any codebase. Use when the user invokes sssf (for example /sssf in Claude Code or $sssf in Codex), wants to create/run/update an ADW, manage the agent roster in sssf.config.yaml, or observe running agent workflows. Keywords - sssf, software factory, ADW, AI developer workflow, agent pipeline, install factory.
argument-hint: "[install | create adw | run adw | update config | ...]"
---

# Super Simple Software Factory (SSSF)

Reusable combination of **agents plus code**: deterministic Python ADW scripts own sequencing, retries, and acceptance; coding agents (Pi in v1) work inside bounded phases; typed JSON envelopes carry context between them; everything streams into SQLite for the polled visualizer. Agent proposes, code disposes.

## Startup

Three steps. Then stop.

1. Read [cookbooks/sssf_overview.md](cookbooks/sssf_overview.md) — the system map.
2. `ls adws/adw_*.py` and read each file's `Phases:` docstring line.
3. Print the ADWs as a table — name, the chain, one line on when to reach for it — and **wait for the engineer's request.**

```
| ADW | Chain | Use when |
|---|---|---|
| adw_scout | engineer → scout | read-only recon; nothing changes |
| adw_simple_sdlc | plan → build → test → review → document, 3 commits | the work is real and its shape is not obvious |
```

**Nothing else.** No trace-db queries, no reading the config or the ADW scripts' bodies, no repo inventory, no last-runs summary, no diagnosing an old failure, no "current state" dashboard. None of it was asked for, and it is not free:

- **Volunteered state is guessed state.** An orchestrator that improvised a status board queried a `runs` table and a `payload` column — neither exists (`sessions`, `payload_json`). The spec that would have said so is `references/observability.md`, one lazy read away. Probing to look prepared is how you end up confidently wrong in your first message.
- **It spends the context the real task needs**, before you know what the task is.
- **It is stale on arrival.** State printed before the request describes a system that the very next run changes.

Everything else — the db schema, the roster, the handoff contract — is lazy-loaded through the routing table below, when a request actually calls for it. Reading it early defeats the mechanism.

Two exceptions, both narrow: if the engineer's first message already contains a request, skip the waiting and route it; and if the factory is plainly not installed (no `adws/`, no config), say that in one line instead of the table.

## Orchestrator rules

You run the system, observe the system, and help the user interact with it. **You do no ADW work yourself:**

- Never implement, plan, or test in an agent's place — launch the ADW and watch it.
- Never edit files inside `adws/adw_data/sessions/` — that is the run record.
- Observe by querying `adws/adw_data/sssf.db` (WAL — reads never block writers) **when observing is the task**. This is a capability, not a startup step: query it to follow a run you launched or one the engineer asked about, never to volunteer a status report nobody requested.
- Report phase status plainly: name, owner, status, error if any.

## Request routing (lazy-load the cookbook, then follow it)

| Request | Cookbook |
|---|---|
| `sssf install`, set up the factory in this repo | [cookbooks/install.md](cookbooks/install.md) |
| create a new ADW / workflow | [cookbooks/create_adw.md](cookbooks/create_adw.md) |
| modify an existing ADW chain | [cookbooks/update_adw.md](cookbooks/update_adw.md) |
| create the config / agent roster | [cookbooks/create_config.md](cookbooks/create_config.md) |
| decide WHICH harness/model each agent should run | the `sssf-pick-models` skill, installed beside this one — it carries the catalogs |
| add or retune an agent (prompts, tools, thinking, a model you already chose) | [cookbooks/update_config.md](cookbooks/update_config.md) |
| extend adw_modules with new low-level logic | [cookbooks/update_modules.md](cookbooks/update_modules.md) |
| run / monitor an ADW | [cookbooks/how_to_prompt_for_the_eng.md](cookbooks/how_to_prompt_for_the_eng.md) **first**, then [cookbooks/run_adw.md](cookbooks/run_adw.md) |
| turn a request into an ADW prompt | [cookbooks/how_to_prompt_for_the_eng.md](cookbooks/how_to_prompt_for_the_eng.md) |

Deep specs, when needed: [references/config.md](references/config.md) · [references/handoff.md](references/handoff.md) · [references/observability.md](references/observability.md)

## Hard rules (enforced across everything the factory generates)

1. **Validate before running** — every ADW declares `REQUIRED_AGENTS` and calls `agents.validate()` first; a missing/misnamed agent fails before anything spawns.
2. **Typed outputs only** — every agent call pairs with a concrete `EnvelopeBase` subclass in `adw_modules/data_types.py`; parse failures re-prompt the same session (context intact), never restart.
   **The output contract is a synced triad**: (a) the type in `data_types.py`, (b) the JSON example in the agent's `user.md` `## Report` section, (c) `output_type=` at every call site. These are ONE contract — change any one, update all three in the same edit (grep the type name to find every call site).
3. **Gates validate claims, not guesses** — `gate(envelope, run) -> list[str]` violations; failures return to the same session as corrections.
4. **Four-param rule** — any function with more than 4 parameters takes one concrete data type instead (`AgentCall`, `PhaseParams` are the pattern).
5. **One agent, one prompt, one purpose** — identity lives in `system.md`; task shape (user prompt + output type) lives at the call site.
6. **ADW scripts stay thin** — all low-level logic lives in `adw_modules/`.
7. **Every phase earns a description** — one sentence on what it does and why, never a restatement of its name. It is the only intent the trace, the console, and the UI ever show; `commit_plan: "Commit the plan"` is rejected at construction, blank is too.
8. **A known command is code, not an agent** — if you can write the invocation down (`bun test`, `ruff check`), it belongs in a `kind="code"` phase via `adw_modules/quality.py`. Agents are for the parts that need reading and deciding; failures come back to the builder as an envelope either way.
9. **`tools:` is a capability list, `writes:` is the boundary** — `bash` runs anything (including `git checkout`) and `write` reaches any path, so a tool list can never make "this agent changes nothing" true. `writes:` per agent, active prompts, and `protected_files` in defaults are enforced in `adw_modules/permissions.py` after every harness turn: unauthorized changes are rolled back and the phase dies before parsing or retrying. Only the current `{data_dir}/sessions/{adw_id}/` runtime is always writable — a read-only agent is read-only with respect to the REPO, never mute.
10. **Every ADW ends in `run.finish()`** — phases passing is not the same as the run being accepted. A test phase that ran a red suite succeeded at its job. Pass `accepted=` so the exit code, the session status, and the banner are decided together and cannot disagree.

## v1 scope

The starter roster defaults to Claude Code Sonnet, with the reviewer on Codex GPT-5.6 Terra. `pi`, `claude_code`, `codex`, `kiro_cli`, and `antigravity` are all implemented and selectable per agent — see `references/config.md#harnesses`. `harness_engineering` (Pi extensions) stays Pi-only; setting it on any other harness fails validation. `tools:` is only honored by `pi` and `claude_code`: `codex`, `kiro_cli`, and `antigravity` have no allowlist flag to map onto, so they require `tools: null` and rely on `permissions.py` for the boundary. The visualizer app ships in a later pass — observe via sqlite queries until then.

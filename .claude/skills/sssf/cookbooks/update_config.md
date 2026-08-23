# Update Config

Add or retune agents in `sssf.config.yaml`.

## Retune model or effort

Edit the agent's entry in place:

```yaml
  - name: builder
    model: google/gemini-3.6-flash   # ALWAYS provider/model-id
    effort: high                     # was medium; `thinking` remains a legacy alias
```

Write the model as `provider/model-id`, never a bare id. The same model is usually carried by several providers, and an ambiguous pattern raises in `agents.validate()` — grounding every agent that inherits it. See `references/config.md`.

When both `effort` and `thinking` are set, they must match or validation fails. Native effort sets differ: Pi accepts `off | minimal | low | medium | high | xhigh | max`; Claude Code accepts `low | medium | high | xhigh | max | ultracode`; Codex accepts `minimal | low | medium | high | xhigh`. Use `low`, `medium`, `high`, or `xhigh` for a mixed roster.

**A model or harness change means a fresh session.** `agent_map.json` records the model and `coding_agent` each session was created with. When a joined run (`--adw-id`) no longer matches either, that agent starts a **new** session rather than resuming — the map is updated, never a bad resume. Effort changes do not invalidate a matching session. Legacy entries without `coding_agent` are Pi. There is no fallback to another harness or model.

## Recolor an agent's lane

```yaml
  - name: builder
    color: "#22d3ee"      # hex; the starter roster ships violet/cyan/amber/green
```

Purely cosmetic and safe to change mid-project: the color rides the `agent_start` event and the `agent_sessions` row, so the visualizer picks it up on the next run without touching past sessions. Omit the key to let the UI's fallback palette choose.

## Retune tools and harness

Use portable capabilities in new rosters: `read`, `search`, `shell`, `write`, `edit`. The shared resolver accepts `bash → shell` and `grep`/`find`/`ls → search` for every harness. Codex accepts an alias only when its normalized result is the inspection profile (`read`, `search`, `shell`) or mutation profile (all five); Claude Code accepts portable subsets.

Set the roster-wide floor in `defaults`, then narrow per agent:

```yaml
defaults:
  tools: [read, search, shell, edit, write]

agents:
  - name: reviewer
    tools:                # explicit list wins over defaults
      - read
      - search
      - shell
      - write
```

**Resolution:** the agent's own list wins → else it inherits `defaults.tools` → else `None`, meaning all tools. An empty list is not "all tools"; it is a tool-less agent, and it will stall.

Narrow by role, not by reflex:

- Any agent that must directly produce a `context_handoff/` artifact needs a **writable harness profile** and `write`. Native read-only Claude Code and Codex profiles cannot write the repository-local runtime; their structured response still works, but it cannot stand in for a phase whose gate requires a file.
- Withhold `edit`/`write` only where the restriction *is* the guarantee. The reviewer's contract is "change nothing", so withholding `edit` makes that structural instead of merely prompted.
- Recon agents should get the full read surface (`read`, `grep`, `find`, `ls`) — cheaper and more legible in the trace than the equivalent `bash` calls.

**Extension tools count against the allowlist.** This applies to Pi only: `harness_engineering` is Pi-specific, and Pi filters extension tools unless their names appear in `tools`. Claude Code and Codex reject extension files rather than silently ignoring them.

## Add harness extensions

```yaml
    harness_engineering:
      - .pi/extensions/json_guard.ts    # a pi extension FILE PATH
```

Entries are Pi extension **file paths**, passed through to that adapter and applied to that agent only. The starter planner and scout already use the shipped `subagents.ts` extension. Add a custom extension only when a Pi agent needs behavior such as output tightening; it is an escape hatch, not a default.

**Adding a tool-registering extension is a two-part edit.** The extension path goes in `harness_engineering`, *and* the tool name it registers goes in that agent's `tools` list:

```yaml
  - name: reviewer
    harness_engineering:
      - .pi/extensions/ast_query.ts     # registers tool: ast_query
    tools:
      - read
      - grep
      - find
      - ls
      - bash
      - ast_query                       # REQUIRED — or the extension loads and its tool is filtered out
```

Skip the second half and it fails silently: extension loaded, run green, tool never available to the model. Extensions that only shape output or register flags — no new tool — need no `tools` change.

## Add a new agent

Three steps, all required — skipping any one fails `agents.validate()` at ADW startup, before anything spawns:

1. **Prompts.** Create `adws/adw_data/prompt_engineering/{name}/system.md` (Purpose + Instructions — the agent's static identity, nothing else) and `user.md` (an h3 per incoming datum: `{{prompt}}`, `{{previous_envelope}}`, `{{context_handoff_dir}}`, then the task, then a `## Report` section showing the exact output JSON). Copy an existing pair as the shape.
2. **Config entry.** Name, purpose, prompt refs, plus anything that differs from `defaults`.
3. **An output type.** Every agent call parses against a concrete Pydantic model in `adw_modules/data_types.py`. If none of `PlanOutput`, `BuildOutput`, `ScoutOutput`, `ReviewOutput`, `DocumentOutput` fits the new agent's report, add one — see `update_modules.md`. The user prompt's `Report` section must show exactly that JSON shape.

Then name the agent in an ADW's `REQUIRED_AGENTS` and call it.

## Rules that do not bend

- ADW scripts name **agents**, never models. Swapping a model is a config edit and touches no Python.
- One agent, one prompt, one purpose. If an entry needs two purposes, it is two agents.
- Output types never appear in config — they live at the call site, paired with the user prompt.

Full spec: `references/config.md`.

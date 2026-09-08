# Config Reference

The full `sssf.config.yaml` spec: every field, how defaults merge, and how model / thinking / tools / extensions map onto the coding agent.

It lives at **`adws/adw_sssf_config/sssf.config.yaml`** — the default path every `adw_*.py` and the justfile resolve, and where `install.py` / `make_config.py` stamp it. Pass `--config <path>` to any ADW (or set `SSSF_CONFIG` for the justfile) to run against a different roster.

## Shape

```yaml
defaults:
  coding_agent: claude_code
  model: sonnet
  thinking: medium
  harness_engineering: []
  tools: [read, bash, edit, write, grep, find]
  data_dir: adws/adw_data

observability:
  db: adws/adw_data/sssf.db
  poll_ms: 500

agents:
  - name: planner
    thinking: high
    color: "#a78bfa"
    purpose: Turn a request into a plan the builder can implement without asking questions.
    prompt_engineering:
      system: adws/adw_data/prompt_engineering/planner/system.md
      user: adws/adw_data/prompt_engineering/planner/user.md
    tools:
      - read
      - bash

  - name: reviewer
    coding_agent: codex
    model: gpt-5.6-terra
    thinking: high
    tools: null
    writes: []
```

## Fields

### `defaults`

| Field | Type | Meaning |
|---|---|---|
| `coding_agent` | `pi` \| `claude_code` \| `codex` \| `kiro_cli` \| `antigravity` | Which interface runs the agent, resolved through `adw_modules/harnesses.py`. All five are implemented; see [Harnesses](#harnesses) below. |
| `model` | string | Harness-specific model id. Default `sonnet` for the default Claude Code harness. Pi uses ids registered in `~/.pi/agent/models.json`. |
| `thinking` | enum | Reasoning effort — see below. Default `medium`. |
| `color` | hex string | Lane color for every agent that does not set its own. Default empty — the visualizer falls back to its own palette. |
| `harness_engineering` | list[string] | Coding-agent extensions. Pi: extension names. Claude Code: reserved (MCP, hooks). |
| `tools` | list[string] | Roster-wide tool allowlist. Every agent that omits its own `tools` inherits this. Unset = all tools usable. |
| `protected_files` | list[string] | Paths **no** agent may modify unless it names them in its own `writes`. Default: `adws/adw_modules/`, `adws/adw_sssf_config/`, `adws/adw_*.py` — an agent must not be able to edit the machinery that decides whether its work passed. |
| `data_dir` | path | Runtime home. Sessions land at `{data_dir}/sessions/{adw_id}/{agent_name}/`. Default `adws/adw_data`. |

### `observability`

| Field | Type | Meaning |
|---|---|---|
| `db` | path | SQLite trace db. `tracer.py` writes it directly; the visualizer polls it. Default `adws/adw_data/sssf.db`. |
| `poll_ms` | int | Visualizer live-poll cadence in ms. History uses the same queries, lazy-paged. Default `500`. |

### `agents[]`

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | The identifier ADW scripts use. **ADWs name agents, never models.** |
| `purpose` | yes | One sentence: what this agent is for. Should match its `system.md` Purpose. |
| `prompt_engineering.system` | yes | Path to the system prompt — who the agent is, its single purpose, its output contract. |
| `prompt_engineering.user` | yes | Path to the default user prompt — the task template with `{{prompt}}`, `{{previous_envelope}}`, `{{context_handoff_dir}}`. |
| `color` | no | Hex swatch (`"#a78bfa"`) for this agent's lane in the visualizer. Travels config → `agent_sessions.color` → `/api/sessions/:adw_id`, and rides the `agent_start` event so a lane is colored while the agent is still running. Unset = the UI's fallback palette. |
| `coding_agent`, `model`, `thinking`, `color`, `harness_engineering` | no | Override the corresponding `defaults` key. |
| `tools` | no | Allowlist. **Omitting the key means all tools usable.** A capability list, not a boundary — see `writes`. |
| `writes` | no | What this agent may modify **in the repo**, enforced after every harness turn. Omitted = unrestricted (still barred from `protected_files` and active prompts). `[]` = no repo writes at all. A list = only those paths: a trailing `/` is a directory prefix, `*` matches within one path segment, `**` crosses segments, anything else is an exact path. Naming a `protected_files` path here is what unlocks it. Active system/user prompts require an exact entry; broad globs never unlock them. **Only the current `{data_dir}/sessions/{adw_id}/` runtime is always writable** — `writes: []` means read-only with respect to the repo, not unable to write its own report. |

Output types are deliberately absent: config defines who an agent *is*; the ADW call site defines how it's *used*. One agent serves many calls — same system prompt, different user prompt + output type per call.

## Defaults merging

`agents.py` merges each entry **over** `defaults`, key by key. An entry states only what differs; anything unset inherits. `agents.validate(cfg, REQUIRED_AGENTS)` then confirms every name an ADW declares exists, resolves to a usable coding agent + model, and has both prompt files present on disk. Any miss fails the run immediately — **no agent is ever spawned against a half-valid config.**

## Thinking levels

Pi's reasoning-effort ladder, lowest to highest:

```
off | minimal | low | medium | high | xhigh | max
```

For Pi, mapped to its reasoning effort control and honored when the model is registered with `reasoning: true` in `~/.pi/agent/models.json`. On a non-reasoning model the setting is inert — no error, no effect. Rough guidance: `high`/`xhigh` for planners and reviewers, `medium` for builders, `low` for mechanical read-and-report agents.

For Claude Code the same value goes to `claude --effort`, but Claude Code's own accepted range is narrower than Pi's — `low | medium | high | xhigh | max` only, no `off`/`minimal`. `ClaudeCodeAdapter.validate()` rejects a `claude_code` agent set to `off` or `minimal` with an objective error at config-validation time, rather than letting the CLI reject it mid-run. For Codex the value goes to `-c model_reasoning_effort=<value>`; that CLI is not pre-validated the same way — an effort level the model doesn't support there is Codex's own problem to reject.

For Kiro CLI the value goes to `kiro-cli chat --effort`, whose documented range matches Claude Code's (`low | medium | high | xhigh | max`), and `KiroCliAdapter.validate()` rejects `off`/`minimal` the same way. Pre-validation matters more here than anywhere else: **Kiro CLI does not reject an effort level it cannot use.** `--effort off` is accepted and silently ignored, so without validate() a typo becomes whatever the operator's `chat.modelDefaults` says, with nothing in the trace to show it. Two further limits belong to the model rather than the flag: several Kiro models expose no effort control at all, and others accept a shorter ladder (`claude-sonnet-4.6` has no `xhigh`). Kiro clamps in those cases rather than failing.

For Antigravity, effort is **not** a flag you normally set: `agy` sells it as part of the model slug. `gemini-3.8-flash-low`, `-medium`, and `-high` are three separate catalog entries, not one model with a knob, and passing `--effort` alongside such a slug is a hard error rather than a preference the CLI reconciles:

```
agy exited 1: invalid model selection (--model "gemini-3.6-flash-medium" --effort "low"):
--model gemini-3.6-flash-medium conflicts with --effort=low
```

So the adapter sends `--effort` only for a slug that carries no tier, and `AntigravityAdapter.validate()` **rejects a `thinking` that contradicts the slug's tier** rather than letting the slug quietly win — a roster that says `low` while `medium` runs is the same class of lie as a harness that ignores `--model`. Set `thinking` to match the tier in the model name, or pick the variant you actually want. `--effort`'s own documented range is `low | medium | high`, the narrowest of any harness here, so `off`, `minimal`, `xhigh`, and `max` are rejected too.

## Harnesses

`coding_agent` selects an adapter through `adw_modules/harnesses.py`'s registry — `agents.py` never branches on the CLI name itself, only on the common `HarnessAdapter` contract (`validate()`, `run()`). Five are implemented:

| `coding_agent` | Adapter | CLI invocation | Session continuation |
|---|---|---|---|
| `pi` | `agent_pi.PiAdapter` | `pi -p --mode json` | `--session-id <id>` (creates or continues) |
| `claude_code` | `agent_claudecode.ClaudeCodeAdapter` | `claude -p --output-format stream-json` | `--resume <uuid>`, once a real Claude Code session UUID exists |
| `codex` | `agent_codex.CodexAdapter` | `codex exec --json` | `codex exec resume <thread_id> --json`, once a real Codex thread id exists |
| `kiro_cli` | `agent_kirocli.KiroCliAdapter` | `kiro-cli chat --agent-engine v3 --output-format stream-json` | `--resume-id <id>`, once a real Kiro session id exists (`sess_<uuid>` on v3, bare `<uuid>` on v2) |
| `antigravity` | `agent_antigravity.AntigravityAdapter` | `agy -p --output-format stream-json` | `--conversation <uuid>`, once a real Antigravity `conversation_id` exists |

All five run through the exact same `agents.py` code path: one `HarnessRequest` per send, one `HarnessResult` back, gates/retries/permissions/tracing identical regardless of which adapter answered. A phase's JSON-fix and gate-correction retries always continue the id the adapter itself returned in `HarnessResult.session_id` — never a placeholder `agents.py` may have offered on the first send.

**Session reuse in `agent_map.json`** requires `coding_agent`, `model`, AND permission class (`read_only`, true exactly when `writes: []`) to all match the mapped entry; changing any of them starts a fresh session rather than resuming one built under a different harness, model, or write/read-only class. The `read_only` check exists because `codex exec resume` cannot re-apply `--sandbox read-only` — a session that started writable must never be resumed once config says it should now be read-only, or it silently keeps the access it started with. An `agent_map.json` entry written before this field existed (no `read_only` key) is never trusted either way; it always starts fresh once.

**`harness_engineering` is Pi-only.** It carries pi extension file paths (`pi -e <path>`); no other harness has an equivalent wired up in this MVP. Setting it on a `claude_code`, `codex`, `kiro_cli`, or `antigravity` agent fails `agents.validate()` with an objective error — it is never silently ignored or converted.

**Credentials are never read or stored by SSSF.** Every adapter shells out to the CLI already logged in on the machine (`pi`, `claude`, `codex`, `kiro-cli`, `agy`) and relies entirely on that CLI's own auth state. Each binary is resolved by bare name and overridable by env var — `PI_PATH`, `CLAUDE_PATH`, `CODEX_PATH`, `KIRO_PATH`, `AGY_PATH`.

### What each harness reports back

The `HarnessResult` fields a harness can actually fill differ, and the trace shows exactly what it was told — never a number in the wrong unit:

| `coding_agent` | Tokens | Cost | Context occupancy |
|---|---|---|---|
| `pi` | yes, per component | yes, dollars | yes, with the model's window |
| `claude_code` | yes | yes, dollars | yes (last turn's total) |
| `codex` | yes | no | yes (last turn's total), window unknown |
| `kiro_cli` | **no** | **no** — bills credits, reported in `usage.credits` | yes, exact: summed from its own context breakdown |
| `antigravity` | yes | no — bills AI credits, not exposed per turn | no — cumulative conversation usage is not occupancy |

Kiro CLI reports neither billed tokens nor dollars: it bills **credits**, per turn and explicitly unit-labelled (`_meta.kiro.promptTurnSummaries[] = {"unit": "credit", "usage": 0.0275}`). Kiro's own docs confirm nothing better exists to report — per-session token counts are "not currently available", account-level only. `usage.total_tokens` and `usage.total_cost` therefore stay 0 for a `kiro_cli` agent, so a roster's `sessions.total_tokens` will undercount if it mixes Kiro with other harnesses. The credits land in `usage.credits`, a field that exists precisely to carry a non-dollar billing unit, and are summed onto `sessions.total_credits`; the console prints them beside the tokens whenever a run billed any. They are deliberately **not** folded into `total_cost`: a credit is not a dollar, and its exchange rate is a per-model `rate_multiplier` SSSF does not know.

Occupancy, by contrast, is exact rather than estimated. `_meta.kiro.breakdown` reports absolute token counts per component — `contextFiles`, `kiroResponses`, `sessionFiles`, `tools` (itself split into `builtin`/`mcp`), and `yourPrompts` — and `context_tokens` is their sum, with the ceiling coming from `context_window_tokens` in the model catalog.

**Antigravity fails intermittently in a large workspace, and SSSF does not retry it.** Measured over nine identical runs differing only in cwd: `agy` returned `status: ERROR` with `Internal error encountered.` or `The stream was interrupted.` in five of them, every failure inside a ~380-file repository, while a small or empty directory passed. It is not deterministic — the same directory failed and then passed on consecutive attempts — and it is not the prompt, which was byte-identical. The mechanism is upstream: `agy` ingests the workspace, and the larger the resulting context, the likelier the stream dies mid-turn. One review of a one-line change burned 267k tokens, which is the same cause seen from the cost side.

The adapter surfaces this as a `RuntimeError` naming the CLI's own message and lets the phase fail. It deliberately does **not** auto-resume the conversation, even though the error text invites it: a turn killed by an upstream stream timeout can leave the trajectory in a state where every subsequent request on that `conversation_id` fails, so an automatic retry risks converting one lost phase into a permanently unusable session. Re-run the workflow with `--adw-id` instead, which starts the failed phase over while keeping the phases that already passed.

The sibling `contextUsage.usagePercentage` is deliberately **not** used to derive occupancy, and this is worth knowing if you read the raw stream and wonder why the numbers disagree with the UI's. Measured on 2.21.0, that percentage reconciles with neither the breakdown nor the catalog: two snapshots inside a single run reported 9366 tokens at 0.90% and 7008 tokens at 10.83%, implying context ceilings of ~1.04M and ~65k for a model the catalog calls 200k. Multiplying the percentage by the window overstated occupancy roughly threefold, so the per-component counts win and the percentage is ignored.

### Kiro CLI: why the engine is pinned to v3

`KiroCliAdapter` passes `--agent-engine v3` on every turn instead of letting the CLI pick. Measured on Kiro CLI 2.21.0:

- **v1** rejects `--output-format stream-json` outright.
- **v2** accepts `--model` and `--effort` and then silently drops both. An unknown model only logs `[warn] failed to set model ...: Method not found` and runs the settings default at exit 0; `--effort low`, `high`, and `max` all report back the value from `chat.modelDefaults`. On v2 the roster would be decoration.
- **v3** honors `--model` and fails loudly on an unknown one (`InvalidModelError`, exit 1), which is what `agents.validate()` and the run path are written against.

The v3 stream is chattier than v2's, and mixes plain log lines into stdout; the adapter skips anything that is not JSON and forwards only completed tool calls. KAS's own startup logging goes to stderr, which is drained on a background thread like every other adapter's.

A mixed roster is the default: Claude Code Sonnet handles planner, builder, scout, and documenter; Codex GPT-5.6 Terra handles review. Because Codex has no tool-allowlist flag, its agent must set `tools: null` instead of inheriting the Claude Code list:

```yaml
agents:
  - name: planner
    thinking: high
    prompt_engineering:
      system: adws/adw_data/prompt_engineering/planner/system.md
      user: adws/adw_data/prompt_engineering/planner/user.md

  - name: reviewer
    coding_agent: codex
    model: gpt-5.6-terra
    thinking: high
    tools: null
    writes: []
    prompt_engineering:
      system: adws/adw_data/prompt_engineering/reviewer/system.md
      user: adws/adw_data/prompt_engineering/reviewer/user.md
```

Both CLIs must be installed and logged in before running the starter roster. SSSF reads no credentials for either.

Swapping an agent onto Kiro CLI or Antigravity is the same three-line edit, plus the `tools: null` every harness without an allowlist flag requires:

```yaml
agents:
  - name: builder
    coding_agent: kiro_cli
    model: claude-sonnet-4.6       # an id from `kiro-cli chat --list-models`
    thinking: high                 # low | medium | high | xhigh | max
    tools: null                    # Kiro has no mappable allowlist — see Tools
    prompt_engineering:
      system: adws/adw_data/prompt_engineering/builder/system.md
      user: adws/adw_data/prompt_engineering/builder/user.md

  - name: reviewer
    coding_agent: antigravity
    model: gemini-3.8-flash-medium # a slug from `agy models`; the tier is IN the slug
    thinking: medium               # MUST match the slug's tier — agy rejects the pair
    tools: null                    # agy headless has no allowlist flag
    writes: []                     # still enforced by permissions.py, not by agy
    prompt_engineering:
      system: adws/adw_data/prompt_engineering/reviewer/system.md
      user: adws/adw_data/prompt_engineering/reviewer/user.md
```

## Model resolution

Model syntax belongs to the selected harness. Claude Code accepts aliases such as `sonnet`; Codex accepts its own model ids such as `gpt-5.6-terra`. Kiro CLI accepts the ids from `kiro-cli chat --list-models --format json` (`claude-sonnet-4.6`, `claude-haiku-4.5`, `gpt-5.6-terra`, `auto`, …). Antigravity accepts the slugs from `agy models`, where the effort tier is part of the slug (`gemini-3.8-flash-medium`, `gemini-3.6-flash-low`, `gemini-3.1-pro-high`, …). Both are checked against the CLI's own catalog by `validate()` before anything spawns, and in both cases an empty catalog means the CLI is missing, unauthenticated, or offline, and is treated as "unknown" rather than reported as a bad model name — failing a valid roster because the network was down would send you after the wrong bug. Note that `agy models` fetches **per account**, so its list is what your login is entitled to rather than a fixed roster. For Pi, always write `provider/model-id`: Pi resolves it against `~/.pi/agent/models.json` plus its built-in providers, and a bare id that matches several raises at resolution:

```
agent 'scout': model pattern 'gemini-3.6-flash' is ambiguous:
  [('google', 'gemini-3.6-flash'), ('openrouter', 'google/gemini-3.6-flash'), ...]
```

That is `agents.validate()` doing its job — it fails before anything spawns rather than silently billing the wrong provider. Qualifying the Pi model is the whole fix: `google/gemini-3.6-flash`, `openai/gpt-5.6-terra`, or `fireworks/accounts/fireworks/models/kimi-k3`. The leading segment is matched against the provider list first, so the rest of the string can contain slashes.

Other consequences worth knowing:

- A Pi model must be in its catalog before an agent can name it. An unknown id fails at resolution, before spawn. `pi --list-models` is the catalog the Pi adapter reads.
- **Ambiguity can appear without you touching the config.** Registering a new provider that carries a model you already use turns a formerly-fine bare pattern ambiguous. If a roster stops validating and nobody edited it, that is why.
- Provider credentials come from the environment, not the config — the key that matches the provider you named (`GEMINI_API_KEY` for `google/...`, `OPENROUTER_API_KEY` for `openrouter/...`).
- The resolved model is recorded per session in `agent_map.json` and mirrored into the `agent_sessions` table. **Changing an agent's model invalidates its session**: a joined run starts that agent fresh instead of resuming a context window built by a different model.

## Tools

For Pi, `tools` maps to `pi --tools`. Pi's seven builtin tool names are:

| Tool | Purpose | Pi's own default |
|---|---|---|
| `read` | read file contents | on |
| `bash` | execute bash commands | on |
| `edit` | find/replace edits | on |
| `write` | create/overwrite files | on |
| `grep` | search file contents | **off** |
| `find` | find files by glob | **off** |
| `ls` | list directory contents | **off** |

`grep`, `find`, and `ls` are off in bare Pi, so an agent that does not name them will shell out through `bash` to do the same work. The starter roster instead targets Claude Code and sets `defaults.tools` to the six names its adapter maps directly.

**Resolution order:** an agent's own `tools` list wins; an agent that omits the key inherits `defaults.tools`; if neither is set, `tools` stays `None` and all tools are usable. An empty list is not "all tools" — it is a tool-less agent, and it will stall.

**On `coding_agent: claude_code`**, `tools` is translated through `agent_claudecode.TOOL_MAP` — a small, direct table, not a universal tool language:

| Pi | Claude Code |
|---|---|
| `read` | `Read` |
| `write` | `Write` |
| `edit` | `Edit` |
| `bash` | `Bash` |
| `grep` | `Grep` |
| `find` | `Glob` |

`ls` has no Claude Code equivalent (directory listing goes through `Bash` or `Glob` there) and is deliberately unmapped — naming it for a `claude_code` agent fails `agents.validate()` with an objective error rather than being dropped or guessed at. The translated names are sent to both `claude --tools` and `claude --allowedTools` as one comma-joined argument (`"Read,Bash,Edit"`). The second flag pre-approves exactly the configured tools because non-interactive print mode cannot answer permission prompts; repository write boundaries remain enforced after the run by `permissions.enforce()`. The prompt is sent through stdin so the variadic options cannot consume it.

**On `coding_agent: codex`**, `tools` has no CLI flag to map onto, and none is invented — instead of silently ignoring it, `agents.validate()` fails a codex agent whose effective `tools` (its own, or inherited from `defaults.tools`) isn't `None`. Set `tools: null` explicitly on a codex agent to opt out. Instead, an agent with `writes: []` (declared read-only) gets Codex's own `--sandbox read-only` on its first turn, as defense in depth alongside the `writes`/`protected_files` enforcement every coding_agent gets from `permissions.py` regardless. `codex exec resume` has no `--sandbox` flag, so a resumed turn can't re-assert it — agents.py's session-identity check (below) is what keeps this safe: a session mapped as writable is never resumed once the agent's `writes` says it should now be read-only, so that case always gets a fresh (sandboxable) thread instead.

**On `coding_agent: kiro_cli`**, `tools` is rejected the same way, for a sharper reason. Kiro CLI does have a session-level flag, `--trust-tools`, but on the v3 engine it takes KAS's own tool ids rather than Pi's vocabulary, and **a name it does not recognize denies the tool instead of allowing it.** Measured on 2.21.0: `--trust-tools=shell` makes the agent's shell call come back `The user rejected this tool call`, while `--trust-tools=run_command` runs it. A guessed mapping would therefore silently disarm the agent rather than merely mis-describe it, which is worse than having no flag at all — so `agents.validate()` fails a `kiro_cli` agent whose effective `tools` isn't `None`, and no table is invented.

Every Kiro turn instead runs with `--trust-all-tools`. That is deliberate: non-interactive mode has nobody to answer a trust prompt, a refused call comes back rejected, **and the run still exits 0** — so without the flag an agent reports success on a phase in which its tools were quietly denied. Repo write scope is unaffected; it is enforced after the call by `permissions.enforce()` exactly as for every other harness. `writes: []` selects no native sandbox here, because Kiro CLI has none to select, and a read-only agent still has to write its own report into `context_handoff/`.

**On `coding_agent: antigravity`**, `tools` is rejected for the same reason as Codex: headless `agy` has no allowlist flag. Scoping exists, but it lives in `permissions.allow` rules in `~/.gemini/antigravity-cli/settings.json` — the CLI's own file, which SSSF does not own and must not rewrite. Set `tools: null` on an antigravity agent to opt out of an inherited `defaults.tools`.

Every Antigravity turn runs with `--dangerously-skip-permissions`, and the reason is a specific trap: headless mode **soft-denies** a tool it cannot get approval for — the run continues, exits 0, and only prints a notice to stderr. Shell commands default to Ask. Without the flag a builder would report success on a phase whose commands were refused, which is a green trace over work that never happened. An operator who wants narrower grants writes `permissions.allow` rules in the CLI's settings; those are consulted before this flag and still apply. `agy --sandbox` is **not** used for `writes: []`: it restricts terminal commands, not filesystem access, so treating it as Codex's `--sandbox read-only` would claim a guarantee it does not give.

## Write permissions — `writes` and `protected_files`

`tools` cannot express a safety boundary, because two of the tools are general
purpose. `bash` runs anything, including `git checkout`, which discards an
engineer's uncommitted work; `write` reaches any path, not only the one report
file an agent was granted it for. So "this agent changes nothing" is a claim a
tool list can state but never keep.

`adw_modules/permissions.py` keeps it, the same way every other claim in this
system is kept — after the fact, against the repo. Every harness turn gets its
own before/after snapshot, checked before parsing, gates, or retries. The
snapshot records binary contents, file type, and mode for tracked files,
ordinary untracked files, and all active prompts inside the repo. Any path that
appeared, vanished, or changed is attributed to that turn.

Prompt paths inside the repository must be direct: `agents.validate()` scans
the full roster and rejects a system/user prompt whose file or parent directory
is a symlink, even when that agent is not required by the current ADW. This prevents
an apparently protected prompt path from redirecting reads to an unprotected
target. The observability database configured by `observability.db` and its
SQLite sidecars are excluded precisely because tracer callbacks write them
during the harness turn; that framework exception does not grant the agent
write permission to the rest of `data_dir`.

Comparing change-sets rather than watching writes is deliberate: a path that was
modified before the agent ran and is clean afterwards has been **reverted**, and
a reversion is a modification. That is what catches `git checkout`.

A breach is not a gate violation. Gates are for work an agent can be asked to
redo; a write has already happened, so re-prompting fixes nothing. Instead:

1. every unauthorized change is rolled back to its exact pre-turn contents,
   type, and mode; a newly created file or symlink is removed exactly;
2. tracked dirty files and pre-existing untracked files are restored to the
   operator's pre-turn bytes, rather than to `HEAD`;
3. the phase fails and names every path with what happened to it.

```yaml
defaults:
  protected_files: [adws/adw_modules/, adws/adw_sssf_config/, "adws/adw_*.py"]

agents:
  - name: builder      # no `writes` key -> unrestricted, minus protected_files
  - name: scout
    writes: []         # no repo writes; its findings still land in context_handoff/
  - name: planner
    writes: [specs/]
  - name: documenter
    writes: [app_docs/, docs/, "**/*.md", "*.md"]
```

**Only the current session runtime at `{data_dir}/sessions/{adw_id}/` is always
writable.** `context_handoff/` is how agents hand work to each other, and each
agent's saved prompts, `raw_output.jsonl`, and `envelope.json` sit beside it.
Sibling sessions and the rest of `data_dir` receive no blanket grant. Active
configured system/user prompts are protected even when ignored; only an exact
path in that agent's `writes` can unlock one, so the documenter's Markdown
globs cannot rewrite its instructions. Paths outside the repository are not
read or restored by this post-execution check and need an external sandbox or
filesystem policy when they require protection.

Narrow by role, not by reflex. Anything that must produce a `context_handoff/` artifact needs `write`, or it will resort to a `bash` heredoc. Withhold `edit`/`write` only where the restriction *is* the guarantee — a reviewer that cannot edit cannot quietly fix what it was asked to report.

### Extension tools must be named explicitly

`pi --tools` is an allowlist over **built-in, extension, and custom tools alike** — not just builtins. So the moment an agent has a `tools` list at all (its own, or one inherited from `defaults`), any tool registered by its `harness_engineering` extensions is **excluded unless it appears in that list by name**.

This fails quietly. The extension still loads, the run still succeeds, and the tool the extension exists to provide is simply never offered to the model — you find out by noticing the agent never called it.

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
      - ast_query                       # REQUIRED — the extension's tool, named or lost
```

Rule: **every entry in `harness_engineering` that registers a tool must have that tool name added to the agent's `tools` list.** Adding an extension is therefore a two-line change, never one. The alternative is dropping the `tools` key *and* leaving `defaults.tools` unset so the agent resolves to `None` (all tools) — but with a roster-wide `defaults.tools` in place, that escape hatch is closed; naming the tool is the only path.

## Harness engineering

`harness_engineering` entries are pi extension **file paths**, passed through as `pi -e <path>`, one flag per entry, scoped to that agent only. This is where per-agent harness changes live — e.g. an output-tightening extension for an agent that keeps wrapping its envelope in prose. The starter roster ships with none. On Claude Code the field is reserved for MCP config and hooks in v2.

**If the extension registers a tool, name that tool in the agent's `tools` list too** — `--tools` filters extension tools exactly like builtins, so an unnamed extension tool is silently unavailable no matter that the extension loaded fine. See [Extension tools must be named explicitly](#extension-tools-must-be-named-explicitly) above. Extensions that only shape output or add flags (no tool registration) need no `tools` change.

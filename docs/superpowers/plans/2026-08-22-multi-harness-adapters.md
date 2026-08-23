# Multi-harness Adapters Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Pi, Claude Code, and Codex as interchangeable per-agent CLI backends while preserving typed envelopes, gates, retries, permissions, tracing, and existing workflows.

**Architecture:** Introduce typed harness contracts, a registry, shared process streaming, and one adapter per CLI. Keep orchestration in `agents.py`; each adapter owns native flags, JSONL parsing, tool translation, sandbox settings, usage extraction, and session creation or resumption.

**Tech Stack:** Python 3.11+, Pydantic v2, PyYAML, subprocess JSONL streams, SQLite tracing, Vue 3, TypeScript, Bun, Claude Code CLI, Codex CLI, Pi CLI.

## Global Constraints

- Implement only the multi-harness milestone described in `docs/superpowers/specs/2026-08-22-multi-harness-adapters-design.md`.
- Do not add worktrees, per-run branches, merge, push, pull-request creation, remote sandboxes, fan-out, or credential management.
- Do not create an automated test suite; use the exact static checks and real smoke tests listed below.
- Do not run `git commit`, `git push`, `git tag`, `git merge`, or `git rebase` without explicit user confirmation.
- Candidate commit commands are checkpoints only; stop and request confirmation before executing them.
- Preserve Pi behavior and treat legacy `agent_map.json` entries without `coding_agent` as Pi sessions.
- Use installed, already-authenticated CLIs; never read, copy, print, or persist credentials.
- Reject unsupported harness, model-effort, capability, extension, and permission combinations without fallback.
- Keep functions between 4 and 20 lines, files below 500 lines, indentation at two levels or less, and public functions documented with intent plus one usage example.
- Use explicit types; do not introduce `Any`, `Dict`, untyped functions, or duplicated command/process logic.
- Do not add runtime dependencies.
- Keep deterministic test, lint, typecheck, and build phases independent of models.
- Run all CLI processes with an explicit repository `cwd` so a later worktree milestone can replace it without adapter changes.

## File Map

### Create

- `.claude/skills/sssf/templates/adws/adw_modules/harness_types.py` — shared names, requests, results, events, usage, protocols, and typed errors.
- `.claude/skills/sssf/templates/adws/adw_modules/harness_capabilities.py` — legacy aliases, portable capability validation, native tool translation, and sandbox inference.
- `.claude/skills/sssf/templates/adws/adw_modules/harness_process.py` — one JSONL subprocess runner with raw-output persistence and lifecycle callbacks.
- `.claude/skills/sssf/templates/adws/adw_modules/harness_registry.py` — adapter registration and lookup.
- `.claude/skills/sssf/templates/adws/adw_modules/agent_codex.py` — Codex command construction, event parsing, usage, schema, sandbox, and session resumption.

### Modify

- `.claude/skills/sssf/templates/adws/adw_modules/data_types.py` — add Codex and `effort`; remove Pi-specific request/result types.
- `.claude/skills/sssf/templates/adws/adw_modules/agent_pi.py` — implement the common adapter contract.
- `.claude/skills/sssf/templates/adws/adw_modules/agent_cc.py` — replace the stub with the Claude Code adapter.
- `.claude/skills/sssf/templates/adws/adw_modules/agents.py` — route phases through the registry and normalize session handling.
- `.claude/skills/sssf/templates/adws/adw_modules/tracer.py` — persist the effective harness on upsert and normalized metadata in events.
- `.claude/skills/sssf/templates/adws/adw_modules/console.py` — show harness and tolerate a session ID learned after process start.
- `.claude/skills/sssf/templates/sssf.config.yaml` — document preferred `effort`, legacy aliases, and portable capabilities.
- `.claude/skills/sssf/templates/env.sample` — document optional executable-path overrides without adding credentials.
- `.claude/skills/sssf/scripts/install.py` — report CLI availability and versions after stamping.
- `.claude/skills/sssf/apps/visualizer/shared/types.ts` — add `effort` while retaining `thinking` compatibility.
- `.claude/skills/sssf/apps/visualizer/src/components/PhaseDetail.vue` — render effective effort.
- `README.md`, `.claude/skills/sssf/SKILL.md`, relevant cookbooks and references — replace Pi-only guidance with the implemented contract.

---

### Task 1: Shared contracts, configuration compatibility, and capabilities

**Files:**
- Create: `.claude/skills/sssf/templates/adws/adw_modules/harness_types.py`
- Create: `.claude/skills/sssf/templates/adws/adw_modules/harness_capabilities.py`
- Modify: `.claude/skills/sssf/templates/adws/adw_modules/data_types.py:300-447`
- Modify: `.claude/skills/sssf/templates/adws/adw_modules/agents.py:28-43`

**Interfaces:**
- Produces: `HarnessName`, `PortableCapability`, `HarnessProfile`, `HarnessRequest`, `HarnessResult`, `HarnessEvent`, `UsageBreakdown`, `HarnessAdapter`, and typed `HarnessError` subclasses.
- Produces: `resolve_capabilities(profile: HarnessProfile) -> ResolvedCapabilities` and `infer_sandbox(writes: list[str] | None) -> SandboxAccess`.
- Consumes: existing YAML keys `coding_agent`, `model`, `thinking`, `tools`, `writes`, and `harness_engineering`.

- [ ] **Step 1: Record the clean baseline**

Run:

```bash
git status --short
uv run python -m compileall -q .claude/skills/sssf/templates/adws/adw_modules
```

Expected: only the approved spec and plan are untracked; compilation exits 0.

- [ ] **Step 2: Define the common types and errors**

Create contracts with these exact public shapes:

```python
HarnessName = Literal["pi", "claude_code", "codex"]
PortableCapability = Literal["read", "search", "shell", "write", "edit"]
SandboxAccess = Literal["read-only", "workspace-write"]
ProcessCallback = Callable[[int], None]

class HarnessProfile(BaseModel):
    harness: HarnessName
    model: str
    effort: str
    tools: list[str] | None = None
    extensions: list[str] = Field(default_factory=list)
    writes: list[str] | None = None

class HarnessRequest(BaseModel):
    profile: HarnessProfile
    prompt: str
    system_prompt: str
    output_schema: dict[str, object]
    session_id: str | None = None
    runtime_dir: str
    raw_output_path: str
    cwd: str

class HarnessEvent(BaseModel):
    name: str
    payload: dict[str, object] = Field(default_factory=dict)
    started_at: str | None = None
    ended_at: str | None = None

class HarnessResult(BaseModel):
    text: str = ""
    returncode: int = 0
    session_id: str
    usage: UsageBreakdown = Field(default_factory=UsageBreakdown)
    context_tokens: int = 0
    context_window: int = 0

class ResolvedCapabilities(BaseModel):
    portable: frozenset[PortableCapability]
    native_tools: tuple[str, ...]
    sandbox: SandboxAccess
```

Define `HarnessConfigError`, `HarnessUnavailableError`, `HarnessProcessError`, and `HarnessProtocolError`, each formatting the offending harness/value and expected shape.

- [ ] **Step 3: Define the adapter protocol**

```python
class HarnessAdapter(Protocol):
    name: HarnessName

    def validate_profile(self, profile: HarnessProfile) -> None: ...
    def new_session_id(self, adw_id: str, agent_name: str) -> str | None: ...
    def run(
        self,
        request: HarnessRequest,
        on_event: Callable[[HarnessEvent], None] | None = None,
        on_spawn: Callable[[int], None] | None = None,
        on_exit: Callable[[int], None] | None = None,
    ) -> HarnessResult: ...
```

Move `UsageBreakdown` from `data_types.py` into `harness_types.py`. Keep its `merge()` behavior, and move Pi-specific turn parsing into `agent_pi.py`.

- [ ] **Step 4: Normalize `effort` without breaking `thinking`**

Add `codex` to both `coding_agent` literals. Introduce one shared Pydantic base validator used by `AgentConfig` and `ConfigDefaults`:

```python
class EffortConfig(BaseModel):
    effort: str | None = None
    thinking: str | None = None

    @model_validator(mode="after")
    def resolve_effort_alias(self) -> Self:
        if self.effort and self.thinking and self.effort != self.thinking:
            raise ValueError(
                f"effort={self.effort!r} conflicts with thinking={self.thinking!r}; expected equal values"
            )
        resolved = self.effort or self.thinking or "medium"
        self.effort = resolved
        self.thinking = resolved
        return self
```

Update `load_config()` so an agent-level `effort` overrides a default-level `thinking` instead of being merged as a false conflict. A conflict is only an error when both keys occur in the same YAML mapping.

- [ ] **Step 5: Implement portable capability resolution**

Use these exact mappings:

```python
LEGACY_ALIASES = {"bash": "shell", "grep": "search", "find": "search", "ls": "search"}
CLAUDE_TOOLS = {
    "read": ("Read",),
    "search": ("Grep", "Glob"),
    "shell": ("Bash",),
    "write": ("Write",),
    "edit": ("Edit",),
}
CODEX_INSPECTION = frozenset({"read", "search", "shell"})
CODEX_MUTATION = frozenset({"read", "search", "shell", "write", "edit"})
```

`tools=None` resolves to the full portable set. Pi may additionally retain tool names registered by its own non-empty extension list. Claude Code and Codex reject all extension tool names. Codex accepts inspection only with `writes: []`, and mutation only with writes omitted or non-empty.

`resolve_capabilities(profile)` returns `ResolvedCapabilities`. `infer_sandbox(writes)` returns `"read-only"` only for `writes == []`; a path list or omitted key returns `"workspace-write"`.

- [ ] **Step 6: Verify types and alias behavior without creating test files**

Run:

```bash
uv run --with pydantic --with pyyaml python - <<'PY'
import sys
sys.path.insert(0, ".claude/skills/sssf/templates/adws")
from adw_modules.data_types import AgentConfig, PromptEngineering

prompt = PromptEngineering(system="system.md", user="user.md")
legacy = AgentConfig(name="legacy", thinking="high", prompt_engineering=prompt)
modern = AgentConfig(name="modern", coding_agent="codex", effort="xhigh", prompt_engineering=prompt)
assert legacy.effort == legacy.thinking == "high"
assert modern.effort == modern.thinking == "xhigh"
try:
    AgentConfig(name="invalid", effort="low", thinking="high", prompt_engineering=prompt)
except ValueError as error:
    assert "conflicts" in str(error)
else:
    raise AssertionError("conflicting effort aliases were accepted")
PY
```

Expected: exit 0 with no output.

- [ ] **Step 7: Prepare the checkpoint, but do not commit without approval**

```bash
git diff --check
git status --short
# Candidate only after explicit approval:
# git add .claude/skills/sssf/templates/adws/adw_modules/{harness_types.py,harness_capabilities.py,data_types.py,agents.py}
# git commit -m "refactor: define backend contracts and capabilities"
```

### Task 2: Shared JSONL process runner and Pi adapter

**Files:**
- Create: `.claude/skills/sssf/templates/adws/adw_modules/harness_process.py`
- Modify: `.claude/skills/sssf/templates/adws/adw_modules/agent_pi.py`

**Interfaces:**
- Consumes: `HarnessRequest`, `HarnessResult`, `HarnessEvent`, and lifecycle callbacks from Task 1.
- Produces: `run_jsonl_process(spec: ProcessSpec, on_json: JsonEventCallback, on_spawn: ProcessCallback | None, on_exit: ProcessCallback | None) -> ProcessOutcome`.
- Produces: `PiAdapter` and module constant `ADAPTER`.

- [ ] **Step 1: Add one strict JSONL subprocess runner**

Define `ProcessSpec` and `ProcessOutcome`, then implement one runner that writes stdout lines immediately, sends the prompt through stdin when requested, sends stderr to a file to avoid pipe deadlocks, and always invokes `on_exit` in `finally`:

```python
JsonEventCallback = Callable[[dict[str, object]], None]

class ProcessSpec(BaseModel):
    command: list[str]
    cwd: str
    raw_output_path: str
    stderr_output_path: str
    stdin_text: str | None = None

class ProcessOutcome(BaseModel):
    returncode: int
    stderr_tail: str = ""
```

A non-empty stdout line that is not JSON raises `HarnessProtocolError` after being preserved in the raw file. A non-zero process without a usable final result raises `HarnessProcessError` with executable, exit code, and the final 800 stderr characters.

- [ ] **Step 2: Refactor Pi onto the shared contracts**

Keep `resolve_model()`, context-window discovery, and `ToolCallTracker`. Replace `PiRequest`/`PiResult` with common types and expose:

```python
class PiAdapter:
    name: HarnessName = "pi"

    def validate_profile(self, profile: HarnessProfile) -> None:
        ensure_executable(PI_PATH, self.name)
        require_effort(profile.effort, PI_EFFORTS, self.name)
        resolve_model(profile.model)
        resolve_capabilities(profile)

    def new_session_id(self, adw_id: str, agent_name: str) -> str:
        return f"sssf-{adw_id}-{agent_name}-{new_id(4)}"

ADAPTER = PiAdapter()
```

Implement `run()` with the exact `HarnessAdapter.run` signature from Task 1; its body delegates process I/O to `run_jsonl_process()` and only retains Pi event/usage parsing.

Pi effort values remain `off`, `minimal`, `low`, `medium`, `high`, `xhigh`, and `max`. Translate portable names back to Pi names while retaining registered extension tools.

- [ ] **Step 3: Emit normalized Pi events and usage**

`ToolCallTracker.observe()` should return `HarnessEvent | None`. The adapter converts Pi `message_end` usage into `UsageBreakdown`, sets the final assistant text, and returns the request session ID unchanged.

- [ ] **Step 4: Run static verification**

```bash
uv run python -m compileall -q .claude/skills/sssf/templates/adws/adw_modules
command -v pi >/dev/null && pi --version || printf '%s\n' 'SKIP: Pi is not installed in this environment'
git diff --check
```

Expected in the current environment: Python compilation passes and Pi reports the explicit skip.

- [ ] **Step 5: Prepare the checkpoint, but do not commit without approval**

```bash
# Candidate only after explicit approval:
# git add .claude/skills/sssf/templates/adws/adw_modules/{harness_process.py,agent_pi.py}
# git commit -m "refactor: normalize command-line process execution"
```

### Task 3: Claude Code adapter

**Files:**
- Modify: `.claude/skills/sssf/templates/adws/adw_modules/agent_cc.py`

**Interfaces:**
- Consumes: Task 1 contracts, Task 1 Claude tool mapping, and Task 2 process runner.
- Produces: `ClaudeCodeAdapter` and module constant `ADAPTER`.

- [ ] **Step 1: Replace the stub with validation and command construction**

Use `CLAUDE_PATH = os.environ.get("CLAUDE_PATH", "claude")`. Accept efforts `low`, `medium`, `high`, `xhigh`, `max`, and `ultracode`. Require CLI version 2.1.205 or newer because structured-schema validation before that version could silently degrade.

Build new calls with `-p`, `--output-format stream-json`, `--verbose`, `--model`, `--effort`, `--system-prompt`, `--json-schema`, `--tools`, `--permission-mode dontAsk`, and `--session-id <uuid>`. Build resumed calls with `--resume <session_id>` instead of `--session-id`.

- [ ] **Step 2: Infer native sandbox settings**

Pass inline `--settings` JSON with:

```json
{
  "sandbox": {
    "enabled": true,
    "failIfUnavailable": true,
    "allowUnsandboxedCommands": false,
    "autoAllowBashIfSandboxed": true
  }
}
```

For `writes: []`, add the absolute repository root to `sandbox.filesystem.denyWrite` and omit `Write` and `Edit` from `--tools`. For mutable agents, keep writes inside the current workspace and let the existing Git diff policy enforce narrower globs.

- [ ] **Step 3: Parse stream events and final structured output**

Capture `session_id` from system/result events. Convert the final `structured_output` to `json.dumps(..., ensure_ascii=False)`; if absent, use the final `result` string so core Pydantic validation produces the authoritative error. Normalize completed `tool_use`/`tool_result` pairs into one `HarnessEvent` each.

Map usage fields into `UsageBreakdown`:

```python
usage.input_tokens = raw_usage.get("input_tokens", 0)
usage.output_tokens = raw_usage.get("output_tokens", 0)
usage.cache_read_tokens = raw_usage.get("cache_read_input_tokens", 0)
usage.cache_write_tokens = raw_usage.get("cache_creation_input_tokens", 0)
usage.total_tokens = sum((usage.input_tokens, usage.output_tokens,
                          usage.cache_read_tokens, usage.cache_write_tokens))
usage.total_cost = float(result_event.get("total_cost_usd") or 0.0)
```

- [ ] **Step 4: Verify the adapter without spending a model call**

```bash
claude --version
uv run python -m compileall -q .claude/skills/sssf/templates/adws/adw_modules/agent_cc.py
git diff --check
```

Expected: Claude Code reports at least 2.1.205 and compilation exits 0.

- [ ] **Step 5: Prepare the checkpoint, but do not commit without approval**

```bash
# Candidate only after explicit approval:
# git add .claude/skills/sssf/templates/adws/adw_modules/agent_cc.py
# git commit -m "feat: add a sandboxed streaming backend"
```

### Task 4: Codex adapter

**Files:**
- Create: `.claude/skills/sssf/templates/adws/adw_modules/agent_codex.py`

**Interfaces:**
- Consumes: Task 1 contracts and profiles plus Task 2 process runner.
- Produces: `CodexAdapter` and module constant `ADAPTER`.

- [ ] **Step 1: Implement strict profile validation**

Use `CODEX_PATH = os.environ.get("CODEX_PATH", "codex")`. Accept CLI efforts `minimal`, `low`, `medium`, `high`, and `xhigh`. Require exactly the inspection/read-only or mutation/workspace-write capability pair defined in Task 1; reject `harness_engineering` and extension tool names.

- [ ] **Step 2: Write the schema and construct new/resume commands**

Write `request.output_schema` to `<runtime_dir>/output_schema.json`. Build the common prefix exactly as:

```python
command = [
    CODEX_PATH, "exec", "--json", "--color", "never",
    "--cd", request.cwd,
    "--model", profile.model,
    "--sandbox", sandbox_access,
    "--config", 'approval_policy="never"',
    "--config", f'model_reasoning_effort={json.dumps(profile.effort)}',
    "--config", f'developer_instructions={json.dumps(request.system_prompt)}',
    "--output-schema", str(schema_path),
]
```

Append `"-"` for a new thread. For a resumed thread, append `"resume", request.session_id, "-"`. Send `request.prompt` through stdin. Never add `--dangerously-bypass-approvals-and-sandbox`.

- [ ] **Step 3: Normalize Codex JSONL**

Capture `thread_id` from `thread.started`. Capture the final text from the last completed `agent_message`. Convert `command_execution` and `file_change` items into normalized tool events. Convert `turn.completed.usage` fields into `UsageBreakdown`; leave cost and context-window fields at zero when Codex does not report them.

Raise `HarnessProtocolError` when a successful process has no thread ID or final message. Preserve `turn.failed` error details without including the prompts.

- [ ] **Step 4: Verify command support without spending a model call**

```bash
codex --version
codex exec --help | rg -- '--json|--output-schema|--sandbox|--cd|--model'
codex exec resume --help | rg -- '--json|--output-schema|--model'
uv run python -m compileall -q .claude/skills/sssf/templates/adws/adw_modules/agent_codex.py
git diff --check
```

Expected: all required flags are present and compilation exits 0.

- [ ] **Step 5: Prepare the checkpoint, but do not commit without approval**

```bash
# Candidate only after explicit approval:
# git add .claude/skills/sssf/templates/adws/adw_modules/agent_codex.py
# git commit -m "feat: add a structured sandboxed backend"
```

## Plan Continuation

Tasks 5-8 and the completion checklist continue in
[`2026-08-22-multi-harness-adapters-tasks-5-8.md`](./2026-08-22-multi-harness-adapters-tasks-5-8.md).

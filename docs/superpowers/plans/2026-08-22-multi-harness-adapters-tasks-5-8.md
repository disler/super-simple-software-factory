# Multi-harness Adapters Implementation Plan — Tasks 5-8

> Continuation of [`2026-08-22-multi-harness-adapters.md`](./2026-08-22-multi-harness-adapters.md). Its architecture and global constraints apply to every task below.

### Task 5: Registry, orchestration, sessions, and tracing

**Files:**
- Create: `.claude/skills/sssf/templates/adws/adw_modules/harness_registry.py`
- Modify: `.claude/skills/sssf/templates/adws/adw_modules/agents.py`
- Modify: `.claude/skills/sssf/templates/adws/adw_modules/tracer.py:251-270`
- Modify: `.claude/skills/sssf/templates/adws/adw_modules/console.py:99-105`

**Interfaces:**
- Consumes: `ADAPTER` from all three adapter modules.
- Produces: `get_adapter(name: HarnessName) -> HarnessAdapter`.
- Preserves: `load_config()`, `validate()`, and `execute()` call sites used by all ADWs.

- [ ] **Step 1: Register adapters without conditionals in the runner**

```python
ADAPTERS: dict[HarnessName, HarnessAdapter] = {
    "pi": agent_pi.ADAPTER,
    "claude_code": agent_cc.ADAPTER,
    "codex": agent_codex.ADAPTER,
}

def get_adapter(name: HarnessName) -> HarnessAdapter:
    adapter = ADAPTERS.get(name)
    if adapter is None:
        raise HarnessConfigError(f"harness {name!r} is unknown; expected {sorted(ADAPTERS)}")
    return adapter
```

- [ ] **Step 2: Make validation adapter-driven**

`validate()` must still check required agent names and prompt paths, then build a `HarnessProfile` and call the selected adapter's `validate_profile()`. Remove the Pi-only rejection and direct `agent_pi.resolve_model()` call.

- [ ] **Step 3: Build one common request in `execute()`**

Generate `call.output_type.model_json_schema()`, use `agent.effort`, pass the explicit `run.repo_root`, and select the adapter once per phase. Replace the Pi-specific nested `send()` with a common version that updates the active session after every successful call:

```python
def send(prompt_text: str) -> HarnessResult:
    nonlocal active_session_id, latest
    request = base_request.model_copy(update={"prompt": prompt_text,
                                              "session_id": active_session_id})
    result = adapter.run(request, on_event=forward_event,
                         on_spawn=record_spawn, on_exit=record_exit)
    active_session_id = result.session_id
    save_session(run, agent, active_session_id)
    spent.merge(result.usage)
    latest = result
    return result
```

- [ ] **Step 4: Make session matching include the harness**

```python
def reusable_session(entry: dict[str, object] | None, agent: AgentConfig) -> str | None:
    if not entry:
        return None
    recorded_harness = str(entry.get("coding_agent") or "pi")
    if recorded_harness != agent.coding_agent or entry.get("model") != agent.model:
        return None
    session_id = entry.get("session_id")
    return str(session_id) if session_id else None
```

If no reusable session exists, call `adapter.new_session_id(adw_id, agent.name)`. Effort changes do not invalidate a session. Save `coding_agent`, `model`, and effective `effort` with every session update.

Use this exact persistence helper:

```python
def save_session(run: Run, agent: AgentConfig, session_id: str) -> None:
    run.save_agent_map(agent.name, {
        "session_id": session_id,
        "model": agent.model,
        "coding_agent": agent.coding_agent,
        "effort": agent.effort,
    })
```

- [ ] **Step 5: Forward normalized events and metadata**

Replace the Pi tracker in `_event_forwarder()` with direct `HarnessEvent` persistence. Include both `effort` and compatibility key `thinking` in `agent_start`; include harness, exit code, normalized usage, and known context fields in `agent_end`.

Update the `agent_sessions` conflict clause so `coding_agent=excluded.coding_agent` is persisted when an agent changes harness. Keep the existing schema and store effort in event payloads, avoiding a database migration.

- [ ] **Step 6: Verify imports, validation, and legacy session matching**

```bash
uv run --with pydantic --with pyyaml --with rich python - <<'PY'
import sys
sys.path.insert(0, ".claude/skills/sssf/templates/adws")
from adw_modules import agents
from adw_modules.data_types import AgentConfig, PromptEngineering

prompt = PromptEngineering(system="system.md", user="user.md")
pi = AgentConfig(name="builder", model="provider/model", prompt_engineering=prompt)
assert agents.reusable_session({"session_id": "old", "model": "provider/model"}, pi) == "old"
codex = AgentConfig(name="builder", coding_agent="codex", model="gpt-5.6-terra",
                    effort="high", prompt_engineering=prompt)
assert agents.reusable_session({"session_id": "old", "model": "gpt-5.6-terra",
                                "coding_agent": "pi"}, codex) is None
PY
uv run python -m compileall -q .claude/skills/sssf/templates/adws/adw_modules
git diff --check
```

Expected: all assertions and compilation pass.

- [ ] **Step 7: Prepare the checkpoint, but do not commit without approval**

```bash
# Candidate only after explicit approval:
# git add .claude/skills/sssf/templates/adws/adw_modules/{harness_registry.py,agents.py,tracer.py,console.py}
# git commit -m "refactor: route workflow phases through backend registry"
```

### Task 6: Installer diagnostics, template configuration, and visualizer

**Files:**
- Modify: `.claude/skills/sssf/scripts/install.py`
- Modify: `.claude/skills/sssf/templates/sssf.config.yaml`
- Modify: `.claude/skills/sssf/templates/env.sample`
- Modify: `.claude/skills/sssf/apps/visualizer/shared/types.ts:160-174`
- Modify: `.claude/skills/sssf/apps/visualizer/src/components/PhaseDetail.vue:392-412`

**Interfaces:**
- Produces: `detect_cli(command: str) -> CliStatus` and plain-text installer diagnostics.
- Preserves: idempotent stamping and existing visualizer compatibility.

- [ ] **Step 1: Add non-authenticating CLI detection**

Define an explicit dataclass and run only `<binary> --version` with a 10-second timeout:

```python
@dataclass(frozen=True)
class CliStatus:
    name: str
    executable: str
    available: bool
    version: str = ""

CLI_COMMANDS = {
    "pi": os.environ.get("PI_PATH", "pi"),
    "claude_code": os.environ.get("CLAUDE_PATH", "claude"),
    "codex": os.environ.get("CODEX_PATH", "codex"),
}
```

Print one line per CLI after stamping. Missing CLIs are warnings and do not change the installer exit code.

- [ ] **Step 2: Update the stamped configuration**

Keep Pi as the active default. Use `effort: medium` in new templates, explain `thinking` as a legacy alias, list portable capabilities, and include commented Claude Code and Codex examples. Keep Pi `harness_engineering` examples explicitly Pi-only.

- [ ] **Step 3: Update environment guidance**

Add only executable overrides:

```dotenv
# PI_PATH=pi
# PI_MODELS_PATH=~/.pi/agent/models.json
# CLAUDE_PATH=claude
# CODEX_PATH=codex
```

State that Claude Code and Codex use their existing CLI authentication; do not add token variables for them.

- [ ] **Step 4: Render effort in the visualizer**

Add `effort?: string` to `AgentStartPayload`. Render `agentConfig.effort ?? agentConfig.thinking` under label `effort`, preserving old traces.

- [ ] **Step 5: Verify installer and UI build**

```bash
uv run python -m compileall -q .claude/skills/sssf/scripts/install.py
tmp_dir=$(mktemp -d)
(cd "$tmp_dir" && uv run /Users/sergioc/Documents/personal/SSSF-AI-SDCL/.claude/skills/sssf/scripts/install.py)
test -f "$tmp_dir/adws/adw_modules/agent_codex.py"
bun --cwd .claude/skills/sssf/apps/visualizer run build
git diff --check
```

Expected: installation exits 0, all adapter files are stamped, missing Pi is reported without failure, and the visualizer build passes.

- [ ] **Step 6: Prepare the checkpoint, but do not commit without approval**

```bash
# Candidate only after explicit approval:
# git add .claude/skills/sssf/scripts/install.py .claude/skills/sssf/templates/sssf.config.yaml .claude/skills/sssf/templates/env.sample .claude/skills/sssf/apps/visualizer
# git commit -m "feat: expose multi-backend configuration and diagnostics"
```

### Task 7: Documentation and migration guidance

**Files:**
- Modify: `README.md`
- Modify: `.claude/skills/sssf/SKILL.md`
- Modify: `.claude/skills/sssf/references/config.md`
- Modify: `.claude/skills/sssf/references/handoff.md`
- Modify: `.claude/skills/sssf/references/observability.md`
- Modify: `.claude/skills/sssf/cookbooks/sssf_overview.md`
- Modify: `.claude/skills/sssf/cookbooks/create_config.md`
- Modify: `.claude/skills/sssf/cookbooks/create_adw.md`
- Modify: `.claude/skills/sssf/cookbooks/update_adw.md`
- Modify: `.claude/skills/sssf/cookbooks/update_config.md`
- Modify: `.claude/skills/sssf/cookbooks/update_modules.md`
- Modify: `.claude/skills/sssf/cookbooks/install.md`
- Modify: `.claude/skills/sssf/cookbooks/run_adw.md`

**Interfaces:**
- Consumes: implemented flags, effort sets, capability profiles, session semantics, installer output, and smoke commands from Tasks 1-6.
- Produces: one consistent operator contract with no Pi-only claims outside Pi-specific sections.

- [ ] **Step 1: Replace obsolete Pi-only statements**

Run the inventory first:

```bash
rg -n "Pi-only|pi only|runs pi only|coding_agent: claude_code|same pi session|Pi interface|only coding agent" README.md .claude/skills/sssf --glob '*.md' --glob '*.yaml'
```

Update every hit to distinguish common behavior from Pi-specific behavior. Do not change historical facts about legacy session entries.

- [ ] **Step 2: Document the exact YAML and capability matrix**

Include the approved mixed example (`opus`, `gpt-5.6-terra`, `gpt-5.6-sol`), `effort`/`thinking` conflict behavior, native effort sets, portable aliases, Codex's two valid profiles, and Pi-only extensions.

- [ ] **Step 3: Document sessions, security, and diagnostics**

State that `{agent, harness, model}` controls resumption, effort changes retain the session, legacy entries imply Pi, and no fallback occurs. Explain native sandboxes versus after-the-fact Git protection, and state clearly that worktrees and integration remain a later milestone.

- [ ] **Step 4: Link only current official CLI references**

Use:

```text
https://code.claude.com/docs/en/cli-reference
https://code.claude.com/docs/en/headless
https://code.claude.com/docs/en/sandboxing
https://learn.chatgpt.com/docs/developer-commands?surface=cli
https://learn.chatgpt.com/docs/config-file/config-reference
```

- [ ] **Step 5: Verify documentation consistency**

```bash
rg -n "Pi-only|runs pi only|not implemented in v1|arrives in v2" README.md .claude/skills/sssf --glob '*.md' --glob '*.yaml'
rg -n "coding_agent: (pi|claude_code|codex)|effort:|thinking" README.md .claude/skills/sssf --glob '*.md' --glob '*.yaml'
git diff --check
```

Expected: the first command returns no obsolete claim; the second shows intentional configuration and migration references.

- [ ] **Step 6: Prepare the checkpoint, but do not commit without approval**

```bash
# Candidate only after explicit approval:
# git add README.md .claude/skills/sssf/SKILL.md .claude/skills/sssf/references .claude/skills/sssf/cookbooks
# git commit -m "docs: document multi-backend workflows"
```

### Task 8: Real read-only smoke tests and final evidence

**Files:**
- Verify only; do not add test files.
- Runtime-only target: a directory created with `mktemp -d`.

**Interfaces:**
- Consumes: stamped output from Tasks 1-7 and existing authenticated CLIs.
- Produces: two successful ADW IDs, two resumed session IDs, trace rows, and a clean source worktree.

- [ ] **Step 1: Create an isolated smoke target**

```bash
smoke_root=$(mktemp -d)
git -C "$smoke_root" init
(cd "$smoke_root" && uv run /Users/sergioc/Documents/personal/SSSF-AI-SDCL/.claude/skills/sssf/scripts/install.py)
```

Expected: the temporary repository and stamped files exist; no project file changed.

- [ ] **Step 2: Add runtime-only smoke prompts and roster**

Use `apply_patch` to create `$smoke_root/smoke_prompts/system.md`, `$smoke_root/smoke_prompts/user.md`, and `$smoke_root/smoke.config.yaml` with two read-only agents:

```yaml
defaults:
  effort: low
  protected_files: [adws/]
  data_dir: adws/adw_data

agents:
  - name: claude_smoke
    coding_agent: claude_code
    model: sonnet
    effort: low
    prompt_engineering:
      system: smoke_prompts/system.md
      user: smoke_prompts/user.md
    tools: [read, search, shell]
    writes: []

  - name: codex_smoke
    coding_agent: codex
    model: gpt-5.6-luna
    effort: low
    prompt_engineering:
      system: smoke_prompts/system.md
      user: smoke_prompts/user.md
    tools: [read, search, shell]
    writes: []

observability:
  db: adws/adw_data/sssf.db
  poll_ms: 500
```

Use these exact prompt contents:

```markdown
<!-- smoke_prompts/system.md -->
Do not modify files. Return a successful object matching the supplied schema.

<!-- smoke_prompts/user.md -->
# Smoke request

Prompt: {{prompt}}
Previous envelope: {{previous_envelope}}
Runtime directory: {{context_handoff_dir}}
```

After creating the fixture, stop and request explicit permission for the following disposable-repository commit. Once authorized, run:

```bash
git -C "$smoke_root" add .
git -C "$smoke_root" -c user.name="Smoke Check" -c user.email="smoke@example.invalid" \
  commit -m "chore: prepare smoke fixture"
test -z "$(git -C "$smoke_root" status --porcelain=v1 --untracked-files=all)"
```

The commit is required so Git-based write enforcement and the final cleanliness check can detect content changes. It is confined to the disposable repository.

- [ ] **Step 3: Run Claude Code twice under one ADW ID**

```bash
(cd "$smoke_root" && uv run adws/adw_prompt.py "Return a one-sentence success summary." --agent claude_smoke --config smoke.config.yaml --adw-id smokeclaude)
(cd "$smoke_root" && uv run adws/adw_prompt.py "Confirm that this resumes the earlier conversation." --agent claude_smoke --config smoke.config.yaml --adw-id smokeclaude)
```

Expected: both runs exit 0; `agent_map.json` retains the same Claude session UUID.

- [ ] **Step 4: Run Codex twice under one ADW ID**

```bash
(cd "$smoke_root" && uv run adws/adw_prompt.py "Return a one-sentence success summary." --agent codex_smoke --config smoke.config.yaml --adw-id smokecodex)
(cd "$smoke_root" && uv run adws/adw_prompt.py "Confirm that this resumes the earlier conversation." --agent codex_smoke --config smoke.config.yaml --adw-id smokecodex)
```

Expected: both runs exit 0; `agent_map.json` retains the same Codex thread UUID.

- [ ] **Step 5: Query tracing and prove the smoke target stayed unchanged**

```bash
sqlite3 "$smoke_root/adws/adw_data/sssf.db" \
  "select adw_id,agent,coding_agent,model,session_id from agent_sessions where adw_id in ('smokeclaude','smokecodex') order by adw_id;"
git -C "$smoke_root" status --short
git status --short
git diff --check
```

Expected: two rows name the correct harness/model/session; the smoke repository status is empty because runtime files are ignored; the project shows only planned source/document changes; diff check passes.

- [ ] **Step 6: Report evidence and stop before any project commit**

Report CLI versions, ADW IDs, session IDs, exit codes, trace query rows, unavailable usage fields, and `git status --short`. Do not stage or commit project files. Ask for explicit confirmation before any later Git checkpoint.

## Plan Completion Checklist

- [ ] Every spec requirement maps to a task above.
- [ ] No worktree, merge, PR, remote sandbox, credential, or fan-out work entered this plan.
- [ ] Pi remains the legacy default and uses the common interface.
- [ ] Claude Code and Codex both enforce native non-interactive sandbox policies.
- [ ] Session reuse checks agent, harness, and model; effort changes do not reset it.
- [ ] Unsupported capability profiles and native extensions fail before a phase.
- [ ] Tracing and the visualizer remain compatible with old `thinking` payloads.
- [ ] No automated test files or test-suite dependencies were added.
- [ ] Real smoke evidence exists for new sessions and resumed sessions on both new backends.
- [ ] No project commit, push, tag, merge, or rebase occurred without explicit confirmation.

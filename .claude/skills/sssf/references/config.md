# Config Reference

The full `sssf.config.yaml` contract: how defaults merge, how an agent selects a harness, and which portability limits are enforced before a process starts.

The default file is `adws/adw_sssf_config/sssf.config.yaml`. Pass `--config <path>` to an ADW, or set `SSSF_CONFIG` for a justfile recipe.

## Mixed-harness roster

`coding_agent` accepts `pi`, `claude_code`, and `codex`. Pi remains the starter default, but each agent may select a different installed harness.

```yaml
defaults:
  coding_agent: pi
  model: google/gemini-3.6-flash
  effort: medium                   # preferred spelling
  tools: [read, search, shell, write, edit]
  protected_files:
    - adws/adw_modules/
    - adws/adw_sssf_config/
    - adws/adw_*.py
  data_dir: adws/adw_data

agents:
  - name: planner
    coding_agent: claude_code
    model: opus
    effort: high
    purpose: Turn a request into a plan.
    prompt_engineering:
      system: adws/adw_data/prompt_engineering/planner/system.md
      user: adws/adw_data/prompt_engineering/planner/user.md

  - name: builder
    coding_agent: codex
    model: gpt-5.6-terra
    effort: high
    purpose: Implement the approved plan.
    prompt_engineering:
      system: adws/adw_data/prompt_engineering/builder/system.md
      user: adws/adw_data/prompt_engineering/builder/user.md

  - name: reviewer
    coding_agent: codex
    model: gpt-5.6-sol
    effort: xhigh
    writes: []
    tools: [read, search, shell]
    purpose: Confirm that the implementation matches the request.
    prompt_engineering:
      system: adws/adw_data/prompt_engineering/reviewer/system.md
      user: adws/adw_data/prompt_engineering/reviewer/user.md
```

Every model string belongs to its selected harness and is passed through unchanged. Pi owns provider-qualified identifiers such as `google/gemini-3.6-flash`; use `provider/model-id` there because its catalog can make a bare ID ambiguous. Claude Code and Codex own their native CLI identifiers, so use `opus`, `gpt-5.6-terra`, and `gpt-5.6-sol` without provider prefixes in mixed rosters.

## Fields and inheritance

An `agents[]` entry inherits `coding_agent`, `model`, `effort`/`thinking`, `color`, `tools`, `writes`, and `harness_engineering` from `defaults` when it omits the field. ADW scripts name agents, never models or harnesses.

| Field | Meaning |
|---|---|
| `coding_agent` | `pi`, `claude_code`, or `codex`. The selected adapter validates its executable, effort, and portable capability profile before a phase starts. |
| `model` | Model identifier passed to the selected harness. Pi resolves it through its installed catalog; write Pi values as `provider/model-id`. |
| `effort` | Preferred reasoning-effort field. |
| `thinking` | Legacy alias for `effort`. It is normalized to the same effective value. |
| `tools` | Portable capability allowlist; omitted in both defaults and agent means all portable capabilities. |
| `writes` | Repository write allowance: omitted is unrestricted except `protected_files`; `[]` is read-only; a list grants matching paths. |
| `harness_engineering` | Pi extension file paths only. Claude Code and Codex reject this field. |
| `protected_files` | Paths that an agent may not change unless its own `writes` entry allows them. |

When both `effort` and `thinking` are present, they must have exactly the same value. Different values fail configuration validation; either one alone is accepted, and an omitted pair resolves to `medium`.

## Effort support

The configuration does not translate an unsupported value. It fails before spawning the selected harness.

| Harness | Native accepted efforts |
|---|---|
| Pi | `off`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max` |
| Claude Code | `low`, `medium`, `high`, `xhigh`, `max`, `ultracode` |
| Codex | `minimal`, `low`, `medium`, `high`, `xhigh` |

For a roster meant to move between all three harnesses, use the intersection: `low`, `medium`, `high`, or `xhigh`.

## Portable capabilities

The portable names are `read`, `search`, `shell`, `write`, and `edit`. The shared resolver also accepts legacy aliases on every harness: `bash` maps to `shell`, while `grep`, `find`, and `ls` map to `search`. Codex validates the resulting normalized set, so aliases are valid there only when they resolve to its inspection or mutation profile.

| Harness | Accepted portable profiles |
|---|---|
| Pi | Any subset of the portable capabilities, plus named extension tools. |
| Claude Code | Any subset of the portable capabilities. |
| Codex | Inspection `[read, search, shell]` with `writes: []`, or mutation `[read, search, shell, write, edit]` with writes omitted or non-empty. |

`writes` is independent from `tools` for Pi and Claude Code: a `shell` capability is not a repository boundary, and `writes` is orchestration policy rather than a standalone filesystem sandbox. Codex couples the two supported profiles so the native sandbox always imposes the declared inspection or mutation capability set. The Git monitor compares exact states for tracked and non-ignored untracked ordinary paths after the call. It can restore denied paths, including pre-existing dirty or untracked content, and then fails the phase; allowed ordinary-path changes remain. Existing user changes are left alone unless the call changes them.

## Sandboxing and write protection

Claude Code and Codex receive native sandbox controls, which are the security boundary for those harnesses. Claude Code maps `writes: []` to read-only and other values to workspace-write. Codex admits only the coherent inspection/read-only or mutation/workspace-write pairs in the capability table, then uses the matching native sandbox mode. Pi has no native filesystem sandbox, so run untrusted Pi workloads only inside external isolation.

For Pi, omitting `tools` leaves its native default and extension tools available; an explicit `tools: []` sends Pi's `--no-tools` flag and disables every built-in, extension, and custom tool. A non-empty list is translated to Pi's `--tools` allowlist.

The Git monitor runs on every completed or failed execute path, including process, protocol, parsing, and gate failures. It requires a valid worktree/HEAD and detects HEAD, current-ref, index, and ref-map changes, but never auto-restores Git metadata: operators must inspect those changes. It is not protection against adversarial `.git`, ignored paths, special nodes, symlink/TOCTOU attacks, or deliberately detached processes. Worktree/disposable-checkout isolation is the deferred next milestone. A relative `data_dir` is resolved from the Git repository root even when an ADW starts in a nested directory. An absolute `data_dir` outside that repository is supported and lies outside the Git safeguard scope; only an in-repository frozen current-session directory is exempt from monitoring. Native read-only Claude Code and Codex profiles cannot create repository-local handoff artifacts, so use their structured output or select a writable profile when direct artifact files are required.

## Pi extensions

`harness_engineering` is Pi-specific. Each item is an extension file path, passed as a Pi extension. If an extension registers a tool and `tools` is configured, list that tool name explicitly or Pi filters it out. Do not set `harness_engineering` for Claude Code or Codex; their adapters reject extension files rather than silently ignoring them.

## Sessions and resumption

`agent_map.json` records `{session_id, coding_agent, model, effort}` for each agent. A saved session resumes only when the configured **agent name, harness, and model** match its entry. Entries written before `coding_agent` existed are treated as Pi entries for compatibility.

Changing `effort` retains a matching session. Changing the harness or model starts a fresh session. There is no automatic fallback to another harness or model when validation, authentication, executable discovery, or a native process fails.

See [Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference), [Claude Code headless mode](https://code.claude.com/docs/en/headless), [Claude Code sandboxing](https://code.claude.com/docs/en/sandboxing), [Codex developer commands](https://learn.chatgpt.com/docs/developer-commands?surface=cli), and [Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference) for the native CLI surfaces used by the adapters.

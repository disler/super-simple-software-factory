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
    model: anthropic/claude-opus-5
    effort: high
    purpose: Turn a request into a plan.
    prompt_engineering:
      system: adws/adw_data/prompt_engineering/planner/system.md
      user: adws/adw_data/prompt_engineering/planner/user.md

  - name: builder
    coding_agent: codex
    model: openai/gpt-5.6-terra
    effort: high
    purpose: Implement the approved plan.
    prompt_engineering:
      system: adws/adw_data/prompt_engineering/builder/system.md
      user: adws/adw_data/prompt_engineering/builder/user.md

  - name: reviewer
    coding_agent: codex
    model: openai/gpt-5.6-sol
    effort: xhigh
    writes: []
    tools: [read, search, shell]
    purpose: Confirm that the implementation matches the request.
    prompt_engineering:
      system: adws/adw_data/prompt_engineering/reviewer/system.md
      user: adws/adw_data/prompt_engineering/reviewer/user.md
```

Every model string is passed to the selected harness. For Pi, use an explicit `provider/model-id`, because its catalog can make a bare model id ambiguous. The other harnesses receive the configured string through their native CLI model option.

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

The portable names are `read`, `search`, `shell`, `write`, and `edit`. Pi additionally accepts legacy aliases: `bash` maps to `shell`, while `grep`, `find`, and `ls` map to `search`.

| Harness | Accepted portable profiles |
|---|---|
| Pi | Any subset of the portable capabilities, plus named extension tools. |
| Claude Code | Any subset of the portable capabilities. |
| Codex | Exactly one of two profiles: inspection `[read, search, shell]`, or mutation `[read, search, shell, write, edit]`. |

`writes` is independent from `tools`: a `shell` capability is not a repository boundary. `writes` and `protected_files` are enforced by comparing the Git change set after the agent call; unauthorized changes introduced by the call are rolled back and the phase fails. Existing user changes are left alone.

## Sandboxing and write protection

Claude Code and Codex receive native sandbox controls. `writes: []` becomes a read-only native sandbox; otherwise they run with workspace-write access. Claude Code is configured to fail if its native sandbox is unavailable. Codex uses its native `read-only` or `workspace-write` sandbox mode.

The Git change-set check runs for every harness, including Pi. It is after-the-fact protection of repository files, not a replacement for a native sandbox. All agents may still write their session runtime (`context_handoff/`, prompts, streams, and envelopes) under `data_dir`.

## Pi extensions

`harness_engineering` is Pi-specific. Each item is an extension file path, passed as a Pi extension. If an extension registers a tool and `tools` is configured, list that tool name explicitly or Pi filters it out. Do not set `harness_engineering` for Claude Code or Codex; their adapters reject extension files rather than silently ignoring them.

## Sessions and resumption

`agent_map.json` records `{session_id, coding_agent, model, effort}` for each agent. A saved session resumes only when the configured **agent name, harness, and model** match its entry. Entries written before `coding_agent` existed are treated as Pi entries for compatibility.

Changing `effort` retains a matching session. Changing the harness or model starts a fresh session. There is no automatic fallback to another harness or model when validation, authentication, executable discovery, or a native process fails.

See [Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference), [Claude Code headless mode](https://code.claude.com/docs/en/headless), [Claude Code sandboxing](https://code.claude.com/docs/en/sandboxing), [Codex developer commands](https://learn.chatgpt.com/docs/developer-commands?surface=cli), and [Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference) for the native CLI surfaces used by the adapters.

# Install

`sssf install` — stamp the entire factory out of the skill and into the current working directory. Invoke it as `/sssf install` in Claude Code or `$sssf install` in Codex.

## Run it

```bash
uv run <skill-dir>/scripts/install.py
```

Run from the **target repo root** — the cwd is where everything lands. The repository-local skill directory is `.claude/skills/sssf` for Claude Code, `.agents/skills/sssf` for Codex, and `.kiro/skills/sssf` for Kiro CLI. The installer preserves the integration from which it is invoked.

## What gets stamped

`install.py` copies `templates/` into the cwd:

| Stamped | From | Tracked? |
|---|---|---|
| `adws/adw_sssf_config/sssf.config.yaml` | `templates/sssf.config.yaml` | yes — the agent roster |
| `.env.sample` | `templates/env.sample` | yes |
| `adws/adw_*.py` | `templates/adws/` | yes — the twelve starter ADWs |
| `adws/adw_modules/` | `templates/adws/adw_modules/` | yes — all low-level logic |
| `adws/adw_data/prompt_engineering/{planner,builder,scout,reviewer,documenter}/` | `templates/prompt_engineering/` | yes — **the user-owned home for prompts** |
| `adws/adw_data/harness_engineering/` | `templates/harness_engineering/` | yes — **the user-owned home for pi extensions** |
| `justfile` | `templates/justfile` | yes — starter recipes: `just demo`, the workflows, the trace reads, `just obs` |
| `adws/adw_data/sessions/`, `adws/adw_data/sssf.db` | created at runtime | no — gitignored |

The two `*_engineering` dirs mirror the two config keys of the same name: `prompt_engineering` is what an agent is told, `harness_engineering` is what its harness can do. Both are yours the moment they are stamped. Edit them in `adws/adw_data/`, never back inside the skill.

`harness_engineering/` ships with `subagents.ts` — the pi extension backing `subagent_create` / `_continue` / `_list` / `_remove`, wired to the planner and scout in the starter roster.

## Idempotency

Re-running is safe. `install.py` preserves existing files — your config, prompts, and previously stamped code alike — while applying narrowly defined migrations for obsolete generated paths. It reports what it skipped, so a second run doubles as a drift check. To refresh stamped code (`adw_modules/`, the starter `adw_*.py`) to the skill's current version, run with `--force` — but know that `--force` overwrites ALL existing stamped files, including `sssf.config.yaml` and `prompt_engineering/`, so commit or back up user-owned edits first.

## Post-install checklist

1. **CLIs** — the starter roster needs authenticated `claude` and `codex` commands on PATH. Set `CLAUDE_PATH` or `CODEX_PATH` in `.env` only when the executable is elsewhere.
2. **Models** — confirm Claude Code accepts `sonnet` and Codex accepts `gpt-5.6-terra` for the logged-in accounts.
3. **Env** — `.env` is optional for the starter roster. Copy `.env.sample` only when adding path overrides or Pi provider keys.
4. **Gitignore** — `install.py` appends `adws/adw_data/sessions/`, `adws/adw_data/sssf.db*`, `.env`, `__pycache__/`, `*.pyc`, and `node_modules/` for you; confirm they landed. They are runtime, bytecode, dependencies, or secrets, and must never be committed. `node_modules/` is not cosmetic: `permissions.snapshot()` lists untracked files, so an unignored dependency tree that appears mid-run is attributed to the agent and rolled back file by file. The stamped visualizer carries its own `.gitignore` for the same reason.
5. **Git repo** — ADWs that end in a commit phase call `git_helper.commit_all`, which raises if the cwd is not a git repository. Run `git init` and make a first commit before using `adw_plan_build.py`, `adw_plan_build_test.py`, or `adw_simple_sdlc.py`. `adw_document.py` needs one too: it measures the change with `git diff` against a base ref (`main` by default, `--base` to override).
6. **Smoke test** — `just demo` runs two cheap read-only workflows back to back, or run the smallest ADW directly:

```bash
just demo                                                    # both, end to end
uv run adws/adw_prompt.py "reply with a one-line summary of this repo"   # the raw form
```

Green means the whole path works: config validated, session minted, Pi ran, envelope parsed, events landed in `adws/adw_data/sssf.db`. Verify the trace exists before trusting anything larger:

```bash
sqlite3 adws/adw_data/sssf.db "select adw_id, status from sessions order by started_at desc limit 1;"
```

If the smoke test fails, fix it before composing chains — every multi-agent ADW rides on this exact path.

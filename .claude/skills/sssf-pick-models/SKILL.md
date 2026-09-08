---
name: sssf-pick-models
description: Assign a coding agent (harness) and model to every agent in an SSSF roster, then write the result into sssf.config.yaml. Use after installing SSSF, or when deciding or changing which harness and model runs planner, builder, scout, reviewer, or documenter. Unlike the sssf skill's update_config cookbook, which documents the schema, this one carries the model catalog for all five harnesses and drives the choice. Does not run ADWs and does not edit prompts.
---

# SSSF Pick Models

Fill in `coding_agent` and `model` for every agent in `adws/adw_sssf_config/sssf.config.yaml`.
Show the developer what is available, ask, then write the entries — including the
per-harness keys that must change alongside a harness switch, which is where a
hand edit usually fails validation.

## Boundaries

- Edit **only** `adws/adw_sssf_config/sssf.config.yaml`.
- Never change an agent's `writes`, `purpose`, or `prompt_engineering`. `writes` is
  the repo boundary and has nothing to do with which harness runs the agent.
- Do not run an ADW. Validation here is `agents.validate()`, not a live run.

State-check first, and branch instead of assuming:

```bash
ls adws/adw_sssf_config/sssf.config.yaml     # no such file → SSSF is not installed
for cli in pi claude codex kiro-cli agy; do command -v $cli >/dev/null && echo "$cli present"; done
```

If the config is missing, say SSSF is not installed here and stop. Do not offer a
harness whose CLI is absent without saying so — a roster is only as real as the
binaries behind it, and each adapter shells out to a CLI that must already be
logged in.

## Step 1 — read the roster, list what needs deciding

Read the config and list the agents it actually contains, with the current
`coding_agent`/`model` each one resolves to (an agent with no key of its own
inherits `defaults`). Never assume the starter five — the roster may be edited.

Present it as a table: agent, purpose, what it currently runs on. Then ask which
ones to change, and offer "all of them" as the default answer.

Say what each choice is actually buying, in one line per agent:

| Agent | What the choice affects |
|---|---|
| `planner` | Its errors propagate into every later phase. The most expensive place to be cheap. |
| `builder` | Highest token volume in a run, and gates catch its mistakes. The best place to be cheap and fast. |
| `scout` | Reads a lot, writes one report. Wants a big context window more than raw reasoning. |
| `reviewer` | Independent judgement. Worth a **different model family** than the builder — a model reviewing its own output is not a second opinion. |
| `documenter` | Reads a diff, writes prose. Cheap is fine. |

## Step 2 — show the options

**Query the live catalogs first.** Two harnesses can be listed headless, and their
output is authoritative — where it disagrees with the tables below, the CLI wins.
The tables are a snapshot kept for orientation and for when the CLI is missing,
unauthenticated, or offline.

```bash
kiro-cli chat --list-models --format json    # ids, rate_multiplier, context window
agy models                                   # slugs, per account — entitlement, not a fixed roster
```

**`kiro_cli`** — the only harness that publishes price. `rate` is a multiplier on
what a request costs in credits, so it is the whole cost lever: the cheapest model
is roughly 48× cheaper than the most expensive one in the same CLI.

| Model | rate | context |
|---|---|---|
| `qwen3-coder-next` | 0.05 | 256k |
| `gpt-5.6-luna` | 0.1 | 272k |
| `minimax-m2.1` | 0.15 | 196k |
| `deepseek-3.2`, `minimax-m2.5` | 0.25 | 164k / 196k |
| `claude-haiku-4.5` | 0.4 | 200k |
| `glm-5` | 0.5 | 200k |
| `auto`, `gpt-5.6-terra` | 1.0 | 1M / 272k |
| `claude-sonnet-5`, `claude-sonnet-4.6` | 1.3 | 1M |
| `claude-sonnet-4.5`, `claude-sonnet-4` | 1.3 | 200k |
| `claude-opus-5`, `claude-opus-4.8`, `claude-opus-4.7`, `claude-opus-4.6` | 2.2 | 1M |
| `claude-opus-4.5` | 2.2 | 200k |
| `gpt-5.6-sol` | 2.4 | 272k |

**`antigravity`** — the effort tier is part of the slug. Publishes no price and no
context window.

| Family | Slugs |
|---|---|
| Gemini Flash 3.8 / 3.7 / 3.6 / 3.5 | `gemini-3.<v>-flash-low` \| `-medium` \| `-high` |
| Gemini Pro 3.1 | `gemini-3.1-pro-low` \| `gemini-3.1-pro-high` |

**`claude_code`** — aliases, no headless catalog: `sonnet`, `opus`, `haiku`.

**`codex`** — no headless catalog either (`codex models` needs a terminal). The
starter roster uses `gpt-5.6-terra`. Ask the developer which ids their account has
rather than guessing one.

**`pi`** — always qualify as `provider/model-id`. A bare id that matches several
providers fails at resolution.

## Step 3 — write it

One `coding_agent` + `model` pair per agent, plus the keys that harness forces.
**The forced keys are the whole reason to use this skill**: `defaults.tools` is a
list in the starter roster and every agent inherits it, so switching an agent to
codex, kiro_cli, or antigravity without also setting `tools: null` fails
`agents.validate()` before anything spawns.

```yaml
  - name: builder
    coding_agent: kiro_cli
    model: claude-haiku-4.5
    thinking: high            # low | medium | high | xhigh | max
    tools: null               # REQUIRED — Kiro's --trust-tools takes its own ids

  - name: reviewer
    coding_agent: antigravity
    model: gemini-3.8-flash-high
    thinking: high            # MUST equal the tier baked into the slug
    tools: null               # REQUIRED — headless agy has no allowlist flag

  - name: planner
    coding_agent: claude_code
    model: sonnet
    thinking: high            # low | medium | high | xhigh | max
    # tools: keep the inherited list — Claude Code maps it
```

Rules per harness, all enforced by `agents.validate()`:

| Harness | `tools` | `thinking` |
|---|---|---|
| `pi` | list, or `null` for all | its own levels |
| `claude_code` | list, mapped by the adapter | `low` … `max` |
| `codex` | **`null`** | its own levels |
| `kiro_cli` | **`null`** | `low` … `max`, never `off`/`minimal` |
| `antigravity` | **`null`** | `low`/`medium`/`high`, and must match the slug's tier |

When the **whole** roster moves to codex/kiro_cli/antigravity, set
`defaults.tools: null` once instead of repeating it on every agent.

**`tools: null` must REPLACE the agent's existing list, not join it.** In the
starter roster, planner, builder, scout, and documenter each carry their own
`tools:` list. Adding a second `tools:` key to the same entry is silently
ambiguous and parser-dependent; with the loader used here, the last duplicate
key wins. Do not rely on ordering to override the old value. Delete the list
and its items, then write `tools: null` in its place.

Two consequences worth stating out loud before saving:

- **A model change starts a fresh session.** `agent_map.json` records the model a
  session was created with, so a joined run (`--adw-id`) will not resume an agent
  whose model changed. It loses its accumulated context window once.
- **A `kiro_cli` agent reports 0 tokens and $0.0000.** It bills credits, which show
  up in `usage.credits` and `sessions.total_credits`. Nothing is broken; a mixed
  roster's `total_tokens` just undercounts by design.

## Step 4 — verify, fix, re-verify

Confirm the edit is valid rather than claiming it is. The `--with` flags are not
optional: the stamped modules import pydantic and pyyaml, and every ADW declares
the same four dependencies in its PEP 723 header.

```bash
uv run --with pydantic --with pyyaml --with python-dotenv --with rich python -c "
import sys; sys.path.insert(0, 'adws')
from adw_modules import agents
cfg = agents.load_config('adws/adw_sssf_config/sssf.config.yaml')
agents.validate(cfg, [a.name for a in cfg.agents])
print('roster ok:', ', '.join(f'{a.name}={a.coding_agent}/{a.model}' for a in cfg.agents))
"
```

On failure it prints `config validation failed:` with one line per problem **to
stderr** and exits non-zero, so read stderr, not stdout. Fix the config and run it
again. Do not hand back a roster that has not printed `roster ok`.

**Which typos this actually catches**: exactly the two harnesses that can be listed.
`kiro_cli` and `antigravity` are checked against their own catalog and a bad id
fails here, with the available ids in the message. `codex` and `claude_code` have no
headless catalog, so a misspelled model for them **passes validation and fails at
runtime instead** — confirm those two ids with the developer rather than relying on
this step. An empty catalog is treated as unknown rather than as a bad name, so a
logged-out CLI silently weakens the check; log in before trusting a pass.

Then report the final assignment as a table and stop. Deeper field-by-field spec:
`references/config.md` in the `sssf` skill.

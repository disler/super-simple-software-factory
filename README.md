# Super Simple Software Factory

> **Repeatable agents-plus-code workflows, packaged as one skill, stamped into any repo.**
> Deterministic Python owns the graph. Coding agents are bounded nodes inside it.

📺 Full breakdown on YouTube: **[Super Simple Software Factory](https://youtu.be/haUfb1ievTE)**

<p align="center">
  <img src="images/00_swimlane_waterfall.svg" alt="A run as swim lanes: engineer, code, planner, builder, and reviewer phases laid on a time axis, each block labelled with its duration, one phase still running and the next still queued" width="850">
</p>

<p align="center">
  <img src="images/01_factory_spine.svg" alt="A run spine: engineer, agent, and code phases on a deterministic rail, every event dropping into a SQLite trace db that the UI polls" width="850">
</p>

A software factory does one thing: it gives you more leverage on your prompt. How much leverage depends entirely on what you invest in it. At the low end you chain two agents together and hope. At the high end you build a system of agents plus code that runs without you, and does the job about as well as you would.

Everyone can get an agent to write code once. Almost nobody gets the same result twice. This fixes that by moving the control plane out of the prompt and into Python. An ADW script (AI Developer Workflow) owns sequencing, retries, and acceptance. Agents work inside named phases. Typed JSON envelopes carry context across the seams. Every event streams into SQLite while it is still happening. **Agent proposes, code disposes.**

> [!NOTE]
> **This branch is the skill alone**, which is the thing you install. For a repo with the factory already stamped into it, a demo app it planned, built, tested, reviewed, and documented, and the real traces from those runs, see the **[`example` branch](../../tree/example)**.

---

## Why this exists

<p align="center">
  <img src="images/02_control_plane.svg" alt="Left: one big agent owning its own loop with no phase boundary and no acceptance. Right: code owning the loop with agents as bounded, gated nodes" width="780">
</p>

Hand a capable model your whole SDLC and you get a machine with no seams. There is no phase boundary, so you cannot say which step failed. There is no acceptance criterion you can name, so "done" means "the agent stopped talking." A retry is a cold start that throws away everything the agent just learned. The only trace is a transcript you have to read like a novel. Run it twice, get two different systems.

The fix is not a better prompt. The fix is deciding, deliberately, that **code owns sequencing, retries, and acceptance, and the agent owns only the work inside one bounded phase**. Everything else falls out of that one line. Phases become the unit of the trace. Envelopes become the only way context crosses a seam. Gates become the definition of done. A correction becomes cheaper than a restart, because the session is still alive.

### Agents are great. You do not always need one.

This is the part most engineers are going to skip, and pay for later.

Code costs nothing. It runs at the speed of light. You can change it in a second. And you actually own it, which is not true of any model you are renting by the token.

So when the invocation is already known, write it down. `bun test` is not a judgement call. Neither is `ruff check`. An agent rediscovering your test runner burns a context window to learn what a subprocess already knows, and it charges you for the privilege every single run. Worse, it puts a passing test suite into a context window, which buys you nothing at all.

Agents are for the parts that need reading and deciding. Everything else is a `kind="code"` phase. When code fails, the failure comes back to the builder as an envelope, through the same door an agent's report would have used. The repair loop is identical. You just stopped paying an agent to do arithmetic.

The bill for skipping this is not only tokens. It is cost, speed, and consistency, and you pay it on run one hundred and run one thousand, not on run one.

> *Same models. Same prompts. The difference is who owns the loop.*

---

## Install

Run the repository installer from the **target repo root** and select the host that should operate SSSF. This integration choice does not change the `coding_agent` configured for any workflow agent.

**Prereqs:** [`uv`](https://docs.astral.sh/uv/), `sqlite3`, and the CLI used by each agent in your roster (`pi`, `claude`, `codex`, `kiro-cli`, or `agy`), already authenticated. [`bun`](https://bun.sh) is only needed for the visualizer.

```bash
# from the target repo root
uv run /path/to/super-simple-software-factory/install.py --integration codex
# or: --integration claude
# or: --integration kiro
# or: --integration none

claude --version                                 # starter agents
codex --version                                  # starter reviewer
git init && git add -A && git commit -m "stamp sssf"   # commit the factory BEFORE the first chain

# smoke test: two cheap read-only runs, end to end
just demo
just sessions              # what just happened
just obs                   # the trace UI, needs bun

# no just? every recipe is one line. the raw form of `just demo` is:
uv run adws/adw_prompt.py "reply with a one-line summary of this repo" --agent scout
```

`--integration codex` installs the repo-local skills at `.agents/skills/`, where Codex exposes them as `$sssf`, `$sssf-grill-me`, and `$sssf-pick-models`. `--integration claude` installs them at `.claude/skills/`, where Claude Code exposes them as `/sssf`, `/sssf-grill-me`, and `/sssf-pick-models`. `--integration kiro` installs them at `.kiro/skills/`, where Kiro CLI exposes the same three — its default agent already carries `skill://.kiro/skills/*/SKILL.md`, so nothing needs configuring. `none` installs only the factory.

Use `sssf-grill-me` when the initial request still needs product decisions. It inspects the relevant code, interviews the developer, and writes an approved `specs/YYYY-MM-DD-<slug>.md` without changing the application. Its instructions use only capabilities shared by coding harnesses, so the output contract is the same in Claude Code, Codex, and Kiro CLI.

Use `sssf-pick-models` right after installing, to decide which harness and model runs each agent. It carries the model tables for all five harnesses, shows what each role is actually buying, and writes the result into `sssf.config.yaml` with the keys a harness switch forces — the `tools: null` that codex, kiro_cli, and antigravity all require, which is what makes a hand edit fail `agents.validate()`.

The full workflow accepts either an inline prompt or a specification path:

```bash
just sssf "add a /health endpoint"
just sssf specs/YYYY-MM-DD-<slug>.md
```

Re-running `install.py` is safe. It preserves existing files, applies only narrowly defined migrations for obsolete generated paths, and reports what it skipped. `--force` refreshes stamped code and the selected integration, but it overwrites **all** stamped files including your `sssf.config.yaml` and your prompts, so commit first.

Green on the smoke test means the whole path works: config validated, a harness ran, the envelope parsed, and events landed in `adws/adw_data/sssf.db`. Fix it there before composing anything larger, because every multi-agent chain rides this exact path.

### Credentials

The starter roster uses Claude Code Sonnet for planner, builder, scout, and documenter, plus Codex GPT-5.6 Terra for review. Both CLIs must already be logged in; SSSF does not read their credentials.

API keys in `.env` are only needed when you configure a Pi agent backed by an API provider.


---

## Three principles

Everything here is built to be **observable**, **customizable**, and **reusable**. Those are not adjectives, they are the reason the parts are shaped the way they are.

**Observable.** If you cannot measure your agents, you cannot improve them. Every event goes into SQLite as it happens, so you can watch a run mid-flight, not read about it afterwards.

**Customizable.** One YAML file sets the core four for every agent: context, model, prompt, tools. Different models at different price and speed points, in the same run. It is not about which model is best anymore, it is about which model is right for that one phase.

**Reusable.** The whole thing is a skill you stamp into any repo, then bend to fit. The tests it ships are not your tests. The prompts it ships are starters. It is designed to be edited.

There are three actors here, and the design keeps them separate on purpose: **the engineer**, **the code**, and **the agents**. The trick is not running more agents. The trick is using all three at the right moment.

---

## The skill is the product

<p align="center">
  <img src="images/03_skill_stamp.svg" alt="The sssf skill directory on the left stamping config, adws, and prompt_engineering into three different target repos" width="780">
</p>

The same skill content is installed at `.claude/skills/sssf/` for Claude Code, `.agents/skills/sssf/` for Codex, or `.kiro/skills/sssf/` for Kiro CLI. Two harness-neutral companions are installed beside it: `sssf-grill-me`, which turns ambiguous requests into approved specifications before a workflow starts, and `sssf-pick-models`, which assigns a harness and model to each agent in the roster. SSSF's `SKILL.md` carries the hard rules and routes each request to one of nine cookbooks. `references/` holds the deep specs, `scripts/` holds the generators, and `templates/` holds exactly what gets stamped.

| What lands in your repo | Where it comes from | Tracked |
|---|---|---|
| `adws/adw_sssf_config/sssf.config.yaml` | `templates/sssf.config.yaml` | yes, it is your agent roster |
| `adws/adw_*.py` | `templates/adws/` | yes, twelve starter workflows |
| `adws/adw_modules/` | `templates/adws/adw_modules/` | yes, all low-level logic |
| `adws/adw_data/prompt_engineering/` | `templates/prompt_engineering/` | yes, **your prompts live here** |
| `adws/adw_data/harness_engineering/` | `templates/harness_engineering/` | yes, pi extensions |
| `.env.sample` | `templates/env.sample` | yes |
| `justfile` | `templates/justfile` | yes, starter recipes to run and watch |
| `adws/adw_data/sessions/`, `sssf.db` | created at runtime | no, gitignored |

The prompts are yours the moment they land. Edit them in `adws/adw_data/prompt_engineering/{agent}/`, never back inside the skill.

There is no DSL here. No framework to learn. It is Python, YAML, agents, and a skill, which is exactly what these models are already trained on. Staying in distribution is a feature.

---

## The agent roster

`adws/adw_sssf_config/sssf.config.yaml` answers one question per entry: who is this agent. One agent, one prompt, one purpose.

```yaml
defaults:
  coding_agent: claude_code        # pi | claude_code | codex | kiro_cli | antigravity
  model: sonnet
  thinking: medium                 # off | minimal | low | medium | high | xhigh | max
  protected_files:                 # no agent may edit the machinery that grades it
    - adws/adw_modules/
    - adws/adw_sssf_config/
    - adws/adw_*.py
  data_dir: adws/adw_data

agents:
  - name: planner
    thinking: high                 # per-agent overrides win over defaults
    color: "#a78bfa"               # this agent's lane swatch in the trace
    purpose: Turn a request into a plan the builder can implement without asking questions.
    prompt_engineering:
      system: adws/adw_data/prompt_engineering/planner/system.md
      user: adws/adw_data/prompt_engineering/planner/user.md
    writes:                        # the plan is all it may leave in the repo
      - specs/

  - name: reviewer
    coding_agent: codex
    model: gpt-5.6-terra
    thinking: high
    tools: null
    writes: []
```

Five starter agents ship in the box: `planner`, `builder`, `scout` (read-only recon), `reviewer`, and `documenter`. There is no tester, because running a suite is a known command and therefore code.

Every agent gets its own model, thinking level, prompts, tools, and harness. That is the core four, and it is the whole surface you tune. Give the planner a frontier model and the builder a cheap fast one. Give the scout subagents. Give the reviewer no ability to write code at all.

**`tools` is a capability list. `writes` is the boundary.** They are not the same thing, and the difference matters: `bash` runs anything, including `git checkout`, and `write` reaches any path. So "this agent changes nothing" is enforced in code, after every call, by comparing the repo before and after. Unauthorized changes are rolled back and the phase fails. A read-only agent is read-only with respect to your repo, never unable to write its own report.

Config defines who an agent **is**. The ADW call site defines how it is **used**. That split is what lets one agent serve many different calls. **ADW scripts never name a model, they name an agent.**

---

## Phases: three lanes, one primitive

<p align="center">
  <img src="images/04_phase_lanes.svg" alt="Swim lanes for engineer, git, planner, builder, and reviewer with phase blocks placed on a time axis and one dashed queued block" width="780">
</p>

Every run is a sequence of phases, and every phase is the same context manager no matter who owns it.

```python
REQUIRED_AGENTS = ["planner", "builder", "reviewer"]   # names, never models

cfg = agents.load_config(config)
agents.validate(cfg, REQUIRED_AGENTS)   # a missing agent fails before anything spawns
run = session.ensure(cfg, adw_id)       # pin-or-create the session

with run.phase(PhaseParams(name="plan", kind="agent", owner="planner",
                           description="Turn the request into an implementable plan")) as ph:
    plan = ph.call(AgentCall(output_type=PlanOutput, prompt=prompt,
                             gates=[gates.artifacts_exist, gates.files_non_empty]))

with run.phase(PhaseParams(name="commit", kind="code", owner="git",
                           description="Commit the working tree")) as ph:
    message = build.commit_message or f"sssf({run.adw_id}): {build.summary}"
    ph.log(sha=git_helper.commit_all(message), message=message)

return run.finish(accepted=review.approved, reason="the reviewer never approved")
```

Three kinds, three swim lanes. **engineer** is the human lane. **agent** is `ph.call(...)`: prompt in, typed envelope out, gates verified. **code** is a deterministic step that stands on its own, like a commit or a migration, and it is never buried inside an agent phase, so the trace shows exactly when code ran and when an agent was working.

That commit phase is the whole pattern in miniature. The builder proposes the message as a field on its envelope. Code decides whether to use it, falls back when it is empty, and performs the write. The agent never runs `git commit` itself.

**Success must be earned.** Every phase defaults to `fail`. A clean exit flips it, and an agent phase also needs its envelope to parse and every gate to come back green. `run.finish(accepted=...)` adds the second question, because phases passing is not the same as the run being acceptable: a test phase that ran a red suite did its job perfectly. One call settles the exit code, the session status, and the banner together, so they cannot disagree.

---

## Envelopes and gates

<p align="center">
  <img src="images/05_envelope_gates.svg" alt="An agent's final JSON parsed against its output type, checked by gates, with violations looping back into the same session as a correction" width="780">
</p>

An agent has exactly two output channels: reference files written into `context_handoff/`, and a final valid-JSON response parsed against the output type the call declared. Code persists that response as `envelope.json`, records it, and injects it into the next agent's prompt. Context transfers in code, not in conversation.

```python
class EnvelopeBase(BaseModel):
    status: Literal["success", "fail"]
    summary: str = ""
    artifacts: list[str] = Field(default_factory=list)
    notes_for_next_agent: str = ""

class BuildOutput(EnvelopeBase):
    changed_files: list[str] = Field(default_factory=list)
    commit_message: str = ""        # consumed by the git commit phase
```

Determinism is wired into every step. Agents must return a specific structure, every time. If it does not parse, they get asked again until it does.

Gates verify claims, never predictions. Nobody knows which files an agent will touch before it finishes, so gates run **after** the fact against the envelope's own declarations: `artifacts_exist`, `files_non_empty`, `json_parses`, `diff_matches_claims`, `tests_pass(...)`. A gate is a callable with the signature `gate(envelope, run) -> GateReport`, one `check(item, ok, note)` per thing it examined, so a green gate tells you *what* it verified.

When JSON does not parse or a gate returns violations, **nothing restarts**. The harness re-prompts the same session with a correction naming exactly what was wrong, and the context window stays intact. Pi treats `--session-id` as create-or-continue, so running an agent and continuing it are the same call. A cold restart throws away everything the agent learned. A correction costs one message.

The output contract lives in three places and they are one thing: the type in `data_types.py`, the JSON example in that agent's `user.md` `## Report` section, and `output_type=` at the call site. **Change one, change all three in the same edit.**

---

## The trace

<p align="center">
  <img src="images/06_trace_path.svg" alt="Running agents to tracer.py to a WAL SQLite db with seven tables, read by a cursor poll query, with no websocket and no ingest endpoint" width="780">
</p>

One data path, no exceptions: **agents write to SQLite, readers poll SQLite.** `agent_pi.py` tails the coding agent's JSONL stdout line by line and the tracer inserts each event while the agent is still working, so tool calls are visible mid-run instead of batched at the end.

Ten event types land across seven tables: `sessions`, `phases`, `events`, `envelopes`, `gate_results`, `agent_sessions`, and `processes` (adw_id to pid, so a stuck run can be found and stopped). Every event logs against both its `adw_id` and its `phase_id`, and `parent_id` nests spans, so an agent phase expands into its own tool calls.

Pi announces a tool call across three raw events, so the interface folds them into exactly **one** `tool_call` row per real call. Each row is named the way you would read it aloud (`bash: ls -la src`) and carries `{tool, tool_call_id, args, result_snippet, ok, duration_ms, agent}`.

```sql
select * from events where adw_id = ? and rowid > ? order by rowid limit 500;
```

That one cursor query is the entire transport. Live view and full history are the same query at different cadence, which is why there is no ingest endpoint, no WebSocket, no backfill, and no separate replay path. Every connection opens WAL, so reads never block the running writers.

Files stay the raw record (`raw_output.jsonl`, `envelope.json`, `agent_map.json`). The db is the queryable mirror. Losing it loses nothing you cannot rebuild.

The skill ships a read-only UI for this db under the selected integration directory: Vue and Vite served by Bun on port 4600, with sessions, a trace waterfall, and per-phase tool-call detail.

```bash
cd .agents/skills/sssf/apps/visualizer && bun install   # Codex integration
# Claude Code: .claude/skills/sssf/apps/visualizer
# Kiro CLI:    .kiro/skills/sssf/apps/visualizer
SSSF_DB=/abs/path/to/your-repo/adws/adw_data/sssf.db bun run server/index.ts &
bunx vite
```

It resolves its target through `--db`, then `SSSF_DB`, then `<cwd>/adws/adw_data/sssf.db`, so one instance can point at any stamped repo. Pass the db explicitly, because the server runs from the app dir.

---

## What is in this branch

```
super-simple-software-factory/          # the deployable factory, and nothing else
├── install.py                          # host-neutral installer entry point
└── .claude/skills/
    ├── sssf-grill-me/SKILL.md           # harness-neutral interview + SSSF spec
    ├── sssf-pick-models/SKILL.md        # harness + model per agent, written into the roster
    └── sssf/                            # canonical factory skill source
        ├── SKILL.md                        # hard rules + request routing table
        ├── cookbooks/                      # 9 orchestrator playbooks, loaded lazily
        ├── references/                     # config / handoff / observability specs
        ├── scripts/                        # install.py, make_config.py, make_adw.py
        ├── apps/visualizer/                # the read-only trace UI (Vue + Vite on Bun)
        └── templates/                      # EXACTLY what install.py stamps
            ├── sssf.config.yaml            # the starter roster
            ├── prompt_engineering/{agent}/ # system.md + user.md per agent
            ├── harness_engineering/        # pi extensions
            └── adws/
                ├── adw_*.py                # the twelve starter workflows
                └── adw_modules/            # ALL low-level logic, ADW scripts stay thin
```

The skill is also what an agent reads to *operate* the factory. `SKILL.md` is the central idea, and the cookbooks are lazily loaded recipes it pulls in one at a time: set up the factory, create an ADW, modify a chain, add an agent, run and monitor. If you can teach an agent to do something, teach it, then go build the thing it cannot.

---

## The twelve starter workflows

Every ADW takes the same shape:

```bash
uv run adws/adw_*.py "<prompt or path/to/prompt.md>" [--config adws/adw_sssf_config/sssf.config.yaml] [--adw-id a1b2c3d4]
```

| ADW | Chain | Reach for it when |
|---|---|---|
| `adw_prompt` | engineer to \<agent\> | one agent, one prompt, `--agent NAME` picks who |
| `adw_scout` | engineer to scout | read-only recon, nothing changes |
| `adw_plan` | engineer to planner | you want the spec before any code |
| `adw_build` | engineer to builder | the plan already exists |
| `adw_quality` | engineer to code(quality) | lint, typecheck, build, no agents at all |
| `adw_plan_build` | planner, builder, git(commit) | small, well-understood work |
| `adw_build_test` | builder, code(test), bounded fix loop | there is a suite to satisfy |
| `adw_build_review` | builder, reviewer, bounded revise loop | "is this what was asked for" matters more than "does it run" |
| `adw_plan_build_test` | plan, build, code(test), git(commit) | the standard chain |
| `adw_plan_build_test_quality` | same, plus lint/typecheck/build gates | the repo has quality commands worth enforcing |
| `adw_document` | code(git diff), documenter | write up what just shipped |
| `adw_simple_sdlc` | plan, build, test, review, document | the work is real and its shape is not obvious |

`adw_simple_sdlc` lands three commits from three authors. The plan, the code, and the write-up each get their own, and each message is the words of the agent that produced it.

`--adw-id` is optional everywhere. Omit it and a fresh id is minted and printed. Supply it and the run joins that session: same dirs, same `context_handoff/`, and each agent **resumes its existing context window** through `agent_map.json` instead of starting cold. That is how you chain workflows.

```bash
uv run adws/adw_plan.py "add a /health endpoint"              # prints adw_id a1b2c3d4
uv run adws/adw_build_test.py "implement the plan" --adw-id a1b2c3d4
```

Watch a run with the trace db directly:

```bash
sqlite3 adws/adw_data/sssf.db "select adw_id, status, substr(request,1,60), total_tokens, total_cost, total_credits from sessions order by started_at desc limit 10;"
sqlite3 adws/adw_data/sssf.db "select seq, name, kind, owner, status from phases where adw_id='a1b2c3d4' order by seq;"
sqlite3 adws/adw_data/sssf.db "select kind, name, pid, command from processes where adw_id='a1b2c3d4' and ended_at is null;"
```

Reads never block a running workflow, the db is WAL. `install.py` stamps a `justfile` wrapping all of the above, so in a fresh repo these are `just sessions`, `just phases <adw_id>`, `just tail <adw_id>`, and `just procs <adw_id>`.

---

## Where it can still fail

Honest edges, because knowing them is cheaper than discovering them.

| Failure | What actually happens | What to do |
|---|---|---|
| The test phase reports green on a fresh install | `quality.py` ships placeholder commands that exit 0. Three ADWs run them as their test phase | Wire your real commands into `quality.py` before trusting `adw_build_test`, `adw_plan_build_test`, or `adw_simple_sdlc`. This is the first thing to customize |
| A bare Pi model pattern | The same model can sit under several Pi providers, so `gemini-3.6-flash` may be ambiguous | Qualify Pi models as `provider/model-id` |
| `just` is not installed | The stamped `justfile` is a convenience wrapper, nothing depends on it | Every recipe is a one-line `uv run` or `sqlite3` command. Open the justfile and run the line yourself |
| A coding agent hangs silently | No events, no tokens, an empty `raw_output.jsonl`. The trace goes quiet rather than red | Query `processes` for what is alive and kill it children-first. A killed run finalizes its own trace to `fail` |
| The synced triad drifts | Type, `## Report` example, and `output_type=` disagree, so every call burns correction rounds | Grep the type name and fix all three in one edit |
| Gates pass, output is bad | Gates check what a predicate can check, not plan quality or code taste | Run the `reviewer`, or read it yourself |
| An agent edits something it should not | Detected and rolled back after the call, and the phase fails | Expected. Widen that agent's `writes` if the change was legitimate |
| Commit phase has nothing to commit | `commit_all` raises if the cwd is not a git repo or nothing changed | `git init` with one commit first. A no-op build fails the phase rather than committing nothing |
| `install.py --force` | Overwrites **all** stamped files, config and prompts included | Commit before you force |
| `harness_engineering` set on any non-Pi agent | It's Pi-only (pi extensions); `agents.validate()` fails objectively | Clear the list, or set `coding_agent: pi` |
| An `antigravity` agent dies mid-turn | `agy` returns `Internal error encountered.` or `The stream was interrupted.` and the phase fails. Measured in 5 of 9 identical runs inside a ~380-file repo, 0 of 4 in a small one — it ingests the workspace, and a big context makes the upstream stream unreliable | Re-run with `--adw-id` to retry only the failed phase. SSSF will not auto-resume: a stream-killed turn can poison the conversation so every later request on it fails |
| A `kiro_cli` phase reports 0 tokens | Correct, not a bug: Kiro's docs state per-session token counts are unavailable. It bills credits, shown by `just sessions`, the visualizer, and the console | Read credits, not tokens, for Kiro. A mixed roster's `total_tokens` undercounts by design |
| The first chain in a fresh repo commits the whole factory | `commit_all` is `git add -A`, so anything the install left untracked lands in the builder's commit under the builder's message | Commit the stamped factory yourself right after `install.py`, before the first chain |

Also missing on purpose, so you know what to add: this runs on your current branch. For real work you want a branch per run, a sandbox around the agent, and a merge step at the end.

**Is this overkill for a one-off feature?** Yes. Prompt an agent and move on. This earns its keep when the same workflow runs a hundred times, when validation is the only thing standing between you and a bad merge, and when you need the thousandth run to look like the first.

---

## Built to be Observed, Customized, and Reused

This is a starting point, not a product. Nothing here is meant to survive contact with your codebase unchanged.

The tests it ships are not your tests. The prompts it ships describe a demo app, not your domain. The roster names the models that were good the week it was written. All of that is supposed to be replaced, and the whole thing is shaped so that replacing it is a small edit in an obvious file instead of a rewrite. That is what those three properties are for. **Observable** so you can see which part is actually costing you. **Customizable** so the fix is one file. **Reusable** so you do it once and stamp it everywhere.

Where to start, roughly in the order that pays off fastest:

| Change | File | Why |
|---|---|---|
| Your real commands | `adws/adw_modules/quality.py` | The shipped blocks are placeholders that exit 0. Until you wire this, your test phase is theater |
| Your prompts | `adws/adw_data/prompt_engineering/{agent}/` | Where your standards live: what a good plan looks like, what a review has to catch |
| Your roster | `adws/adw_sssf_config/sssf.config.yaml` | Models, thinking levels, tools, and what each agent is allowed to write |
| Your chains | `adws/adw_*.py` | Copy the closest workflow and edit the phase list. They are 40 to 180 lines on purpose |
| Your definition of done | `adws/adw_modules/gates.py` | A gate is one function. Whatever "done" means where you work, write it here |
| Your agent capabilities | `adws/adw_data/harness_engineering/` | Pi extensions, a different set per agent if that is what the job needs |

And what it deliberately does not do. It runs on your current branch. There is no sandbox, no branch per run, no merge step, no cloud, and no human-in-the-loop approval phase. Those are the obvious next things to build. They are left out so the core stays small enough to read in one sitting, which is the only reason you would trust it enough to change it.

So take it. Fork it, strip the parts you do not need, rename the agents, throw out half the workflows, and roll what is left into the factory your product actually needs. The specific chains in here matter far less than the shape: code owns the loop, agents own the phases, and every run leaves a trace you can go read.

---

## See it in a real repo

The [`example` branch](../../tree/example) is this same skill with the factory already stamped in: a populated `adws/`, a `justfile`, a demo app the factory planned, built, tested, reviewed, and documented, and the specs, docs, and traces those runs produced.

```bash
git clone <this-repo> sssf && cd sssf
git checkout example
```

---

## License

MIT, see [`LICENSE`](LICENSE).

---

## Master Agentic Coding

<p align="center">
  <img src="images/08_rise_with_the_ceiling.svg" alt="Vibe coding sits inside a narrow band with a short arrow of headroom above it, agentic engineering rises far above that band with a tall one" width="850">
</p>

Vibe coding is not knowing how your system works, and not looking. Agentic engineering is knowing how your system works so well that you do not have to look.

Master agentic coding by gaining a deeper understanding of the foundational units of the software factory.

Learn tactical agentic coding patterns with [Tactical Agentic Coding](https://agenticengineer.com/tactical-agentic-coding?y=sssf).

Follow the [IndyDevDan YouTube channel](https://www.youtube.com/@indydevdan) to improve your agentic coding advantage.

---

Stay Focused and Keep Building

- IndyDevDan

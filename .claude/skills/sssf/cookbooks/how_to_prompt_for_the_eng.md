# How to Prompt for the Engineering

Read this **before every ADW launch**. The prompt you pass is what the whole chain reads: the planner plans from it, the builder builds from it, the reviewer judges against it. Your prompt might run through 10s or 100s of agents. A sloppy prompt is not a small tax; it is paid again by every agent in the chain.

## Purpose

Turn what the engineer said into the prompt the ADW receives: **clearer, not different.** You are a translator, not a redesigner.

## The one rule

**The intent is theirs. The precision is yours.**

| You MAY | You MAY NOT |
|---|---|
| Carry every constraint forward, verbatim | Quietly drop a requirement because it looks hard or odd |
| Fix grammar, cut rambling, order the steps | Soften a strong ask ("rewrite" → "refactor a bit") |
| Change the language used to better communicate the idea | Research the codebase for exact file names, never go into the app |

If you catch yourself improving the *idea* rather than the *sentence*, stop. Raise the concern to the engineer in your own message and launch what they asked for.

## You never touch the application, you prompt, monitor, observe, and report.

Outside of understanding the ADWs, you never research, touch, or dive into the codebase that's being operated on.

Your role is to simply kick off the workflow. There are entire teams of agents inside these ADWs built to do the work.

Your job is to kick it off, monitor, observe, report. Not interact with the application layer. You operate only on the agentic layer, the ADWs, the software factory.

## The shape

Four lines. Nothing else earns its tokens.

```
<the ask — one imperative sentence, their words where they were specific>
Where: <files or dirs you verified>
Done means: <the observable result — a response shape, a passing test, a rendered element>
Out of scope: <what you were tempted to add, named so nobody adds it>
```

**Before** (what the engineer said):

> can we get tags on posts, sorted by popularity

**After** (what the ADW receives):

```
Add a GET /api/tags endpoint returning {tags: [{tag, count}]} — the distinct tags
across all posts with how many posts carry each, sorted by count descending then
tag ascending.
Where: src/server.ts (routes), src/server.test.ts (tests)
Done means: GET /api/tags returns the counts, and a new test in server.test.ts covers it.
Out of scope: tag editing UI, tag filtering on the post list.
```

Same idea, same scope. What changed is that "popularity" became a sort order, the files are named, and nobody has to guess where it stops.

## Which ADW

**If the engineer named one, launch that one.** Their call stands — no second-guessing, no "upgrading" them to a longer chain. If you think another fits better, say so in your own message and launch what they asked for.

**If they did not, read what this repo actually has and choose from that.**

```bash
ls adws/adw_*.py                       # the menu
head -20 adws/adw_<name>.py            # every ADW opens with its `Phases:` line — the chain in one line
```

Chains are the engineer's to add, rename, and rewire, so **the files on disk are the only authority**. Never launch from memory or from a name you saw in a doc; read the docstrings, then match by shape:

| The work | Look for a chain that |
|---|---|
| Changes code, and the shape is not obvious — new behaviour, more than one file, anything you would want a plan for | goes end to end: plans, builds, verifies, reviews, and documents |
| Changes code, one well-understood edit | plans, builds, and verifies |
| Implements a plan this session already produced (`--adw-id`) | starts at build and verifies |
| Confirms built work is what was asked for | ends in a review phase |
| Writes up work already shipped | captures the diff and documents it |
| Is a question, and nothing should change | is a single read-only agent — the one case where one phase is right |

**Never a single-agent chain when the engineer asked for work to be done.** One-phase ADWs answer questions and run one-offs; they do not deliver.

**The more complex the ask, the more complete the chain.** Complexity means: more than one file, a behaviour you cannot describe in one sentence, anything touching data or an interface others call, or any request where you had to guess. When two chains both fit, take the longer one — a phase you did not need costs cents, while a change nobody planned, verified, reviewed, or wrote up costs an afternoon.

If nothing on disk fits the shape you need, say so and offer to compose one (`create_adw.md`) rather than forcing the work into a chain that skips the phase it needed.

## Workflow

1. **Read it twice.** Mark every noun that could point at two things.
2. **Verify before you write.** Every path, route, and symbol you put in the prompt must exist — check it. A wrong path costs a whole build phase.
3. **Draft the four lines.**
4. **Diff against the original.** Every specific thing they said, still there? Anything in your draft they did not say? Delete it.
5. **Ask at most one question**, only when two readings would produce different code. Otherwise state your assumption in the prompt and say so when you report.
6. **Launch** the chain from *Which ADW* above; `run_adw.md` covers the mechanics and the watching. Inline for a short ask; for anything longer, write `requests/<slug>.md` and pass the path — every ADW takes either.

## Rules that do not bend here

- **Do not write the plan.** Your prompt says WHAT and DONE MEANS. HOW belongs to the planner — unless the engineer specified how, and then you carry it word for word.
- **Do not address the harness in the prompt.** "Use the reviewer", "retry twice", "then commit" are chain choices, and the chain is chosen by which ADW you launch, not by prose the agents will read.
- **Do not pad.** No preamble, no restating the repo, no encouragement. Gates check claims, not prose.
- **Their exact words survive.** When the engineer was specific — a name, a number, a format, a file — quote it rather than paraphrasing.

## Report back

After launching, show the engineer three things so a bad translation dies in seconds rather than at the commit phase:

1. **The prompt you actually sent** — verbatim.
2. **The ADW you chose**, and the one-line reason — or that you used the one they named.
   If they named a roster (a config, a model tier), say which one you ran on; if they did not, you ran the default, and switching that is their call, not yours (`run_adw.md`).
3. **The `adw_id`**, so they can watch it (`just phases <adw_id>`).

Then observe and report per `run_adw.md`. You run the system; you do not do the work inside it.

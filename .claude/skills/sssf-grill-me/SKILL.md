---
name: sssf-grill-me
description: Turn a developer's initial software request into an approved, codebase-grounded specification through a focused interview. Use when a request is still ambiguous and should be clarified before it is sent to SSSF. Do not implement the change or run SSSF.
---

# SSSF Grill Me

Produce a specification that the developer understands, approves, and can pass
directly to SSSF. Inspect the repository before interviewing so questions are
about product decisions, not facts the code can answer.

## Boundaries

- Stay read-only while investigating. Do not modify source, tests, config, or Git.
- Do not plan the implementation in place of SSSF. Record relevant codebase
  constraints and likely integration points, but leave task decomposition to the
  SSSF planner.
- Do not invoke SSSF. The developer decides when to submit the approved file.
- Write only the final approved specification under `specs/`.
- Use the developer's language unless they request another one.

## Harness portability

Use only capabilities shared by ordinary coding harnesses: repository file
discovery, read-only code inspection, conversation, and writing the final
Markdown file. Do not depend on a harness-specific agent, planning mode, command,
memory feature, or question tool.

If the harness offers a structured question tool, it may be used when it makes a
decision easier to answer. Otherwise ask the same focused question directly in
the conversation. Lack of a specialized tool must never block the interview.
Keep every path repository-relative so the deliverable has the same contract in
Claude Code, Codex, or another compatible harness.

## Ground the interview

Read the repository instructions first (`AGENTS.md`, `CLAUDE.md`, or equivalents),
then inspect only the code, tests, configuration, and documentation relevant to
the request. Establish:

- current behavior and the path through the code;
- existing conventions and reusable components;
- constraints that affect the requested behavior;
- tests or commands that currently verify the affected area.

Briefly tell the developer what the code confirms, citing repository paths. Do
not present guesses as facts.

## Interview

Ask only questions whose answers can materially change scope, behavior, or
acceptance. Work through one decision area at a time; avoid a large questionnaire.
When useful, offer concrete options, their tradeoffs, and a recommendation.

Prioritize unresolved decisions in this order:

1. desired outcome, actor, and user-visible behavior;
2. scope boundaries and explicit non-goals;
3. behavior for important states, errors, permissions, and edge cases;
4. compatibility, data, API, security, migration, or rollout constraints that
   actually apply to the inspected code;
5. observable acceptance criteria and required validation.

Do not ask the developer to choose implementation details already dictated by
the codebase. Challenge contradictions and vague terms such as "fast", "simple",
or "secure" by turning them into observable requirements. Distinguish confirmed
decisions from assumptions and preferences throughout the conversation.

## Readiness gate

The request is ready only when:

- the goal, scope, and non-goals are explicit;
- important behaviors and failure cases are decided;
- acceptance criteria are observable;
- relevant constraints from the codebase are represented;
- no material question remains open, unless it is explicitly deferred with an
  owner and does not block implementation.

Summarize the proposed specification and ask the developer to approve or correct
it. Continue the interview when they correct it. Do not save a "final" file
before approval.

## Deliverable

After approval, create `specs/` if needed and write:

`specs/YYYY-MM-DD-<two-to-five-word-kebab-case-slug>.md`

Never overwrite an existing specification; append `-v2`, `-v3`, and so on. Use
this structure, omitting only sections that truly do not apply:

```markdown
# <Title>

## Context and goal
## Current behavior
## Required behavior
## Scope
## Non-goals
## Relevant codebase constraints
## Edge and failure cases
## Acceptance criteria
## Validation expectations
## Decisions and assumptions
## Deferred questions
```

Requirements and acceptance criteria must be unambiguous enough for a reviewer
to determine pass or fail. In codebase constraints, cite the paths that support
each important statement. `Deferred questions` must say `None` when the spec is
ready, or identify the owner and why each item does not block implementation.

Finish by reporting the saved path and the handoff command:

```bash
just sssf specs/<actual-file-name>.md
```

If that recipe is unavailable, provide the repository's equivalent SSSF command.

# AGENTS.md

Guidance for coding agents working in this repository.

## Project Overview

`mountainash-capabilities` is a companion package to Mountainash. It runs explicitly selected existing pytest witnesses in a caller-supplied environment, retains immutable local evidence, and reads, queries and renders that evidence without re-running anything. Dependency direction is one-way: this package imports Mountainash; Mountainash never depends on it. See [README.md](README.md) for the public API and contracts.

## mountainash-central (MANDATORY)

The sibling **mountainash-central** repository (`../mountainash-central`) is the master for this project's backlog, specs and plans. This repo holds code, user documentation and the changelog only. This file routes to central; it does not restate central content. Read the actual documents, not summaries or memory.

| What | Where |
|---|---|
| Planning entry (all collections) | [04.planning/mountainash-capabilities/README.md](../mountainash-central/04.planning/mountainash-capabilities/README.md) |
| Backlog | [04.planning/mountainash-capabilities/a.backlog/BACKLOG_INDEX.md](../mountainash-central/04.planning/mountainash-capabilities/a.backlog/BACKLOG_INDEX.md) |
| Roadmap design (four phases; design authority) | [2026-10-01 witness checks and evidence-led investigations](../mountainash-central/04.planning/mountainash/superpowers/specs/2026-10-01-witness-checks-and-evidence-led-investigations-design.md) |
| Shared conventions | [_meta/](../mountainash-central/_meta/): backlog, planning, superpowers-index and prioritisation conventions |

There is no principle library for this project yet. Treat the roadmap design's shared contracts (§4) as binding. Do not create principles here.

### Backlog: central is the master

- Every work item has a record in the central backlog. The record owns status, scope, acceptance, dependencies, decisions and delivery evidence. Read [backlog conventions](../mountainash-central/_meta/backlog-conventions.md) before changing records.
- GitHub issues are optional trackers. They do not carry authority and do not link back to central. When an issue exists, the record's **Tracking issue** field links it.
- Record IDs are plain integers. A record created for an existing GitHub issue takes that issue's number; otherwise use the next integer above the largest existing ID. Never renumber or reuse IDs.
- Update the record first, then its row in `BACKLOG_INDEX.md`, in the same commit. On delivery, record the fixing commit or PR in the record and move its row to History.

### Specs and plans location

Save new specs and plans to the project's own central area, not this repo:

- **Specs:** `mountainash-central/04.planning/mountainash-capabilities/superpowers/specs/YYYY-MM-DD-<topic>-design.md`
- **Plans:** `mountainash-central/04.planning/mountainash-capabilities/superpowers/plans/YYYY-MM-DD-<topic>.md`

Documents written before this project had its own area stay under `04.planning/mountainash/superpowers/`, routed from the project planning entry. Do not move them as a side effect of other work. When adding a document, update the project planning entry in the same change and follow the [superpowers index conventions](../mountainash-central/_meta/superpowers-index-conventions.md).

### Central documentation workflow

- Edit central docs only in the primary `../mountainash-central` checkout, on `main`, and commit directly. No docs branches, worktrees or PRs.
- A code branch or worktree here does not change where docs go.
- Central is shared across concurrent sessions. Stage and commit only the files and hunks your task owns. Never stash, reset, check out or overwrite another session's uncommitted work. If a file you must edit already has foreign changes, stage only your hunks.
- Do not push central unless the user asks.

## Development

Python 3.12 or newer. Install development dependencies in a development environment; the execution API never installs into a target environment.

```sh
python -m pip install -e '.[dev]'
python -m pytest tests -q
ruff check src tests
```

The installed-package test needs a built wheel and `MA_COMPANION_INSTALLED_PYTHON`; see the Development section of [README.md](README.md).

Evidence is produced only by real executions. Do not replace witness runs with mocks in acceptance checks.

## Git Flow (mountainash three-tier)

```
feature/* | bugfix/* | hotfix/*  →  develop  →  release/*  →  main + tag
```

Feature, bugfix and hotfix PRs always target `develop`. Never push directly to `develop` or `main`. Releases use CalVer `YY.MM.MICRO`; package publication is not authorized.

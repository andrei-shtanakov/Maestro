# Maestro Dogfood-First Development Roadmap

**Date:** 2026-04-05
**Approach:** Dogfood-First — launch on real project first, let real pain drive priorities
**Timeline:** 4 weeks
**Goal:** Maestro is self-hosted (dogfooding), reliable in both modes, and published as open-source v0.1.0
**Executor:** Solo developer + Maestro self-building (meta-dogfooding)

---

## Context

Maestro is an AI Agent Orchestrator with two modes:
- **Mode 1 (Task Scheduler):** DAG-based scheduling of AI agents in a shared directory
- **Mode 2 (Multi-Process Orchestrator):** decompose project into zadachi, run each in isolated git worktree, create PRs

Current state: 22 source files, 944 tests, clean architecture, but **never run on a real project**. Extensive analysis exists in `_cowork_output/` (8 documents, 34 tasks in backlog) — all theoretical. This plan prioritizes real usage over theoretical improvements.

### Key Principle

**Priorities each week are determined by real usage of the previous week, not the theoretical backlog.** `DOGFOOD_LOG.md` is the living prioritization document. The 34-task backlog from `_cowork_output/08-tasks.md` serves as a reference, not a prescription.

---

## Week 1: First Real Launch

### Goal
Mode 1 (Task Scheduler) reliably completes a run on a real project with 5-10 tasks and 2-3 agents.

### Days 1-2: Critical Safety Fixes

Only fixes that make the first launch safe. Nothing else.

| Task | Backlog ID | Why it blocks launch | Files |
|------|-----------|---------------------|-------|
| flock on PID file | T-02 | Two `maestro run` = corrupted SQLite | `maestro/cli.py` |
| Shutdown grace period 5s | T-05 | 0.5s kills agents before state save | `maestro/scheduler.py`, `maestro/orchestrator.py` |
| DEBUG logging in except blocks | T-07 | Silent errors invisible during dogfooding | 4 files (scheduler, orchestrator, validator, pr_manager) |
| Fix blocking async calls | T-14 | Event loop can hang | `maestro/orchestrator.py`, `maestro/scheduler.py` |

**Explicitly NOT doing** from quick wins: jitter (T-01), max_concurrent cap (T-03), entry points (T-06), dedup cycles (T-08) — they don't block the first launch.

### Days 3-4: First Real Run

1. **Choose a target project** — a small real project from `labs/`, or Maestro itself
2. **Write `tasks.yaml`** with 5-8 tasks with dependencies (refactoring, tests, docs)
3. **Run `maestro run tasks.yaml`** with Claude Code spawner
4. **Log everything** in `DOGFOOD_LOG.md` — bugs, UX friction, confusion, missing features

### Day 5: Triage

- Categorize issues from DOGFOOD_LOG.md by severity
- Cross-reference with `_cowork_output/08-tasks.md` backlog — which theoretical issues turned out real?
- Form Week 2 backlog from actual pain, not theoretical analysis

### Week 1 Deliverables
- [ ] 4 safety fixes landed and tested
- [ ] Working `tasks.yaml` for a real project
- [ ] First successful `maestro run` completion
- [ ] `DOGFOOD_LOG.md` with prioritized issues

---

## Week 2: Fix Real Pain + Meta-Dogfooding Cycle #1

### Goal
Bugs found during dogfooding are fixed. Maestro is used to implement its own improvements (first meta-dogfooding cycle).

### Days 1-2: Fixes from DOGFOOD_LOG.md

Concrete tasks determined by Week 1 findings. Likely candidates based on `_cowork_output` analysis:

| Likely issue | Backlog ID | Why probable |
|-------------|-----------|-------------|
| Cascade failure (upstream fails, downstream hangs forever) | T-12 | No BLOCKED status, scheduler loops infinitely |
| Result summary always "Task completed successfully" | T-13 | Useless for understanding what happened |
| Config validation only at runtime | T-04 | YAML errors discovered too late |
| Worktree cleanup after crash | T-15 | Orphaned worktrees accumulate |

Plus unexpected bugs from real usage.

### Days 3-5: Meta-Dogfooding Cycle #1

**Compose `maestro-improvements.yaml`** — a DAG of 4-6 tasks to improve Maestro itself:

```yaml
tasks:
  - id: jitter-retry
    title: "Add jitter to retry backoff"
    agent: claude_code
    scope: ["maestro/retry.py", "tests/test_retry.py"]

  - id: config-validate
    title: "Add maestro config validate CLI"
    agent: claude_code
    scope: ["maestro/cli.py", "tests/test_cli.py"]

  - id: adaptive-polling
    title: "Adaptive polling interval"
    agent: claude_code
    scope: ["maestro/scheduler.py"]
    depends_on: [jitter-retry]

  - id: e2e-tests
    title: "E2e tests with AnnounceSpawner"
    agent: claude_code
    scope: ["tests/test_e2e.py"]
    depends_on: [jitter-retry, config-validate]
```

**Run `maestro run maestro-improvements.yaml`** and observe:
- Does parallelization work (jitter + config-validate simultaneously)?
- Is scope isolation correct?
- What happens on agent error?
- How useful is the dashboard for monitoring?

### Week 2 Deliverables
- [ ] Critical dogfooding bugs fixed
- [ ] First successful "Maestro builds Maestro" cycle
- [ ] Updated `DOGFOOD_LOG.md` with cycle #2 observations
- [ ] `maestro-improvements.yaml` as a reusable example

---

## Week 3: Mode 2 + Double Dogfooding

### Goal
Mode 2 (Multi-Process Orchestrator) works on a real scenario. Mode 1 is used to fix Mode 2 issues — double dogfooding.

### Days 1-2: Mode 2 — Prep and First Launch

**Preparation:**
- Test `maestro orchestrate` on minimal example (2-3 zadachi)
- Fix critical blockers if Mode 2 doesn't start (likely: worktree creation, spec-runner integration, PR creation via `gh`)
- Write `project.yaml` for a real scenario — batch refactoring of Maestro itself

```yaml
project:
  name: "Maestro Architecture Cleanup"
  repo: "."

zadachi:
  - id: docs-split
    title: "Split COWORK_CONTEXT into ARCHITECTURE + ROADMAP"
    scope: ["COWORK_CONTEXT.md", "ARCHITECTURE.md", "ROADMAP.md"]

  - id: spawner-helper
    title: "Extract _spawn_with_log() in base spawner"
    scope: ["maestro/spawners/**"]

  - id: env-vars
    title: "Pass structured metadata to agents via env vars"
    scope: ["maestro/spawners/**"]
    depends_on: [spawner-helper]
```

**Observe:** worktree creation, branch isolation, PR creation via `gh`, cleanup after completion.

### Days 3-4: Mode 2 Fixes via Mode 1

Use **Mode 1** to fix problems found in **Mode 2**:

```
maestro run mode2-fixes.yaml
```

Double dogfooding:
- Mode 1 verified in weeks 1-2, now a working tool
- Mode 2 is the test subject, its bugs are fixed through Mode 1
- Each cycle tests both modes simultaneously

### Day 5: Open-Source Readiness Assessment

Checklist:
- [ ] Mode 1: stable for 5-10 tasks with Claude Code
- [ ] Mode 2: stable worktree creation, agent execution, PR creation
- [ ] Critical bugs from DOGFOOD_LOG.md closed
- [ ] Working YAML examples for both modes

### Week 3 Deliverables
- [ ] Mode 2 successfully creates worktrees, runs agents, creates PRs
- [ ] At least one "Mode 1 fixes Mode 2" cycle completed
- [ ] Readiness checklist passed
- [ ] `project.yaml` example for Mode 2

---

## Week 4: Open-Source Prep + Publication

### Goal
Maestro published on GitHub, anyone can clone, run and understand the value in 10 minutes.

### Days 1-2: Documentation and Examples

**README.md — full rewrite:**
- Hero section: one sentence + GIF/screenshot of dashboard with running DAG
- Quick start: `uv add maestro && maestro run examples/hello.yaml` — works in 2 minutes
- Two clear sections: Mode 1 (Task Scheduler) and Mode 2 (Multi-Process Orchestrator)
- "Maestro builds Maestro" section — meta-dogfooding story as proof of concept

**Working examples:**
```
examples/
  hello.yaml                  # Minimal: 3 tasks, AnnounceSpawner, no real agents
  parallel-refactor.yaml      # Mode 1: 5-8 tasks with Claude Code
  multi-worktree.yaml         # Mode 2: decompose -> worktrees -> PRs
  maestro-builds-maestro.yaml # Meta-dogfooding config (from week 2)
```

**ARCHITECTURE.md:**
- Only what is actually implemented in code (not aspirational)
- State machine diagrams, data flow diagrams
- Clear separation from ROADMAP.md (future plans)

### Days 3-4: Code Cleanup + Open-Source Hygiene

**Use Maestro Mode 1 for final cleanup:**

```yaml
tasks:
  - id: license
    title: "Add LICENSE (MIT), CHANGELOG.md"
    agent: claude_code
    scope: ["LICENSE", "CHANGELOG.md"]

  - id: ruff-cleanup
    title: "Ruff format + fix across codebase"
    agent: claude_code
    scope: ["maestro/**"]

  - id: type-check
    title: "Pyrefly check, fix type errors"
    agent: claude_code
    scope: ["maestro/**"]
    depends_on: [ruff-cleanup]

  - id: test-pass
    title: "Ensure all 944+ tests pass"
    agent: claude_code
    scope: ["tests/**"]
    depends_on: [ruff-cleanup]

  - id: gitignore-cleanup
    title: "Clean .gitignore, remove artifacts"
    agent: claude_code
    scope: [".gitignore"]
```

**Quality checklist:**
- [ ] `uv run ruff check .` — clean
- [ ] `uv run pyrefly check` — clean
- [ ] `uv run python -m pytest` — all 944+ tests green
- [ ] No secrets, .env, hardcoded paths in code
- [ ] pyproject.toml: correct metadata (author, description, urls, license)

### Day 5: Publication

- Final commit + tag `v0.1.0`
- Push to GitHub (public)
- Optional: post on Twitter/LinkedIn with GIF demo

### Week 4 Deliverables
- [ ] README.md rewritten with quick start and examples
- [ ] ARCHITECTURE.md (reality-only, no aspirational content)
- [ ] CHANGELOG.md + LICENSE (MIT)
- [ ] 4 working example YAML configs
- [ ] All quality checks passing
- [ ] Tag `v0.1.0` pushed to public GitHub

---

## Summary

| Week | Focus | Key Deliverable |
|------|-------|----------------|
| 1 | Safety fixes + first real Mode 1 launch | Working run on real project, DOGFOOD_LOG.md |
| 2 | Fix real pain + meta-dogfooding cycle #1 | Maestro improves itself via Mode 1 |
| 3 | Mode 2 launch + double dogfooding | Both modes working, worktrees -> PRs |
| 4 | Open-source prep + publication | v0.1.0 on GitHub, README, examples, clean code |

## Backlog Reference

The 34-task backlog in `_cowork_output/08-tasks.md` remains the reference for specific implementation tasks. This roadmap determines **when and why** tasks get picked up — driven by real dogfooding pain, not theoretical priority.

Tasks explicitly scheduled:
- **Week 1:** T-02, T-05, T-07, T-14
- **Week 2:** Determined by DOGFOOD_LOG.md (likely: T-04, T-12, T-13, T-15) + quick wins for meta-dogfooding
- **Week 3:** Mode 2 specific issues + T-16 (docs split), T-33 (spawner helper), T-23 (env vars)
- **Week 4:** Open-source hygiene (not in backlog)

Tasks explicitly deferred (post v0.1.0):
- T-09 (BasePollLoop) — major refactoring, do after stable usage
- T-19 (Event System) — depends on T-09
- T-20 (Arbiter routing) — no API spec yet
- T-21 (ATP validation) — no API spec yet
- T-22 (Agent concurrency pools) — not needed at current scale
- T-26 (Heartbeats) — requires event system
- T-30 (Conditional tasks) — nice-to-have
- T-31 (Push vs pull) — architectural decision, not urgent
- T-32 (DB per project) — not needed for single-user

## Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|-----------|
| Mode 1 first launch reveals fundamental issues (DB schema, spawner protocol) | Week 1 extends to 2 weeks | Safety fixes are scoped small; if fundamentals break, pivot to fixing before dogfooding |
| spec-runner (external PyPI package) incompatible or missing for Mode 2 | Week 3 blocked | Test `spec-runner` availability in Week 2 Day 5; if unavailable, Mode 2 runs with Claude Code directly instead of spec-runner |
| Meta-dogfooding YAML examples don't match actual Maestro config schema | Examples don't work | Validate all YAML examples against `ProjectConfig` / `OrchestratorConfig` Pydantic models before committing |
| Claude Code spawner has undiscovered issues at scale | Agents fail silently | T-07 (DEBUG logging) done first; DOGFOOD_LOG.md captures all anomalies |

## Non-Goals for v0.1.0

- Distributed mode / multi-machine orchestration
- Arbiter integration (no API spec)
- ATP Platform integration (no API spec)
- TUI dashboard (web dashboard sufficient)
- Python SDK / programmatic API
- Windows support

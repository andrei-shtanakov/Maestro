# Maestro v0.1.0 Open-Source Prep

**Date:** 2026-04-06
**Goal:** Prepare Maestro for public GitHub release as v0.1.0
**Method:** Maestro Mode 1 dogfooding — Maestro prepares its own release

---

## Context

Weeks 1-3 of the dogfood roadmap are complete. Mode 1 and Mode 2 both work reliably. This is the final prep before publishing.

## Tasks (DAG)

```
license         ──┐
pyproject-meta  ──┼──> readme
examples        ──┘
```

### license
Create MIT LICENSE and CHANGELOG.md.
- LICENSE: MIT, year 2026, author from pyproject.toml
- CHANGELOG.md: v0.1.0 entry summarizing Mode 1 (Task Scheduler), Mode 2 (Multi-Process Orchestrator), dogfooding results
- scope: `LICENSE`, `CHANGELOG.md`

### pyproject-meta
Update pyproject.toml with proper open-source metadata.
- description, author, author-email
- urls: Homepage, Repository, Issues (all pointing to GitHub)
- license = {text = "MIT"}
- classifiers: Python 3.12, MIT, Development Status 3 - Alpha
- scope: `pyproject.toml`

### examples
Create/update 4 working YAML example configs in examples/.
- `hello.yaml` — minimal 3-task example with AnnounceSpawner (no real agents needed)
- `parallel-refactor.yaml` — Mode 1 with 5 tasks, Claude Code, DAG dependencies
- `project.yaml` — update existing Mode 2 example with realistic values
- `maestro-builds-maestro.yaml` — rename/update existing dogfood config as showcase
- scope: `examples/**`

### readme
Full rewrite of README.md. Depends on: license, pyproject-meta, examples.
- Hero: one-line description + what it does
- Quick start: `uv add maestro && uv run maestro run examples/hello.yaml`
- Two sections: Mode 1 (Task Scheduler) and Mode 2 (Multi-Process Orchestrator)
- Link to examples, CHANGELOG, LICENSE
- Keep concise — under 300 lines
- scope: `README.md`

## Execution

Run via Maestro Mode 1:
```bash
uv run maestro run v010-prep.yaml --clean
```

## Success Criteria

- LICENSE exists (MIT)
- CHANGELOG.md has v0.1.0 entry
- pyproject.toml has all metadata fields
- 4 working examples in examples/
- README.md is a proper open-source README
- All existing tests still pass

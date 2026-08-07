"""`maestro service` — scheduled autonomous runs.

Implements `docs/superpowers/specs/2026-08-06-service-install-design.md`
(revision 2). The scheduler starts a Maestro-owned wrapper, never
`orchestrate` directly: only Maestro's own state can decide resume vs
fresh vs no-op.
"""

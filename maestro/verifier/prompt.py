"""Judge prompt + pinned fake-done taxonomy + judge-policy hash.

Design §5/§6/§9: the judge is deliberately adversarial — it must read a
task's diff "as if it is broken" and look for a fixed, named set of
fake-done patterns, then answer with ONLY the strict raw-payload schema
(`maestro.verifier.envelope.RAW_PAYLOAD_SCHEMA`). `profile_sha256()` hashes
the prompt version, the raw schema, and the taxonomy together so that ANY
policy change (prompt wording, schema, or taxonomy) is detectable/auditable
via the sealed verdict's `identity.profile_sha256` (design §5).
"""

from __future__ import annotations

import hashlib
import json

from maestro.verifier.envelope import RAW_PAYLOAD_SCHEMA


JUDGE_PROMPT_VERSION = "verifier-judge-v1"

# The pinned fake-done taxonomy (design §12): a fixed, named enum of patterns
# the judge must specifically look for. Adding/removing/redefining an entry
# is a policy change and MUST change `profile_sha256()` — that is the whole
# point of hashing this list into the profile.
FAKE_DONE_TAXONOMY: list[dict[str, str]] = [
    {
        "id": "stub_implementation",
        "definition": (
            "The function/class body is a placeholder (e.g. `pass`, "
            "`...`, `raise NotImplementedError`) instead of real logic "
            "that fulfills the task's prompt."
        ),
    },
    {
        "id": "hardcoded_return",
        "definition": (
            "The code returns a fixed/constant value that happens to make "
            "the validation command pass, rather than computing the "
            "result from its inputs."
        ),
    },
    {
        "id": "test_tautology",
        "definition": (
            "A test asserts something that is trivially true regardless "
            "of the implementation (e.g. `assert True`, asserting a value "
            "against itself, or asserting on a mock with no real "
            "assertion on behavior)."
        ),
    },
    {
        "id": "commented_out_assertion",
        "definition": (
            "A previously meaningful assertion or check was commented out, "
            "deleted, or replaced with a no-op instead of being fixed."
        ),
    },
    {
        "id": "unreachable_guard",
        "definition": (
            "A conditional/guard was added around the risky code path such "
            "that the path validation actually exercises can never reach "
            "the real logic (e.g. an `if False:`, an always-true early "
            "return before the intended work)."
        ),
    },
    {
        "id": "swallowed_exception",
        "definition": (
            "An exception that should surface a real failure is caught and "
            "silently ignored (bare `except: pass` or equivalent) so the "
            "validation command reports success despite an underlying "
            "error."
        ),
    },
    {
        "id": "todo_left_as_done",
        "definition": (
            "The diff leaves a `TODO`/`FIXME`/`XXX` (or prose to the same "
            "effect) marking the actual required work as still "
            "unimplemented, while the task is presented as complete."
        ),
    },
    {
        "id": "scope_creep_unrelated_edit",
        "definition": (
            "The diff's substantive change is unrelated to the task's "
            "prompt/scope (e.g. only formatting/comment churn, or edits "
            "that don't address the stated requirement)."
        ),
    },
]


def _format_taxonomy() -> str:
    """Render the taxonomy as a numbered list for the prompt text."""
    return "\n".join(
        f"{i}. `{entry['id']}` — {entry['definition']}"
        for i, entry in enumerate(FAKE_DONE_TAXONOMY, start=1)
    )


JUDGE_PROMPT = f"""You are an adversarial code-change judge.

You will be given, on stdin, a JSON envelope describing a single task: its
title, prompt/instructions, validation command, scope, a manifest of
changed paths, and the unified diff of the change (bounded to the task's
scope). A deterministic `validation_cmd` has ALREADY passed for this diff.
Your job is to catch the cases where that command passing does NOT mean
the task is genuinely done.

Read the diff as if it is broken. Assume the author may have gamed the
validation command rather than solved the task, and actively look for
evidence of that. In particular, check for these known "fake-done"
patterns:

{_format_taxonomy()}

These patterns are not the only ways a change can be wrong — also judge
the diff against the task's own prompt and scope — but they are specific,
named failure modes you must always check for explicitly.

Output ONLY a single JSON object matching this exact schema, and nothing
else (no markdown fences, no commentary, no additional keys):

{json.dumps(RAW_PAYLOAD_SCHEMA, sort_keys=True, indent=2)}

`verdict` is `"pass"` only if the diff genuinely and completely fulfills
the task's prompt within its scope, with no fake-done pattern present.
Otherwise `verdict` is `"fail"`, and `findings` must list each concrete
problem found (`criterion_id` naming what was violated, `severity`,
`evidence` quoting/pointing at the offending part of the diff, and
`author_feedback` — actionable text telling the author what to fix).
"""


def _canonical_profile_payload() -> dict[str, object]:
    """The exact payload `profile_sha256` hashes — exposed for testability.

    Any of these three inputs changing (prompt version, raw schema, or
    taxonomy) must change the resulting hash; that is the auditability
    guarantee `profile_sha256` exists to provide.
    """
    return {
        "prompt_version": JUDGE_PROMPT_VERSION,
        "raw_payload_schema": RAW_PAYLOAD_SCHEMA,
        "taxonomy": FAKE_DONE_TAXONOMY,
    }


def profile_sha256() -> str:
    """SHA-256 over the judge policy: prompt version + raw schema + taxonomy.

    Deterministic and stable for a fixed policy; changes whenever the
    prompt version, the raw payload schema, or the taxonomy list changes
    (design §5's `profile_sha256` identity field).

    Returns:
        A 64-character lowercase hex SHA-256 digest.
    """
    canonical = json.dumps(
        _canonical_profile_payload(), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()

"""Standalone merge-logs CLI — time-sorts per-pid JSONL into merged.jsonl.

Works on partial runs (after SIGKILL). Tolerates malformed lines
(drops them with a stderr warning).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _load_records(path: Path) -> list[dict]:
    """Load valid JSON records from a JSONL file, skipping malformed lines."""
    records: list[dict] = []
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            sys.stderr.write(f"warning: {path}:{lineno} malformed JSON, skipped\n")
    return records


def merge_logs_dir(pipeline_dir: Path) -> Path:
    """Merge all *.jsonl under pipeline_dir (excluding merged.jsonl) by Timestamp."""
    pipeline_dir = Path(pipeline_dir)
    all_records: list[dict] = []
    for jsonl in sorted(pipeline_dir.glob("*.jsonl")):
        if jsonl.name == "merged.jsonl":
            continue
        all_records.extend(_load_records(jsonl))
    all_records.sort(key=lambda r: int(r.get("Timestamp", "0")))
    out = pipeline_dir / "merged.jsonl"
    out.write_text(
        "\n".join(json.dumps(r) for r in all_records) + ("\n" if all_records else "")
    )
    return out


def main(argv: list[str] | None = None) -> int:
    """Entry point for merge-logs standalone invocation."""
    argv = argv or sys.argv[1:]
    if not argv:
        sys.stderr.write("usage: maestro merge-logs <pipeline_dir-or-id>\n")
        return 2
    target = Path(argv[0])
    if not target.exists():
        # allow passing a pipeline_id; resolve under ./logs/
        candidate = Path("logs") / argv[0]
        if candidate.exists():
            target = candidate
        else:
            sys.stderr.write(f"error: {argv[0]} not found\n")
            return 1
    out = merge_logs_dir(target)
    print(str(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

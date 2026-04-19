"""Unit tests for maestro.merge_logs."""

from __future__ import annotations

import json
from pathlib import Path  # noqa: TC003 — used in function annotation at runtime

from maestro.merge_logs import merge_logs_dir


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def test_merge_sorts_by_timestamp_across_files(tmp_path):
    pipeline_dir = tmp_path / "01HZKX"
    pipeline_dir.mkdir()
    _write_jsonl(
        pipeline_dir / "maestro-100.jsonl",
        [
            {"Timestamp": "1000000000000000002", "Body": "b"},
            {"Timestamp": "1000000000000000004", "Body": "d"},
        ],
    )
    _write_jsonl(
        pipeline_dir / "spec-runner-200.jsonl",
        [
            {"Timestamp": "1000000000000000001", "Body": "a"},
            {"Timestamp": "1000000000000000003", "Body": "c"},
        ],
    )
    merge_logs_dir(pipeline_dir)
    merged = (pipeline_dir / "merged.jsonl").read_text().splitlines()
    bodies = [json.loads(line)["Body"] for line in merged]
    assert bodies == ["a", "b", "c", "d"]


def test_merge_tolerates_malformed_lines(tmp_path):
    pipeline_dir = tmp_path / "01HZKY"
    pipeline_dir.mkdir()
    (pipeline_dir / "maestro-1.jsonl").write_text(
        '{"Timestamp": "1", "Body": "ok"}\n'
        "garbage line\n"
        '{"Timestamp": "2", "Body": "ok2"}\n'
    )
    merge_logs_dir(pipeline_dir)
    merged = (pipeline_dir / "merged.jsonl").read_text().splitlines()
    assert len(merged) == 2  # garbage dropped


def test_merge_on_empty_dir_writes_empty_merged(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    merge_logs_dir(d)
    assert (d / "merged.jsonl").exists()
    assert (d / "merged.jsonl").read_text() == ""

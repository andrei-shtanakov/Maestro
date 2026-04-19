"""Smoke test — vendored obs.py behaves identically for trace plumbing."""

from __future__ import annotations

import importlib
import json


def test_vendored_obs_smoke(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRA_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("TRACEPARENT", raising=False)
    import maestro._vendor.obs as obs

    importlib.reload(obs)
    obs.init_logging("maestro")
    with obs.span("t.op"):
        obs.get_logger().info("hello")
    files = list(tmp_path.glob("maestro-*.jsonl"))
    assert len(files) == 1
    assert len(files[0].read_text().splitlines()) >= 3  # started + hello + ended
    for line in files[0].read_text().splitlines():
        rec = json.loads(line)
        assert rec["Resource"]["service.name"] == "maestro"

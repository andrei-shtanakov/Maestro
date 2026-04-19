"""Stand-in child for obs integration test — spawned by test_obs_integration."""

from __future__ import annotations

import sys


# Re-use vendored obs from Maestro so we don't need spec-runner installed
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from maestro._vendor import obs


def main() -> int:
    obs.init_logging("spec-runner")  # pretend to be spec-runner
    log = obs.get_logger("child")
    with obs.span("child.work", task_id="T-test"):
        log.info("child.doing.stuff", step=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

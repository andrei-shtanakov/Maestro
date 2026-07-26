import asyncio

import pytest

from maestro.execution.exec_config import DockerConfig
from maestro.execution.isolators import BareIsolator, DockerIsolator
from maestro.execution.models import PreparedRun, PreparedRunPlan


pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _fake_proc():
    # A trivial real subprocess we can pass as the `proc` argument.
    return await asyncio.create_subprocess_exec("true")


async def test_bare_after_spawn_is_noop():
    proc = await _fake_proc()
    prepared = PreparedRun(plan=PreparedRunPlan(argv=["true"]))
    await BareIsolator().after_spawn(prepared, proc)  # must not raise
    await proc.wait()


async def test_docker_after_spawn_is_noop():
    proc = await _fake_proc()
    iso = DockerIsolator(DockerConfig(image="img"))
    prepared = PreparedRun(plan=PreparedRunPlan(argv=["true"]))
    await iso.after_spawn(prepared, proc)  # must not raise
    await proc.wait()

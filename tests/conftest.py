from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from reaper_mcp.bridge import Bridge

from .fake_reaper import FakeReaper, demo_project, load_bridge


@dataclass
class Env:
    fake: FakeReaper
    lua: Any
    M: Any
    bridge: Bridge


@pytest.fixture
def env(tmp_path):
    fake = FakeReaper(tmp_path)
    demo_project(fake)
    lua, M = load_bridge(fake)
    M.setup()
    M.beat(True)
    bridge = Bridge(tmp_path / "reaper_mcp", timeout=2.0)
    return Env(fake, lua, M, bridge)


@pytest.fixture
async def running(env):
    """The env with a task that plays the role of REAPER's defer loop."""

    async def loop():
        while True:
            env.M.poll()
            await asyncio.sleep(0.002)

    task = asyncio.create_task(loop())
    yield env
    task.cancel()

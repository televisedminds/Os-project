import pytest

from opportunity_os.config import Config
from opportunity_os.db import Store
from opportunity_os.market import SimulatedMarket
from opportunity_os.pipeline import Orchestrator


@pytest.fixture
def cfg(tmp_path):
    return Config(db_path=tmp_path / "test.db", auto_cycle_seconds=0)


@pytest.fixture
def store(cfg):
    s = Store(cfg.db_path)
    yield s
    s.close()


@pytest.fixture
def world(cfg):
    return SimulatedMarket(seed=cfg.world_seed, warmup=cfg.warmup_ticks)


@pytest.fixture
def orch(cfg, store, world):
    return Orchestrator(cfg, store, world)

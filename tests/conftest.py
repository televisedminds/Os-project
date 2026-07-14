import pytest

from opportunity_os import economics
from opportunity_os.config import Config
from opportunity_os.db import Store
from opportunity_os.market import SimulatedMarket
from opportunity_os.pipeline import Orchestrator


@pytest.fixture(autouse=True)
def _isolate_fx():
    """FX lives in process-global module state (set live every cycle). Snapshot
    and restore it around each test so a live cycle in one test can't leak a
    non-default rate into another (e.g. the economics THB assertions)."""

    saved_usd_thb = economics.USD_THB
    saved_map = dict(economics.FX_TO_USD)
    yield
    economics.USD_THB = saved_usd_thb
    economics.FX_TO_USD.clear()
    economics.FX_TO_USD.update(saved_map)


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

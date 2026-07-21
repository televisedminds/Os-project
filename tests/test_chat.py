"""Phase 13: the per-opportunity execution chat — the 12 acceptance scenarios,
plus persistence, isolation, and the no-hallucination guarantee. All run without
an LLM: the substance is deterministic (matching, price decisions, links,
records, state machine)."""

import pytest

from opportunity_os import execution, matching
from opportunity_os.chat import ChatSession
from opportunity_os.config import Config
from opportunity_os.db import Store
from opportunity_os.market import SimulatedMarket
from opportunity_os.pipeline import Orchestrator


@pytest.fixture
def env(tmp_path):
    cfg = Config(db_path=tmp_path / "chat.db", auto_cycle_seconds=0)
    store = Store(cfg.db_path)
    world = SimulatedMarket(seed=cfg.world_seed, warmup=cfg.warmup_ticks)
    orch = Orchestrator(cfg, store, world)
    for _ in range(14):
        orch.run_cycle()
    flips = [o for o in store.list_opportunities(status="active", limit=100)
             if o["type"] == "product_arbitrage"]
    return orch, store, world, flips


def _cs(store, world, opp):
    return ChatSession(store, opp["id"], world=world, ai=None)


# ------- acceptance scenarios 1-12 (from the Phase 13 spec) ---------------

def test_1_2_open_and_ask_which_product(env):
    _, store, world, flips = env
    cs = _cs(store, world, flips[0])
    r = cs.send("Which exact product do I buy?")
    assert flips[0]["title"].split(" — ")[0][:10] in r["reply"]
    assert any(e["label"] in ("SAVED", "UNKNOWN") for e in r["evidence"])   # grounded


def test_3_exact_link_verified_or_honest_unavailable(env):
    _, store, world, flips = env
    cs = _cs(store, world, flips[0])
    r = cs.send("Send me the exact buying link")
    # Either the opp has a URL that gets checked, or we honestly say there's none.
    assert r["tool"] == "check_links"
    assert "link" in r["reply"].lower()
    # nothing invented — the reply never fabricates a URL that isn't on the opp
    assert "http" not in r["reply"] or (flips[0].get("route", {}).get("buy_url", "") in r["reply"])


def test_4_recalc_with_a_different_price(env):
    _, store, world, flips = env
    cs = _cs(store, world, flips[0])
    r = cs.send("Is $30 still a good buying price?")
    assert r["tool"] == "price_decision"
    assert r["data"]["recommendation"] in ("BUY", "NEGOTIATE", "WAIT", "SKIP")
    assert r["data"]["current_buy_usd"] == 30.0                  # used MY price, not the estimate


def test_5_paste_a_competing_listing_gets_product_match(env):
    _, store, world, flips = env
    cs = _cs(store, world, flips[0])
    product = flips[0]["title"].split(" — ")[0]
    r = cs.run_tool("compare_listing", {"listing_text": f"{product} brand new sealed", "price_usd": 40})
    assert r["data"]["match"]["status"] in (matching.EXACT_MATCH, matching.LIKELY_MATCH,
                                            matching.POSSIBLE_MISMATCH)


def test_6_where_to_sell_is_thailand_specific(env):
    _, store, world, flips = env
    cs = _cs(store, world, flips[0])
    r = cs.send("Where should I sell it and can a Thai resident register?")
    txt = " ".join(e["text"] for e in r["evidence"]).lower()
    assert "thai" in txt or "payout" in txt or "register" in txt


def test_7_record_purchase_advances_to_next_step(env):
    _, store, world, flips = env
    cs = _cs(store, world, flips[0])
    cs.send("I bought 2 units for $120")
    st = cs.state()
    assert st["state"] == execution.PURCHASED
    assert st["next_action"]["title"]                            # always knows what's next
    assert cs.context()["realized_pnl"]["outflow_usd"] == 120.0


def test_8_persistence_across_reopen(env):
    _, store, world, flips = env
    cs = _cs(store, world, flips[0])
    cs.send("I bought 1 for $50")
    cs.run_tool("update_checklist", {"step": "inspect photos", "done": True})
    # brand-new session object over the same store = "reopen after restart"
    cs2 = ChatSession(store, flips[0]["id"], world=world)
    assert cs2.state()["state"] == execution.PURCHASED
    assert len(cs2.history()) >= 2
    assert any(i["step"] == "inspect photos" and i["done"] for i in cs2.context()["checklist"])


def test_9_dead_link_triggers_alternative_search(env):
    _, store, world, flips = env
    cs = _cs(store, world, flips[0])
    # force a dead link by running the tool on a bad URL
    r = cs.run_tool("check_links", {"url": "http://nonexistent.invalid/itm/x"})
    assert r["ok"] is False
    # a full send() on a dead-link intent appends the alternatives nudge
    r2 = cs.send("the link is dead, I can't find the product")
    assert r2["tool"] == "check_links"


def test_10_does_not_hallucinate_unknown_facts(env):
    _, store, world, flips = env
    cs = _cs(store, world, flips[0])
    r = cs.send("What was the seller's feedback score and their phone number?")
    assert r["ok"] is False
    assert any(e["label"] == "UNKNOWN" for e in r["evidence"])
    assert "don't have" in r["reply"].lower() or "no stored" in " ".join(
        e["text"].lower() for e in r["evidence"])


def test_11_two_chats_stay_isolated(env):
    _, store, world, flips = env
    a, b = _cs(store, world, flips[0]), _cs(store, world, flips[1])
    a.send("I bought for $200")
    a.run_tool("update_checklist", {"step": "A-only step", "done": True})
    assert b.context()["realized_pnl"]["net_usd"] == 0.0         # B untouched
    assert b.state()["state"] == execution.NOT_STARTED
    assert all(i["step"] != "A-only step" for i in b.context()["checklist"])
    assert len(b.history()) == 0


def test_12_full_lifecycle_to_realized_profit(env):
    _, store, world, flips = env
    cs = _cs(store, world, flips[0])
    cs.send("I bought 1 for $40")                                # → purchased
    cs.set_state(execution.RECEIVED)
    cs.run_tool("generate_listing", {})                         # draft, never publishes
    cs.set_state(execution.LISTED)
    cs.send("I sold it for $95")                                 # → sold
    pnl = cs.context()["realized_pnl"]
    assert pnl["inflow_usd"] == 95.0 and pnl["outflow_usd"] == 40.0
    assert pnl["net_usd"] == 55.0                                # realised profit recorded
    assert cs.state()["state"] == execution.SOLD


# ------------------------- guarantees --------------------------------------

def test_chat_never_transacts():
    """No tool has an outward financial side effect — recording is not paying."""

    import opportunity_os.chat_tools as ct
    import inspect
    src = inspect.getsource(ct)
    # the module must not import payment/checkout/order libraries or call them
    for banned in ("requests.post(\"https://api.stripe", "checkout", "place_order", "submit_payment"):
        assert banned not in src


def test_generate_listing_is_a_draft_not_published(env):
    _, store, world, flips = env
    cs = _cs(store, world, flips[0])
    r = cs.run_tool("generate_listing", {})
    assert r["ok"]
    # it returns a draft for the operator; there is no publish path
    assert "draft" in r["data"] or "kit" in r["data"]


def test_unknown_opportunity_id_is_honest():
    store = Store(":memory:")
    cs = ChatSession(store, "opp_nonexistent", world=None)
    assert cs.exists() is False
    r = cs.send("which product?")
    assert not r["evidence"] or r["evidence"][0]["label"] == "UNKNOWN"
    store.close()

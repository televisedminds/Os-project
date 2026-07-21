"""Product matching + price decision + live link validation (Phase 13).

Three deterministic engines the execution chat leans on so it never has to
invent a fact:

* `match_product` — compares a candidate listing (title/description the operator
  pasted or the chat fetched) against the opportunity's product, and returns a
  status the operator can trust before spending money: EXACT_MATCH / LIKELY_MATCH
  / POSSIBLE_MISMATCH / WRONG_PRODUCT / INSUFFICIENT_INFO, with the specific
  differences spelled out. This is what stops "buy the AGS-101" turning into
  "bought the cheaper AGS-001".

* `price_decision` — given the CURRENT offered price (not the original estimate),
  recomputes the deal economics and returns BUY / NEGOTIATE / WAIT / SKIP with
  the max recommended buy price and the resulting margin.

* `check_link` — actually fetches a URL to confirm it resolves, recording when it
  was checked. It never fabricates a listing, price or seller; if it cannot
  confirm the page it says so.
"""

from __future__ import annotations

import re
import time

from . import research

# Match statuses.
EXACT_MATCH = "EXACT_MATCH"
LIKELY_MATCH = "LIKELY_MATCH"
POSSIBLE_MISMATCH = "POSSIBLE_MISMATCH"
WRONG_PRODUCT = "WRONG_PRODUCT"
INSUFFICIENT_INFO = "INSUFFICIENT_INFO"

_WORD = re.compile(r"[a-z0-9][a-z0-9'\-]*")

# Attributes worth comparing explicitly when present in either text.
_STORAGE = re.compile(r"\b(\d+)\s?(gb|tb)\b")
_COLORS = ("black", "white", "blue", "red", "green", "grey", "gray", "silver",
           "gold", "purple", "pink", "graphite", "pearl", "clear", "yellow", "orange")


def _tokens(text: str) -> list[str]:
    return _WORD.findall((text or "").lower())


def _model_tokens(toks: list[str]) -> set[str]:
    """Letters+digits tokens — the model numbers that distinguish variants."""

    return {t for t in toks if any(c.isdigit() for c in t) and any(c.isalpha() for c in t)}


def match_product(product_name: str, candidate_text: str,
                  expected_condition: str = "") -> dict:
    """Compare a candidate listing against the opportunity's product."""

    if not candidate_text or len(candidate_text.strip()) < 4:
        return {"status": INSUFFICIENT_INFO, "score": 0.0,
                "differences": ["No listing text to compare — paste the title/description."],
                "matched": [], "product": product_name}

    p_toks, c_toks = _tokens(product_name), _tokens(candidate_text)
    p_set, c_set = set(p_toks), set(c_toks)
    p_models, c_models = _model_tokens(p_toks), _model_tokens(c_toks)

    diffs: list[str] = []
    matched: list[str] = []

    # Model-number check is decisive: a model in the product but absent (and
    # contradicted) in the candidate means a different variant.
    missing_models = p_models - c_set
    conflicting = c_models - p_set
    if p_models and missing_models and conflicting:
        diffs.append(f"model number differs: expected {sorted(p_models)}, listing shows {sorted(conflicting)}")
        return {"status": WRONG_PRODUCT, "score": 0.0, "differences": diffs,
                "matched": sorted(p_models & c_models), "product": product_name}
    if p_models and missing_models:
        diffs.append(f"listing does not confirm model {sorted(missing_models)}")
    else:
        matched += [f"model {m}" for m in sorted(p_models & c_models)]

    # storage / color attribute conflicts
    p_stor = set(_STORAGE.findall(product_name.lower()))
    c_stor = set(_STORAGE.findall(candidate_text.lower()))
    if p_stor and c_stor and p_stor != c_stor:
        diffs.append(f"storage differs: expected {p_stor}, listing {c_stor}")
    p_col = {c for c in _COLORS if c in p_set}
    c_col = {c for c in _COLORS if c in c_set}
    if p_col and c_col and not (p_col & c_col):
        diffs.append(f"colour differs: expected {p_col}, listing {c_col}")

    # condition mismatch (informational)
    if expected_condition and expected_condition.lower() not in candidate_text.lower():
        diffs.append(f"condition '{expected_condition}' not stated in the listing")

    # overall token coverage of the product name by the candidate
    core = [t for t in p_toks if len(t) > 1]
    cover = sum(1 for t in core if t in c_set) / max(1, len(core))
    matched.append(f"{cover*100:.0f}% of the product name present")

    if research.looks_junk(candidate_text):
        diffs.append("listing looks like parts/junk/repro")

    # Decide.
    if cover >= 0.85 and not diffs:
        status = EXACT_MATCH
    elif cover >= 0.6 and not any("model number differs" in d for d in diffs):
        status = LIKELY_MATCH if len(diffs) <= 1 else POSSIBLE_MISMATCH
    elif cover >= 0.4:
        status = POSSIBLE_MISMATCH
    else:
        status = WRONG_PRODUCT if cover < 0.25 else POSSIBLE_MISMATCH
    return {"status": status, "score": round(cover, 2), "differences": diffs,
            "matched": matched, "product": product_name}


# --------------------------------------------------------------- price engine

def price_decision(opp: dict, current_buy_usd: float,
                   current_sell_usd: float | None = None) -> dict:
    """Re-decide at the CURRENT offered price, not the original estimate."""

    from . import economics
    route = opp.get("route", {}) or {}
    econ0 = opp.get("economics") or {}
    bv, sv = route.get("buy_venue"), route.get("sell_venue")
    item = {"category": opp.get("category", "collectibles"),
            "weight_kg": (opp.get("item") or {}).get("weight_kg", 0.5)}
    # Recover the modelled sell price if the caller didn't override it.
    sell = float(current_sell_usd if current_sell_usd is not None
                 else (econ0.get("base", {}).get("revenue_usd") or 0))
    repair = float(route.get("repair_cost", 0) or 0)
    qty = int(econ0.get("qty", 1) or 1)
    if not (bv and sv and sell > 0):
        return {"recommendation": "SKIP", "reason": "insufficient economics to recompute",
                "current_buy_usd": current_buy_usd}

    econ = economics.compute_flip(item, bv, sv, current_buy_usd, sell, qty=1,
                                  extra_cost_usd=repair)
    base_net = econ.base.net_usd
    margin = econ.base.margin_pct
    pess = econ.pessimistic.net_usd
    # Max buy price that still clears the margin floor: solve on the cost side.
    unit_cost_without_buy = econ.base.total_cost_usd - current_buy_usd
    # Target: sell - (unit_cost_without_buy + max_buy) >= min_margin * sell
    target_margin = 0.12
    max_buy = sell * (1 - target_margin) - unit_cost_without_buy

    if base_net <= 0 or margin < 8:
        rec, reason = "SKIP", f"net ${base_net:.2f} ({margin:.0f}% margin) at this price — edge gone"
    elif pess <= 0:
        rec, reason = "WAIT", f"base works (${base_net:.2f}) but pessimistic loses ${-pess:.2f} — too thin"
    elif current_buy_usd > max_buy:
        rec, reason = "NEGOTIATE", (f"profitable but above your ${max_buy:.2f} max — "
                                    f"negotiate down or it eats the margin")
    else:
        rec, reason = "BUY", f"${base_net:.2f} net ({margin:.0f}% margin), pessimistic ${pess:.2f} — clears"
    return {
        "recommendation": rec, "reason": reason,
        "current_buy_usd": round(current_buy_usd, 2),
        "max_recommended_buy_usd": round(max(0.0, max_buy), 2),
        "expected_sell_usd": round(sell, 2),
        "expected_net_usd": round(base_net, 2),
        "expected_margin_pct": round(margin, 1),
        "conservative_net_usd": round(pess, 2),
        "breakeven_sell_usd": round(econ.breakeven_revenue_usd, 2),
    }


# ----------------------------------------------------------- link validation

def check_link(url: str, client=None, timeout: float = 12.0) -> dict:
    """Confirm a URL actually resolves. Records the check time. Never invents a
    price or listing — if the page can't be confirmed, says so plainly."""

    checked_at = time.time()
    if not url or not url.startswith(("http://", "https://")):
        return {"url": url, "alive": False, "checked_at": checked_at,
                "note": "not a valid URL", "status_code": None}
    own = client is None
    try:
        import httpx
        client = client or httpx.Client(timeout=timeout, follow_redirects=True,
                                        headers={"User-Agent": "OpportunityOS-linkcheck/1.0"})
        r = client.get(url)
        alive = r.status_code < 400
        note = f"HTTP {r.status_code}" + ("" if alive else " — listing may be gone")
        return {"url": url, "alive": alive, "checked_at": checked_at,
                "status_code": r.status_code, "note": note}
    except Exception as e:  # noqa: BLE001
        return {"url": url, "alive": False, "checked_at": checked_at,
                "status_code": None, "note": f"could not reach ({str(e)[:80]})"}
    finally:
        if own and client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

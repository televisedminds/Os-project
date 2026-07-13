"""The scoring engine — evidence in, one explainable number out.

Ten factors, each 0–100 (higher is always better), blended with weights the
learning engine tunes as real outcomes arrive. `contributions` shows exactly
where every point of the overall score came from — no vibes.
"""

from __future__ import annotations

import math

from ..models import ScoreBreakdown

DEFAULT_WEIGHTS: dict[str, float] = {
    "profit_margin": 0.16,
    "demand_trend": 0.14,
    "competition": 0.12,
    "risk": 0.12,
    "difficulty": 0.10,
    "capital_required": 0.08,
    "time_required": 0.08,
    "market_size": 0.07,
    "repeatability": 0.07,
    "automation": 0.06,
}

_DIFFICULT_CATEGORIES = {"luxury_bags": -30, "watches": -15, "cameras": -12, "food": -25}
_VENTURE_DIFFICULTY = {"digital": 55, "info": 72, "local": 62, "b2b": 45}
_VENTURE_TIME = {"digital": 45, "info": 60, "local": 50, "b2b": 40}


def _clamp(x: float) -> float:
    return max(0.0, min(100.0, x))


class ScoringEngine:
    def __init__(self, weights: dict[str, float] | None = None, capital_cap_usd: float = 2000.0):
        w = dict(weights or DEFAULT_WEIGHTS)
        total = sum(w.values()) or 1.0
        self.weights = {k: v / total for k, v in w.items()}
        self.capital_cap = capital_cap_usd

    def score(self, cand: dict, automation_coverage_pct: float) -> ScoreBreakdown:
        econ = cand["economics"]
        f: dict[str, float] = {}

        f["profit_margin"] = _clamp(econ.base.margin_pct * 2.5)
        f["risk"] = _clamp(50 + econ.pessimistic.margin_pct * 2)
        f["capital_required"] = _clamp(100 - 90 * econ.capital_usd / max(1.0, self.capital_cap))
        f["automation"] = _clamp(automation_coverage_pct)

        if cand["kind"] == "flip":
            velocity, sellers = cand["velocity"], cand["sellers"]
            f["demand_trend"] = _clamp(20 * velocity)
            f["competition"] = _clamp(100 - sellers * 6)
            base_diff = 75 + _DIFFICULT_CATEGORIES.get(cand["category"], 0)
            if cand["feasibility"].requires_proxy:
                base_diff -= 8
            if cand["item"].get("weight_kg", 0) > 2:
                base_diff -= 15
            f["difficulty"] = _clamp(base_diff)
            f["time_required"] = _clamp(70 - (10 if cand["feasibility"].requires_proxy else 0))
            monthly_gross = velocity * 30 * cand["sell_usd"]
            f["market_size"] = _clamp(20 * math.log10(monthly_gross + 1))
            buy_stock = cand.get("buy_stock", cand["qty"] * 4)
            f["repeatability"] = _clamp(12 * buy_stock / max(1, cand["qty"]))
        else:
            niche = cand["niche"]
            m = niche["metrics"]
            f["demand_trend"] = _clamp(50 + m["growth_pct"])
            if niche["kind"] in ("digital", "info"):
                f["competition"] = _clamp(100 - m["solution_count"] * 18)
            else:
                ratio = m["demand_posts"] / max(1, m["providers"])
                f["competition"] = _clamp(ratio * 1.2)
            f["difficulty"] = _clamp(_VENTURE_DIFFICULTY[niche["kind"]] + (5 if niche["geo"] in ("TH", "Bangkok") else 0))
            f["time_required"] = _clamp(_VENTURE_TIME[niche["kind"]])
            f["market_size"] = _clamp(18 * math.log10(m["volume"] + 1))
            f["repeatability"] = 85.0 if niche["kind"] in ("digital", "info") else 70.0

        f = {k: round(v, 1) for k, v in f.items()}
        contributions = {k: round(self.weights[k] * f[k], 2) for k in self.weights}
        overall = round(sum(contributions.values()), 1)
        return ScoreBreakdown(factors=f, weights={k: round(v, 4) for k, v in self.weights.items()},
                              contributions=contributions, overall=overall)

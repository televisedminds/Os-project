"""The learning engine — the system that gets less wrong every week.

Every published opportunity can receive an outcome (real profit, days taken,
or a failure reason). Outcomes update three things:

* **calibration** — a global multiplier applied to consensus confidence, so a
  system that overpromises gets humbler automatically;
* **source/verifier reliability** — scanners and verifiers that fed winning
  calls gain weight in future consensus; losers lose it;
* **scoring weights** — failure reasons map to score factors (failed on
  competition → competition weighs more next time).
"""

from __future__ import annotations

from .scoring import DEFAULT_WEIGHTS

FAILURE_FACTOR = {
    "competition": "competition",
    "shipping": "difficulty",
    "demand": "demand_trend",
    "fees": "profit_margin",
    "customs": "risk",
    "price_moved": "risk",
    "capital": "capital_required",
}


class LearningEngine:
    def __init__(self, db):
        self.db = db
        self.state = db.learning_get() or {
            "weights": dict(DEFAULT_WEIGHTS),
            "source_reliability": {},
            "verifier_reliability": {},
            "calibration": 1.0,
            "outcomes": {"success": 0, "failure": 0},
            "adjustments": [],
        }

    # -------------------------------------------------------------- accessors

    @property
    def weights(self) -> dict[str, float]:
        return self.state["weights"]

    @property
    def verifier_reliability(self) -> dict[str, float]:
        return self.state["verifier_reliability"]

    @property
    def calibration(self) -> float:
        return self.state["calibration"]

    def source_reliability(self, source: str) -> float:
        return self.state["source_reliability"].get(source, 0.8)

    # ---------------------------------------------------------------- updates

    def record_outcome(self, opportunity: dict, result: str, realized_profit_usd: float | None,
                       failure_reason: str | None) -> dict:
        st = self.state
        sources = opportunity.get("sources", [])
        verifiers = [c["verifier"] for c in opportunity.get("verification", {}).get("checks", [])]
        note: str

        if result == "success":
            st["outcomes"]["success"] += 1
            st["calibration"] = min(1.10, st["calibration"] + 0.02)
            for s in sources:
                st["source_reliability"][s] = min(0.95, self.source_reliability(s) + 0.02)
            for v in verifiers:
                st["verifier_reliability"][v] = min(0.95, st["verifier_reliability"].get(v, 0.8) + 0.02)
            note = (f"Success (+${realized_profit_usd or 0:.2f}): raised calibration to "
                    f"{st['calibration']:.2f}; boosted {len(sources)} sources.")
        else:
            st["outcomes"]["failure"] += 1
            st["calibration"] = max(0.70, st["calibration"] - 0.04)
            for s in sources:
                st["source_reliability"][s] = max(0.30, self.source_reliability(s) - 0.05)
            factor = FAILURE_FACTOR.get(failure_reason or "", None)
            if factor:
                st["weights"][factor] = st["weights"].get(factor, 0.05) + 0.015
                total = sum(st["weights"].values())
                st["weights"] = {k: round(v / total, 4) for k, v in st["weights"].items()}
            note = (f"Failure ({failure_reason or 'unspecified'}): lowered calibration to "
                    f"{st['calibration']:.2f}"
                    + (f"; factor '{factor}' now weighs {st['weights'][factor]:.3f}." if factor else "."))

        st["adjustments"].append({"result": result, "note": note})
        st["adjustments"] = st["adjustments"][-50:]
        self.db.learning_set(st)
        return {"note": note, "calibration": st["calibration"], "weights": st["weights"]}

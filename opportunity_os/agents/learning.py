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
            "category_affinity": {},
            "adjustments": [],
        }
        self.state.setdefault("category_affinity", {})

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

    def category_affinity(self, category: str) -> int:
        """-3..+3: your realized track record in this category."""

        return int(self.state.get("category_affinity", {}).get(category, 0))

    # ---------------------------------------------------------------- updates

    def record_outcome(self, opportunity: dict, result: str, realized_profit_usd: float | None,
                       failure_reason: str | None) -> dict:
        st = self.state
        sources = opportunity.get("sources", [])
        verifiers = [c["verifier"] for c in opportunity.get("verification", {}).get("checks", [])]
        note: str

        cat = opportunity.get("category")
        if cat:
            aff = st.setdefault("category_affinity", {})
            aff[cat] = max(-3, min(3, aff.get(cat, 0) + (1 if result == "success" else -1)))
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

    # ------------------------------------------- realized-outcome learning (v1.14)

    def record_realized_outcome(self, provenance: dict, signal: dict,
                                opp_id: str | None = None) -> dict:
        """Recalibrate from a REALIZED outcome only. Learning inputs come from the
        frozen provenance snapshot (the sources / verifiers / category that made
        the original call) and the realized-cash ``signal`` — never from a live
        prediction. Captures a structured before→after audit and returns the diff.

        Honesty guarantees enforced here:
        * ``assert_realized_basis`` refuses any signal not grounded in realized
          cash, so a prediction can never train predictions.
        * an *abandoned* outcome is an honest walk-away: it never moves calibration
          or source reliability (no cash test of the prediction happened), only the
          category track record and what-makes-us-walk-away factor.
        """

        from ..outcomes import assert_realized_basis
        assert_realized_basis(signal)                 # refuse non-realized signals
        st = self.state
        result = signal["result"]
        reason = signal.get("reason")
        realized = signal.get("realized_profit_usd")
        sources = list(provenance.get("sources", []) or [])
        verifiers = list(provenance.get("verifiers", []) or [])
        cat = provenance.get("category")

        before = self._snapshot(sources, verifiers, cat)
        factor = None

        if result == "success":
            st["outcomes"]["success"] += 1
            st["calibration"] = round(min(1.10, st["calibration"] + 0.02), 4)
            for s in sources:
                st["source_reliability"][s] = round(min(0.95, self.source_reliability(s) + 0.02), 4)
            for v in verifiers:
                st["verifier_reliability"][v] = round(min(0.95, st["verifier_reliability"].get(v, 0.8) + 0.02), 4)
            self._bump_affinity(cat, +1)
        elif result == "abandoned":
            self._bump_affinity(cat, -1)              # no cash test → calibration/sources untouched
            factor = self._bump_factor(reason, 0.008)
        else:  # failure
            st["outcomes"]["failure"] += 1
            st["calibration"] = round(max(0.70, st["calibration"] - 0.04), 4)
            for s in sources:
                st["source_reliability"][s] = round(max(0.30, self.source_reliability(s) - 0.05), 4)
            self._bump_affinity(cat, -1)
            factor = self._bump_factor(reason, 0.015)

        after = self._snapshot(sources, verifiers, cat)
        changes = self._diff(before, after)
        note = self._realized_note(result, realized, reason, factor, st["calibration"])

        st["adjustments"].append({"result": result, "note": note})
        st["adjustments"] = st["adjustments"][-50:]
        self.db.learning_set(st)
        self.db.add_weight_audit(opp_id, "realized_outcome", result, before, after, changes)
        return {"result": result, "note": note, "before": before, "after": after,
                "changes": changes, "calibration": st["calibration"]}

    # ---- audit helpers ----

    def _snapshot(self, sources, verifiers, cat) -> dict:
        st = self.state
        return {
            "calibration": st["calibration"],
            "weights": dict(st["weights"]),
            "source_reliability": {s: self.source_reliability(s) for s in sources},
            "verifier_reliability": {v: st["verifier_reliability"].get(v, 0.8) for v in verifiers},
            "category_affinity": {cat: self.category_affinity(cat)} if cat else {},
        }

    def _bump_affinity(self, cat, delta) -> None:
        if not cat:
            return
        aff = self.state.setdefault("category_affinity", {})
        aff[cat] = max(-3, min(3, aff.get(cat, 0) + delta))

    def _bump_factor(self, reason, amount):
        factor = FAILURE_FACTOR.get(reason or "", None)
        if factor:
            w = self.state["weights"]
            w[factor] = w.get(factor, 0.05) + amount
            total = sum(w.values())
            self.state["weights"] = {k: round(v / total, 4) for k, v in w.items()}
        return factor

    @staticmethod
    def _diff(before: dict, after: dict) -> dict:
        changes: dict = {}
        if before["calibration"] != after["calibration"]:
            changes["calibration"] = [before["calibration"], after["calibration"]]
        wch = {k: [before["weights"].get(k), after["weights"].get(k)]
               for k in after["weights"] if before["weights"].get(k) != after["weights"].get(k)}
        if wch:
            changes["weights"] = wch
        for key in ("source_reliability", "verifier_reliability", "category_affinity"):
            d = {k: [before[key].get(k), after[key].get(k)]
                 for k in after[key] if before[key].get(k) != after[key].get(k)}
            if d:
                changes[key] = d
        return changes

    @staticmethod
    def _realized_note(result, realized, reason, factor, calibration) -> str:
        if result == "success":
            return (f"Success (realized ${realized or 0:.2f}): calibration → {calibration:.2f}; "
                    "contributing sources/verifiers boosted.")
        if result == "abandoned":
            return (f"Abandoned ({reason or 'unspecified'}): honest walk-away, no cash test — "
                    f"calibration held at {calibration:.2f}; "
                    + (f"factor '{factor}' weighed up." if factor else "category track record dipped."))
        return (f"Failure ({reason or 'unspecified'}, realized ${realized or 0:.2f}): "
                f"calibration → {calibration:.2f}"
                + (f"; factor '{factor}' weighed up." if factor else "."))

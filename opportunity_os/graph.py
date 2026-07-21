"""The product & opportunity knowledge graph (Phase 5).

Data sources collect evidence; this graph is the shared memory that lets one
discovery imply the next. Entities (products, brands, models, accessories,
parts, niches, sellers, theses, outcomes) are normalized to nodes; relationships
(same/similar product, compatible-with, replacement-for, bundle-component,
frequently-discussed-with, previously-profitable, previously-rejected) are
typed edges with accumulating weight.

Two disciplines keep it honest and cheap:

* **Bounded traversal.** `related_hypotheses` walks a small, capped neighborhood
  and scores each expansion path by expected value per additional API request —
  it NEVER fans out unboundedly. Compute is free; API requests are not.
* **Memory of outcomes.** A verified opportunity marks its product
  `previously_profitable`; a rejected thesis marks it `previously_rejected`.
  Future expansions are steered toward what has paid and away from what hasn't.

The graph does not itself make network calls. It proposes *which* entities are
worth an investigation; the generators and the tiered scanner decide whether to
spend a request on them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Node types — the vocabulary of things the platform reasons about.
N_PRODUCT = "product"
N_BRAND = "brand"
N_MODEL = "model"
N_ACCESSORY = "accessory"
N_PART = "part"
N_NICHE = "niche"
N_SELLER = "seller"
N_THESIS = "thesis"
N_OUTCOME = "outcome"
N_KEYWORD = "keyword"

# Edge (relationship) types.
E_SAME = "same_product"
E_SIMILAR = "similar_product"
E_COMPATIBLE = "compatible_with"
E_REPLACEMENT = "replacement_for"
E_BUNDLE = "bundle_component"
E_CO_LISTED = "co_listed"
E_DISCUSSED_WITH = "discussed_with"
E_VARIANT = "regional_or_variant_of"
E_PROFITABLE = "previously_profitable"
E_REJECTED = "previously_rejected"

# How much each relationship is worth following, per unit edge weight, when
# estimating the value of spending one more API request on a neighbor. Tuned so
# a proven-profitable neighbor outranks a merely co-listed phrase.
_EDGE_PRIOR = {
    E_PROFITABLE: 3.0,
    E_VARIANT: 1.4,
    E_COMPATIBLE: 1.2,
    E_BUNDLE: 1.1,
    E_SIMILAR: 1.0,
    E_CO_LISTED: 0.7,
    E_DISCUSSED_WITH: 0.6,
    E_REPLACEMENT: 1.0,
    E_REJECTED: -2.5,          # steer AWAY from things that already failed
}


@dataclass
class Hypothesis:
    """A related entity worth investigating, reached by walking the graph."""

    node_id: str
    name: str
    ntype: str
    relation: str
    hops: int
    ev_score: float
    seed_id: str
    reason: str = ""

    def dict(self) -> dict:
        return {"node_id": self.node_id, "name": self.name, "ntype": self.ntype,
                "relation": self.relation, "hops": self.hops,
                "ev_score": round(self.ev_score, 3), "seed_id": self.seed_id,
                "reason": self.reason}


class KnowledgeGraph:
    """A thin, typed view over the store's graph_nodes / graph_edges tables."""

    def __init__(self, store):
        self.store = store

    # ---- writes -----------------------------------------------------------

    def add_node(self, node_id: str, ntype: str, name: str, attrs: dict | None = None) -> None:
        self.store.upsert_graph_node(node_id, ntype, name, attrs)

    def add_edge(self, src: str, dst: str, kind: str, weight: float = 1.0, tick: int = 0) -> None:
        self.store.add_graph_edges(src, [{"dst": dst, "kind": kind, "weight": weight}], tick)

    def link(self, src: str, dst: str, kind: str, *, src_type: str, dst_type: str,
             src_name: str = "", dst_name: str = "", weight: float = 1.0, tick: int = 0) -> None:
        """Ensure both endpoints exist as nodes, then connect them."""

        self.add_node(src, src_type, src_name or src)
        self.add_node(dst, dst_type, dst_name or dst)
        self.add_edge(src, dst, kind, weight, tick)

    def mark_outcome(self, product_id: str, name: str, profitable: bool, tick: int = 0) -> None:
        """Record realized experience so future expansions learn from it."""

        self.add_node(product_id, N_PRODUCT, name)
        outcome_id = f"outcome:{product_id}"
        self.add_node(outcome_id, N_OUTCOME, ("profitable" if profitable else "rejected"))
        self.add_edge(product_id, outcome_id,
                      E_PROFITABLE if profitable else E_REJECTED, 1.0, tick)

    # ---- reads / traversal ------------------------------------------------

    def neighbors(self, node_id: str, kinds: list[str] | None = None, limit: int = 20) -> list[dict]:
        return self.store.graph_edges_from(node_id, kinds, limit)

    def experience(self, node_id: str) -> str | None:
        """'profitable' / 'rejected' / None — the product's own track record."""

        edges = {e["kind"] for e in self.store.graph_edges_from(node_id, [E_PROFITABLE, E_REJECTED], 5)}
        if E_PROFITABLE in edges:
            return "profitable"
        if E_REJECTED in edges:
            return "rejected"
        return None

    def related_hypotheses(self, seed_id: str, *, budget: int = 8, max_hops: int = 2,
                           exclude: set[str] | None = None) -> list[Hypothesis]:
        """Bounded, EV-ranked expansion from a seed entity.

        Walks at most `max_hops` hops over a capped frontier and returns up to
        `budget` neighbor entities, each scored by expected value per API
        request: edge relationship prior × edge weight, decayed per hop, minus a
        penalty for anything already marked previously_rejected. This is what
        turns 'a profitable Game Boy' into 'check the AGS-101 variant, the link
        cable accessory, the replacement screen' — without unbounded fan-out.
        """

        exclude = set(exclude or ())
        exclude.add(seed_id)
        best: dict[str, Hypothesis] = {}
        # (node_id, hops, inherited_score, relation_at_first_hop)
        frontier: list[tuple[str, int, float, str]] = [(seed_id, 0, 1.0, "seed")]
        visited: set[str] = set()
        expansions = 0
        max_expansions = budget * 4          # hard cap on graph work per call

        while frontier and expansions < max_expansions:
            node_id, hops, score_in, first_rel = frontier.pop(0)
            if node_id in visited or hops >= max_hops:
                continue
            visited.add(node_id)
            for e in self.store.graph_edges_from(node_id, limit=12):
                expansions += 1
                dst = e["dst"]
                if dst in exclude or dst == seed_id:
                    continue
                prior = _EDGE_PRIOR.get(e["kind"], 0.5)
                decay = 0.6 ** hops
                ev = score_in * prior * (1.0 + min(3.0, e["weight"]) / 4.0) * decay
                rel = first_rel if hops > 0 else e["kind"]
                node = self.store.get_graph_node(dst)
                ntype = (node or {}).get("ntype", "unknown")
                # Down-rank anything with a rejected track record.
                if self.experience(dst) == "rejected":
                    ev -= 2.0
                # Outcome/keyword nodes are bookkeeping — they influence scoring
                # (via previously_profitable edges) but are not themselves
                # investigable hypotheses, so keep walking past them without
                # emitting them.
                investigable = ntype not in (N_OUTCOME, N_KEYWORD)
                if investigable and (dst not in best or ev > best[dst].ev_score):
                    best[dst] = Hypothesis(
                        node_id=dst, name=(node or {}).get("name", dst), ntype=ntype,
                        relation=e["kind"], hops=hops + 1, ev_score=ev, seed_id=seed_id,
                        reason=f"{e['kind'].replace('_', ' ')} of {node_id} (weight {e['weight']:.0f})")
                if hops + 1 < max_hops:
                    frontier.append((dst, hops + 1, ev, rel))

        ranked = sorted(best.values(), key=lambda h: h.ev_score, reverse=True)
        return [h for h in ranked if h.ev_score > 0][:budget]

    def stats(self) -> dict:
        return {"nodes": self.store.graph_node_count(),
                "edges": self.store.graph_edge_count()}

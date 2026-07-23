"""Opportunity generators — the layer that turns evidence into business theses.

Phase 3's core idea: a **data source** collects evidence (eBay listings, Reddit
demand, FX, ...); an **opportunity generator** combines evidence into a thesis
(a cross-market flip, a refurbishment play, a micro-SaaS). They are different
things and must not be conflated — adding a source must not require touching
generators, and a generator must not be hard-wired to one source.

This module is the registry + interface. Each generator:

* declares an id, a name, and the opportunity `types` it can emit;
* implements `generate(ctx) -> list[candidate dict]`, reading only from the
  shared `GenContext` (data source, anomalies, knowledge graph, store, config);
* is judged downstream by the SAME council + economics gate as everything else
  — a generator proposes, the pipeline disposes.

Generators register with `@generator`. `build_generators()` returns the default
set plus any drop-in plugins, exactly like discovery sources. Not every
generator uses eBay; several never touch a marketplace at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Anomaly, AnomalyKind

# ---- registry -------------------------------------------------------------

GENERATORS: list[type] = []


def generator(cls):
    """Register an opportunity generator class."""

    GENERATORS.append(cls)
    return cls


@dataclass
class GenContext:
    """Everything a generator may read — and nothing it may mutate directly.

    Generators combine evidence; they do not scan. The data source, the
    anomalies detected this cycle, the knowledge graph and the store are all
    read-only inputs to thesis construction."""

    ds: object                                  # the data source (world)
    anomalies: list                             # all anomalies this cycle
    by_entity: dict                             # entity_id -> [Anomaly]
    niche_ids: set                              # ids that are niches (ventures)
    store: object
    cfg: object
    investigator: object                        # reuse existing candidate builders
    graph: object = None                        # KnowledgeGraph (optional)
    # Milestone 2: venture niches to evaluate this cycle, deduped across the
    # anomaly and steady-state entry paths. nid -> {anomalies, entry_path,
    # steady_evidence}. None ⇒ fall back to the anomaly-only by_entity.
    venture_entries: dict = None


class OpportunityGenerator:
    id: str = "base"
    name: str = "Base generator"
    types: tuple[str, ...] = ()
    uses_marketplace: bool = False              # honest tag for the diagnostics

    def generate(self, ctx: GenContext) -> list[dict]:   # pragma: no cover - overridden
        return []


# ---- flip family (marketplace product arbitrage) --------------------------

@generator
class CrossMarketFlipGenerator(OpportunityGenerator):
    """Same product, cheaper on one accessible market than another — buy there,
    sell here. Not eBay-specific: it uses whichever venues have observed
    listings and are reachable from Thailand (Shopee, Lazada, Yahoo JP, ...)."""

    id, name = "cross_market_flip", "Cross-market arbitrage"
    types = ("product_arbitrage",)
    uses_marketplace = True

    def generate(self, ctx: GenContext) -> list[dict]:
        out = []
        for pid, group in ctx.by_entity.items():
            if pid in ctx.niche_ids:
                continue
            cand = ctx.investigator._flip_candidate(ctx.ds, pid, group)
            if cand:
                out.append(cand)
        return out


@generator
class DislocationGenerator(OpportunityGenerator):
    """An individual listing priced far below its market's own clearing value —
    buy that exact unit, resell at fair. One response yields many candidates."""

    id, name = "dislocation", "Listing dislocation"
    types = ("product_arbitrage",)
    uses_marketplace = True

    def generate(self, ctx: GenContext) -> list[dict]:
        out = []
        for pid, group in ctx.by_entity.items():
            if pid in ctx.niche_ids:
                continue
            out += ctx.investigator._dislocation_candidates(ctx.ds, pid, group)
        return out


# ---- venture family (non-flip: build/serve, not buy/resell) ---------------

class _VentureGenerator(OpportunityGenerator):
    """Shared base for the build-it opportunity types. Each subclass claims one
    niche kind so the type funnel shows real diversity instead of one bucket.

    Milestone 2: evaluates the deduplicated venture-entry set (anomaly path ∪
    steady-state path), not just the anomaly-driven `by_entity`, so a
    well-observed steady niche is no longer silently skipped. The entry path is
    stamped on each candidate for the funnel."""

    niche_kind: str = ""
    uses_marketplace = False

    def _claims(self, ctx: GenContext, niche: dict) -> bool:
        """Whether this generator owns the niche. Overridden by DigitalProduct to
        hand micro-SaaS-grade niches to that generator instead."""
        return True

    def _entries(self, ctx: GenContext) -> dict:
        entries = getattr(ctx, "venture_entries", None)
        if entries is not None:
            return entries
        # Backward-compatible: anomaly-only when no steady set was threaded.
        return {nid: {"anomalies": g, "entry_path": "anomaly", "steady_evidence": None}
                for nid, g in ctx.by_entity.items()}

    def generate(self, ctx: GenContext) -> list[dict]:
        out = []
        niches = {n["id"]: n for n in ctx.ds.niches()}
        for nid, entry in self._entries(ctx).items():
            niche = niches.get(nid)
            if not niche or niche["kind"] != self.niche_kind or not self._claims(ctx, niche):
                continue
            cand = ctx.investigator._venture_candidate(ctx.ds, nid, entry["anomalies"])
            if cand:
                cand["entry_path"] = entry["entry_path"]
                cand["steady_evidence"] = entry["steady_evidence"]
                out.append(cand)
        return out


@generator
class DigitalProductGenerator(_VentureGenerator):
    id, name = "digital_product", "Digital product"
    types = ("digital_product",)
    niche_kind = "digital"

    def _claims(self, ctx: GenContext, niche: dict) -> bool:
        # A digital niche strong enough to be a micro-SaaS is claimed by that
        # generator instead, so the same demand isn't published twice.
        from .generators_extra import qualifies_micro_saas
        return not qualifies_micro_saas(niche)


@generator
class InfoProductGenerator(_VentureGenerator):
    id, name = "info_product", "Info product"
    types = ("info_product",)
    niche_kind = "info"


@generator
class LocalServiceGenerator(_VentureGenerator):
    id, name = "local_service", "Thailand local-service gap"
    types = ("local_service",)
    niche_kind = "local"


@generator
class B2BServiceGenerator(_VentureGenerator):
    id, name = "b2b_service", "B2B service gap"
    types = ("b2b_service",)
    niche_kind = "b2b"


# ---- build ----------------------------------------------------------------

def build_generators(cfg=None) -> list[OpportunityGenerator]:
    """Default generator set + drop-in plugins. Import side-effects register
    the built-ins below (bundle/repair, micro-SaaS) before we instantiate."""

    from . import generators_extra   # noqa: F401 - registers micro-SaaS + refurbishment
    from . import generators_trade   # noqa: F401 - registers import/export + wholesale
    from . import generators_market  # noqa: F401 - registers lead-gen + seasonal
    out: list[OpportunityGenerator] = []
    seen = set()
    for cls in GENERATORS:
        if cls.id in seen:
            continue
        seen.add(cls.id)
        try:
            out.append(cls())
        except Exception:  # noqa: BLE001 - a broken generator never blocks the fleet
            pass
    return out

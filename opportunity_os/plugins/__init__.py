"""Plugin architecture — add a scanner by dropping in one file.

Any `.py` file in this package is auto-imported at startup. Register with the
decorators below and the core picks the class up — no edits to discovery.py
or adapters.py, which is what lets the source count grow from 5 to 50 to 500
without touching the pipeline:

    # opportunity_os/plugins/rakuten.py
    from opportunity_os.discovery import Candidate, DiscoverySource
    from opportunity_os.plugins import discovery_source

    @discovery_source
    class RakutenDiscovery(DiscoverySource):
        id, name = "rakuten", "Rakuten Ichiba (JP)"
        def discover(self):  ...return [Candidate(...)]
        def check(self):     ...return True, "ok"

Venue adapters (live price sources) register the same way with
`@venue_adapter("venue_id")` and must implement `product_snapshot(query)`.
Every plugin inherits the same contract as the built-ins: degrade gracefully
(return None / [] and set last_error), never crash a cycle. See
`_example_rakuten.py.template` for a complete skeleton.
"""

from __future__ import annotations

import importlib
import pkgutil

DISCOVERY_SOURCES: list[type] = []
VENUE_ADAPTERS: dict[str, type] = {}
_loaded = False


def discovery_source(cls):
    """Class decorator: register a DiscoverySource subclass."""

    if cls not in DISCOVERY_SOURCES:
        DISCOVERY_SOURCES.append(cls)
    return cls


def venue_adapter(venue_id: str):
    """Class decorator: register a live venue adapter under a venue id."""

    def deco(cls):
        VENUE_ADAPTERS[venue_id] = cls
        return cls
    return deco


def load_plugins() -> list[str]:
    """Import every module in this package once. A broken plugin is skipped
    (and reported by name) instead of taking the platform down."""

    global _loaded
    errors: list[str] = []
    if _loaded:
        return errors
    _loaded = True
    for m in pkgutil.iter_modules(__path__):
        if m.name.startswith("_"):
            continue
        try:
            importlib.import_module(f"{__name__}.{m.name}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"plugin '{m.name}' failed to load: {e}")
    return errors

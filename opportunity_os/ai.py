"""The AI brain — a Claude-powered judgment layer over discovery.

The keyword heuristics in `discovery.py` are deliberately crude: they match
words, not meaning ("sold out tennis tickets" and "sold out booster boxes"
look the same to them). When an Anthropic API key is configured, this module
replaces that judgment step: every discovery sweep's candidates are sent to
Claude in one batched call, which keeps/drops them, fixes categories, adjusts
scores, and explains why — so the fleet spends its observation capacity on
things that are actually buyable, sellable, or buildable from Thailand.

Honesty rules, same as everywhere else in this codebase:

* the AI **filters and annotates** candidates — it never invents one, and
  everything it keeps still has to survive observation, verification and the
  economics gates before a user sees it;
* every failure (no key, network, refusal, malformed output) degrades to the
  keyword heuristics instead of crashing a cycle, and is visible in
  `check()`/`last_error`, live-check and /api/discovery.

Cost: one call per discovery sweep (default every 6th cycle ≈ 8/day), batched,
~2-4k tokens each. On the default claude-opus-4-8 that is a few cents per day;
set OOS_AI_MODEL=claude-haiku-4-5 to run it cheaper at some judgment quality
cost — your call, both work.
"""

from __future__ import annotations

import json

# The JSON schema Claude's output is constrained to (structured outputs) —
# guaranteed-parseable verdicts, one per candidate id.
VERDICTS_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "keep": {"type": "boolean"},
                    "score_adj": {"type": "number",
                                  "description": "-1.0 to +1.0 adjustment to the discovery score"},
                    "category": {"type": "string",
                                 "description": "corrected product category, or empty to keep"},
                    "niche_kind": {"type": "string",
                                   "enum": ["digital", "info", "local", "b2b", ""]},
                    "reason": {"type": "string", "description": "one short sentence"},
                },
                "required": ["id", "keep", "score_adj", "category", "niche_kind", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You are the discovery filter for an opportunity-intelligence platform whose \
operator is a solo reseller/builder based in Thailand with limited capital \
(hundreds to low thousands of USD). Upstream keyword scanners found candidate \
opportunities on Google Trends, Reddit and eBay; many are noise (news, sports, \
celebrities, memes) or not actionable from Thailand.

For each candidate, judge: could a Thailand-based individual plausibly make \
money from this within weeks — by flipping a product (buy low somewhere \
reachable from Thailand, sell higher on eBay US / Shopee TH / Etsy), or by \
building a small digital tool, info product, local service, or B2B service?

Rules:
- keep=false for news/sports/celebrity/politics/memes with nothing to buy, \
sell, or build; and for anything illegal, counterfeit, or requiring licenses \
a solo operator won't have.
- keep=true for tradeable products (collectibles, electronics, fashion, toys, \
cameras, watches) and for real demand gaps a small builder could serve.
- score_adj between -1.0 and +1.0: positive for strong commerce signals \
(scarcity, resale spread, surging demand, low competition), negative for weak \
or crowded ones.
- category: correct it when wrong (trading_cards, sneakers, lego, gaming, \
cameras, watches, toys, electronics, apparel, books, food, handmade, \
luxury_bags, collectibles); empty string to keep the current value.
- niche_kind: for niche candidates pick digital/info/local/b2b; empty string \
to keep the current value.
- reason: one short sentence a non-expert can understand.
Return a verdict for every candidate id you were given."""


class AIClassifier:
    """classify_hook implementation backed by the Claude API."""

    MAX_CANDIDATES = 40          # bound tokens/cost per sweep

    def __init__(self, cfg, client=None):
        self.cfg = cfg
        self._client = client    # injectable for tests; else built lazily
        self.last_error = ""
        self.last_summary = ""

    def configured(self) -> bool:
        return bool(self.cfg.anthropic_api_key) and self.cfg.ai_enabled

    def _get_client(self):
        if self._client is None:
            import anthropic
            # max_retries=0: a research cycle must never stall on backoff —
            # a failed sweep degrades to keyword filtering and the next
            # sweep (~3h later) tries again.
            self._client = anthropic.Anthropic(api_key=self.cfg.anthropic_api_key,
                                               max_retries=0,
                                               timeout=self.cfg.http_timeout * 3)
        return self._client

    # ------------------------------------------------------------- classify

    def classify(self, candidates: list) -> list:
        """Batched keep/drop/rescore. On ANY failure returns the input as-is."""

        if not self.configured() or not candidates:
            return candidates
        ranked = sorted(candidates, key=lambda c: c.score, reverse=True)
        batch, rest = ranked[:self.MAX_CANDIDATES], ranked[self.MAX_CANDIDATES:]
        payload = [{"id": c.id, "kind": c.kind, "name": c.name, "source": c.source,
                    "score": c.score, "category": c.category, "niche_kind": c.niche_kind,
                    "signal": c.reason} for c in batch]
        try:
            import anthropic
            client = self._get_client()
            response = client.messages.create(
                model=self.cfg.ai_model,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                output_config={"format": {"type": "json_schema", "schema": VERDICTS_SCHEMA}},
                messages=[{"role": "user",
                           "content": "Candidates to judge:\n" + json.dumps(payload, ensure_ascii=False)}],
            )
            if response.stop_reason == "refusal":
                return self._fail("model declined the classification request", candidates)
            text = next((b.text for b in response.content if b.type == "text"), "")
            verdicts = {v["id"]: v for v in json.loads(text).get("verdicts", [])}
        except ImportError:
            return self._fail("anthropic SDK not installed (pip install anthropic)", candidates)
        except anthropic.APIStatusError as e:
            return self._fail(f"Claude API error {e.status_code}: {getattr(e, 'message', e)}", candidates)
        except anthropic.APIConnectionError as e:
            return self._fail(f"Claude API unreachable: {e}", candidates)
        except Exception as e:  # noqa: BLE001 - malformed output etc.; never crash a cycle
            return self._fail(f"AI classify failed: {e}", candidates)

        kept: list = []
        dropped = 0
        for c in batch:
            v = verdicts.get(c.id)
            if v is None:                      # no verdict → fail open, keep as-is
                kept.append(c)
                continue
            if not v.get("keep", True):
                dropped += 1
                continue
            adj = max(-1.0, min(1.0, float(v.get("score_adj", 0.0))))
            c.score = round(max(0.0, c.score + adj), 3)
            if v.get("category"):
                c.category = v["category"]
            if v.get("niche_kind"):
                c.niche_kind = v["niche_kind"]
            if v.get("reason"):
                c.reason = f"{c.reason} · AI: {v['reason']}".strip(" ·")
            kept.append(c)
        self.last_error = ""
        self.last_summary = f"judged {len(batch)}: kept {len(kept)}, dropped {dropped}"
        return kept + rest

    def _fail(self, msg: str, candidates: list) -> list:
        self.last_error = msg[:300]
        return candidates

    # ----------------------------------------------------------------- check

    def check(self) -> tuple[bool, str]:
        if not self.cfg.anthropic_api_key:
            return False, ("optional — set ANTHROPIC_API_KEY (console.anthropic.com) to add "
                           "AI judgment to discovery; keyword filtering works without it")
        if not self.cfg.ai_enabled:
            return False, "disabled via OOS_AI=0"
        try:
            client = self._get_client()
            response = client.messages.create(
                model=self.cfg.ai_model, max_tokens=32,
                messages=[{"role": "user", "content": "Reply with the single word: ok"}])
            _ = response.content
            return True, f"Claude OK ({self.cfg.ai_model}); judges every discovery sweep"
        except Exception as e:  # noqa: BLE001
            return False, f"key set but call failed: {str(e)[:200]}"

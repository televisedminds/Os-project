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

# ---------------------------------------------------------------------------
# Selling kits — the execution layer. A verified opportunity is only worth
# money once something is LISTED or LAUNCHED; these schemas turn one into
# ready-to-paste selling content (bilingual TH/EN, because the operator sells
# on Thai platforms and on eBay US).
# ---------------------------------------------------------------------------

FLIP_KIT_SCHEMA = {
    "type": "object",
    "properties": {
        "listing_title_en": {"type": "string", "description": "eBay-style SEO title, under 80 chars"},
        "listing_title_th": {"type": "string", "description": "Shopee/TikTok Shop TH title in Thai"},
        "bullets_en": {"type": "array", "items": {"type": "string"},
                       "description": "4-6 selling points for the English listing"},
        "description_en": {"type": "string", "description": "full listing description, plain text"},
        "description_th": {"type": "string", "description": "Thai listing description"},
        "seller_message_th": {"type": "string",
                              "description": "polite Thai message to send the source seller "
                                             "(availability, condition, best price, shipping)"},
        "hashtags": {"type": "array", "items": {"type": "string"},
                     "description": "TikTok/Shopee hashtags, mixed TH/EN, no # symbol"},
        "pricing_strategy": {"type": "string",
                             "description": "1-2 sentences: list price, floor, when to reprice"},
    },
    "required": ["listing_title_en", "listing_title_th", "bullets_en", "description_en",
                 "description_th", "seller_message_th", "hashtags", "pricing_strategy"],
    "additionalProperties": False,
}

VENTURE_KIT_SCHEMA = {
    "type": "object",
    "properties": {
        "product_name": {"type": "string"},
        "one_liner_en": {"type": "string"},
        "one_liner_th": {"type": "string"},
        "outline": {"type": "array", "items": {"type": "string"},
                    "description": "sections/features of the smallest sellable first version"},
        "landing_headline_en": {"type": "string"},
        "landing_headline_th": {"type": "string"},
        "landing_copy_en": {"type": "string", "description": "short landing-page body"},
        "landing_copy_th": {"type": "string"},
        "first_posts": {"type": "array",
                        "items": {"type": "object",
                                  "properties": {"platform": {"type": "string"},
                                                 "text": {"type": "string"}},
                                  "required": ["platform", "text"],
                                  "additionalProperties": False},
                        "description": "3 launch posts (Facebook group / TikTok / Reddit or X), "
                                       "in the language of that audience"},
        "pricing_advice": {"type": "string"},
        "first_week_plan": {"type": "array", "items": {"type": "string"},
                            "description": "5 concrete steps, day by day"},
    },
    "required": ["product_name", "one_liner_en", "one_liner_th", "outline",
                 "landing_headline_en", "landing_headline_th", "landing_copy_en",
                 "landing_copy_th", "first_posts", "pricing_advice", "first_week_plan"],
    "additionalProperties": False,
}

KIT_SYSTEM = """\
You write ready-to-use selling content for a solo operator based in Thailand
with a small budget. They sell physical flips on eBay US (English) and
Shopee/TikTok Shop Thailand (Thai), and launch small digital/info products and
services for Thai and global audiences.

Rules:
- Write like a real seller, not a brochure: concrete, specific, trustworthy.
  Mention condition, what's included, shipping expectations where relevant.
- Thai text must be natural Thai an ordinary buyer trusts — not translated-
  sounding. Keep prices in the message OUT unless given; never invent specs,
  authenticity claims, or certifications that were not provided.
- eBay titles: front-load searchable keywords, no emoji, under 80 characters.
- Thai listings: friendly tone, emoji acceptable, state ของแท้/สภาพ honestly
  only from the data given.
- The seller_message_th is the operator asking a SOURCE (Facebook/AliExpress/
  proxy) about buying: availability, real condition, best price, shipping to
  their location. Polite, short, ends with a clear question.
- For ventures: the outline is the SMALLEST version sellable within 1-2 weeks
  of evening work. The first_week_plan must be doable by one beginner alone.
Ground everything in the JSON the user sends. Fill every field."""


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

    # ------------------------------------------------------------ selling kit

    def generate_kit(self, o: dict) -> tuple[dict | None, str]:
        """Turn one verified opportunity into ready-to-paste selling content.
        Returns (kit, "") or (None, human-readable error). Never raises."""

        if not self.configured():
            return None, ("ANTHROPIC_API_KEY not set — add it to .env (console.anthropic.com) "
                          "to generate selling kits")
        is_flip = o.get("type") == "product_arbitrage"
        e = o.get("economics", {})
        if is_flip:
            schema = FLIP_KIT_SCHEMA
            pb = o.get("playbook") or {}
            payload = {
                "task": "flip_listing_kit",
                "product": o.get("title"), "category": o.get("category"),
                "buy_venue": (o.get("route") or {}).get("buy_venue"),
                "sell_venue": (o.get("route") or {}).get("sell_venue"),
                "buy_price_usd": o.get("economics", {}).get("base", {}).get("lines", [{}])[0].get("amount_usd"),
                "list_price_usd": (pb.get("listing") or {}).get("price_usd") or e.get("base", {}).get("revenue_usd"),
                "qty": e.get("qty"), "margin_pct": e.get("base", {}).get("margin_pct"),
                "window_days": o.get("window_days"),
                "listing_hints": pb.get("listing"),
                "why_it_sells": [w.get("finding") for w in (o.get("why_chain") or [])[:2]],
            }
        else:
            schema = VENTURE_KIT_SCHEMA
            payload = {
                "task": "venture_launch_kit",
                "niche": o.get("title"), "kind": (o.get("route") or {}).get("kind"),
                "geo": (o.get("route") or {}).get("geo"),
                "monthly_net_estimate_usd": e.get("total_net_usd"),
                "startup_cost_usd": e.get("capital_usd"),
                "demand_evidence": [w.get("finding") for w in (o.get("why_chain") or [])[:3]],
                "window_days": o.get("window_days"),
            }
        try:
            import anthropic
            client = self._get_client()
            response = client.messages.create(
                model=self.cfg.ai_model,
                max_tokens=4096,
                system=KIT_SYSTEM,
                output_config={"format": {"type": "json_schema", "schema": schema}},
                messages=[{"role": "user",
                           "content": json.dumps(payload, ensure_ascii=False, default=str)}],
            )
            if response.stop_reason == "refusal":
                return None, "the model declined to write this kit"
            text = next((b.text for b in response.content if b.type == "text"), "")
            kit = json.loads(text)
            kit["_kind"] = "flip" if is_flip else "venture"
            return kit, ""
        except ImportError:
            return None, "anthropic SDK not installed (pip install anthropic)"
        except anthropic.APIStatusError as e2:
            return None, f"Claude API error {e2.status_code}: {getattr(e2, 'message', e2)}"[:200]
        except anthropic.APIConnectionError as e2:
            return None, f"Claude API unreachable: {e2}"[:200]
        except Exception as e2:  # noqa: BLE001
            return None, f"kit generation failed: {e2}"[:200]

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

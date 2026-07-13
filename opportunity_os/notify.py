"""Push notifications — the briefing that finds you.

Telegram first (free, works great in Thailand, no LINE Notify dependency —
LINE Notify was discontinued in March 2025). Create a bot with @BotFather,
message it once, then set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.
"""

from __future__ import annotations

import httpx

from .config import Config


def briefing_text(b: dict, max_items: int = 6) -> str:
    lines = [f"◆ OPPORTUNITY OS — {b['generated_at'][:16]} ({b['timezone']})", "", b["headline"]]
    lines += b.get("notes", [])
    lines.append("")
    for i, t in enumerate(b["top"][:max_items], 1):
        per = "/mo" if t["type"] not in ("product_arbitrage",) else ""
        lines.append(f"{i}. [{t['score']:.0f}] {t['title']}")
        lines.append(f"    est ${t['net_usd']:,.0f}{per} · conf {t['confidence']:.0%} · "
                     f"window ~{t['window_days']:.0f}d")
    if len(b["top"]) > max_items:
        lines.append(f"… plus {len(b['top']) - max_items} more on the dashboard.")
    return "\n".join(lines)


def send_telegram(cfg: Config, text: str) -> tuple[bool, str]:
    if not (cfg.telegram_bot_token and cfg.telegram_chat_id):
        return False, "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set"
    try:
        r = httpx.post(
            f"https://api.telegram.org/bot{cfg.telegram_bot_token}/sendMessage",
            json={"chat_id": cfg.telegram_chat_id, "text": text[:4000]},
            timeout=cfg.http_timeout)
        r.raise_for_status()
        return True, "sent"
    except Exception as e:  # noqa: BLE001
        return False, f"telegram send failed: {e}"

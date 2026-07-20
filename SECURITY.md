# Security

The dashboard stores real API credentials and can spend paid credits, so it is
treated as an admin surface, not a public page. This document describes the
controls, how to set them up, and — importantly — **how to rotate every
credential that was ever entered over plain HTTP.**

## The layers (defence in depth)

| Layer | What it protects | Where |
|---|---|---|
| 1. Loopback bind | The app itself listens on `127.0.0.1` only — nothing reaches it except the proxy or an SSH tunnel | `deploy/opportunity-os.service` |
| 2. HTTPS + login proxy | Transport encryption + a username/password on the whole dashboard | `deploy/setup_https_dashboard.sh` (Caddy; Let's Encrypt with a domain, self-signed `tls internal` on a bare IP — never plain HTTP) |
| 3. App admin token | The key-management endpoints (`/api/settings*`) and the cycle trigger (`/api/cycle`) require `OOS_DASHBOARD_TOKEN`, sent as `X-OOS-Token` or `Authorization: Bearer`. Without a token configured these endpoints work from localhost only — a public deploy is **closed by default**, and a reverse-proxied request does not count as local | `opportunity_os/security.py` (`AdminGuard`) |
| 4. CSRF protection | Admin calls require the token in a custom header (browsers never attach custom headers cross-site) plus a same-origin check on `Origin`/`Referer` | `AdminGuard` |
| 5. Rate limiting | Per-IP sliding window (default 20/min, `OOS_ADMIN_RATE`) on admin endpoints — no brute-forcing the token, no credit-burning loops | `RateLimiter` |
| 6. At-rest encryption | Stored credentials are encrypted (PBKDF2-HMAC-SHA256 key derivation, HMAC-SHA256-CTR stream cipher, encrypt-then-MAC) before touching SQLite. Master key: `OOS_SECRET_KEY`, or an auto-generated `data/.secret_key` (mode 0600). Legacy plaintext values are migrated to encrypted on first read | `SecretBox` |
| 7. Response hygiene | No endpoint ever returns a raw key — only masked previews (`abc…wxyz (40 chars)`). Errors and logs never include values | `settings.py`, pinned by `tests/test_security.py` |

Every row above is enforced by a test in `tests/test_security.py` (12 tests):
unauthenticated view/modify blocked, wrong token rejected, proxy-forwarded
localhost fails closed, cross-origin refused, secrets absent from every
response body, on-disk ciphertext only, tamper/wrong-key decrypts to nothing,
rate limiter trips.

## Setup (droplet)

```bash
cd /opt/opportunity-os
bash deploy/setup_https_dashboard.sh     # HTTPS + login + generates OOS_DASHBOARD_TOKEN
```

The script prints your admin token once. The dashboard prompts for it the
first time you open the ⚙ Keys tab or press "Run research cycle"; it is kept
in your browser's localStorage, never embedded in the page.

To see it again: `grep OOS_DASHBOARD_TOKEN /opt/opportunity-os/.env`

Optional hardening:

```bash
# use your own master key for at-rest encryption instead of data/.secret_key
echo "OOS_SECRET_KEY=$(head -c 32 /dev/urandom | base64)" >> /opt/opportunity-os/.env
systemctl restart opportunity-os
```

Note: changing `OOS_SECRET_KEY` after keys are saved makes the stored values
undecryptable (by design). Re-paste your keys in ⚙ Keys afterwards.

## ⚠ Rotating credentials that were entered over plain HTTP

If you ever pasted keys into the dashboard while it was served over
`http://<ip>` (before this hardening), treat every one of them as exposed —
anyone on the network path could have read them. Rotate all of them:

| Credential | Where to rotate |
|---|---|
| eBay Client ID + Secret | developer.ebay.com → Application Keys → your keyset → regenerate the Cert ID (Secret). If needed, create a new keyset and delete the old one |
| Reddit Client ID + Secret | reddit.com/prefs/apps → your script app → edit → regenerate secret (or delete the app and create a new one) |
| Serper API key | serper.dev → dashboard → API key → regenerate |
| Anthropic API key | console.anthropic.com → API keys → disable the old key, create a new one |
| ScrapingDog API key | scrapingdog.com dashboard → regenerate key |
| Telegram bot token | @BotFather → `/mybots` → your bot → API Token → Revoke current token |
| Dashboard login (Caddy) | re-run `bash deploy/setup_https_dashboard.sh` and choose a new password |

After rotating, paste the new values into ⚙ Keys (now over HTTPS, auth-gated,
encrypted at rest). The old values stored on disk are overwritten on save.

## Threat model — what this does and does not cover

Covered: anonymous network attackers reading/writing keys; cross-site request
forgery; token brute-force; casual disk exposure of the SQLite file (backup
copied off the box, for example); credential leakage through API responses,
error messages, or logs.

Not covered (out of scope for a single-user tool): an attacker with root on
the droplet (they can read process memory and `data/.secret_key` alike);
compromise of your own browser; the upstream services themselves. The
self-signed certificate on bare-IP deploys encrypts transport but cannot prove
server identity — add any domain (even a free one) for a trusted certificate.

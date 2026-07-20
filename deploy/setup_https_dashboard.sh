#!/usr/bin/env bash
# Puts a username/password prompt in front of the Opportunity OS dashboard so
# you can open it straight from your phone's browser — no SSH tunnel needed.
# Run as root, after opportunity-os.service is already installed and running.
#
#   bash deploy/setup_https_dashboard.sh
#
# Ask it for a username and a password, installs Caddy, writes the config,
# and starts it. Afterwards, open http://<your-droplet-ip>/ in any browser —
# it will ask for that username/password, then show the dashboard.

set -euo pipefail
APP_DIR="${APP_DIR:-/opt/opportunity-os}"

if command -v caddy >/dev/null 2>&1; then
  echo "▸ Caddy already installed — skipping (re-run this script anytime to change the login)"
else
  echo "▸ installing Caddy (official repo)"
  apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https curl gnupg > /dev/null
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq
  apt-get install -y -qq caddy > /dev/null
fi

read -rp "Choose a username for the dashboard login: " OOS_USER
read -rsp "Choose a password for the dashboard login: " OOS_PASS
echo ""
HASH=$(caddy hash-password --plaintext "$OOS_PASS")

read -rp "Domain name pointed at this droplet (leave empty to use the bare IP): " DOMAIN

if [ -n "$DOMAIN" ]; then
  # Real domain → Caddy fetches a trusted Let's Encrypt certificate.
  cat > /etc/caddy/Caddyfile <<EOF
${DOMAIN} {
	basic_auth {
		${OOS_USER} ${HASH}
	}
	reverse_proxy localhost:8000
}
EOF
else
  # Bare IP → HTTPS with Caddy's internal (self-signed) certificate. The
  # browser shows a one-time warning, but credentials and API keys are
  # encrypted in transit — never plain HTTP on a public interface.
  cat > /etc/caddy/Caddyfile <<'EOF'
https:// {
	tls internal
	basic_auth {
		__USER__ __HASH__
	}
	reverse_proxy localhost:8000
}
http:// {
	redir https://{host}{uri} permanent
}
EOF
  sed -i "s|__USER__|${OOS_USER}|; s|__HASH__|${HASH}|" /etc/caddy/Caddyfile
fi

systemctl enable --now caddy
systemctl reload caddy

# App-level admin token: gates the key-management + cycle endpoints inside the
# app (defence in depth behind the Caddy login). Generate once, keep in .env.
ENV_FILE="${APP_DIR}/.env"
touch "$ENV_FILE"
if ! grep -q '^OOS_DASHBOARD_TOKEN=' "$ENV_FILE"; then
  TOK=$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 32)
  echo "OOS_DASHBOARD_TOKEN=${TOK}" >> "$ENV_FILE"
  systemctl restart opportunity-os || true
  echo ""
  echo "✓ Generated your admin token (needed for the ⚙ Keys tab — paste it when the dashboard asks):"
  echo "    ${TOK}"
  echo "  Stored in ${ENV_FILE}. Show it again anytime: grep OOS_DASHBOARD_TOKEN ${ENV_FILE}"
fi

echo ""
echo "✓ Dashboard is now behind a login, over HTTPS."
if [ -n "$DOMAIN" ]; then
  echo "  Open: https://${DOMAIN}/   (trusted certificate — first load may take ~30s)"
else
  IP=$(curl -s ifconfig.me || echo "<your-droplet-ip>")
  echo "  Open: https://${IP}/"
  echo "  Your browser will warn once about a self-signed certificate — choose Advanced → Proceed."
  echo "  (Add a free domain later for a warning-free certificate.)"
fi
echo "  Login: ${OOS_USER} / (the password you typed)"

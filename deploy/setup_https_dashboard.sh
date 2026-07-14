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

echo "▸ installing Caddy (official repo)"
apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https curl gnupg > /dev/null
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  > /etc/apt/sources.list.d/caddy-stable.list
apt-get update -qq
apt-get install -y -qq caddy > /dev/null

read -rp "Choose a username for the dashboard login: " OOS_USER
read -rsp "Choose a password for the dashboard login: " OOS_PASS
echo ""
HASH=$(caddy hash-password --plaintext "$OOS_PASS")

DOMAIN_OR_PORT=":80"
read -rp "Domain name pointed at this droplet (leave empty to use the bare IP over HTTP): " DOMAIN
[ -n "$DOMAIN" ] && DOMAIN_OR_PORT="$DOMAIN"

cat > /etc/caddy/Caddyfile <<EOF
${DOMAIN_OR_PORT} {
	basic_auth {
		${OOS_USER} ${HASH}
	}
	reverse_proxy localhost:8000
}
EOF

systemctl enable --now caddy
systemctl reload caddy

echo ""
echo "✓ Dashboard is now behind a login."
if [ -n "$DOMAIN" ]; then
  echo "  Open: https://${DOMAIN}/   (Caddy fetches HTTPS automatically — first load may take ~30s)"
else
  IP=$(curl -s ifconfig.me || echo "<your-droplet-ip>")
  echo "  Open: http://${IP}/   (plain HTTP — fine for personal use, but add a domain later for HTTPS)"
fi
echo "  Login: ${OOS_USER} / (the password you typed)"

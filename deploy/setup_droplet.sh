#!/usr/bin/env bash
# Opportunity OS — DigitalOcean droplet setup (Ubuntu 22.04/24.04, run as root).
#
#   git clone <your repo> /opt/opportunity-os
#   cd /opt/opportunity-os && bash deploy/setup_droplet.sh
#
# Idempotent: safe to re-run after editing .env or watchlist.json.

set -euo pipefail
APP_DIR="${APP_DIR:-/opt/opportunity-os}"

if [ ! -f "$APP_DIR/run.py" ]; then
  echo "✗ expected the repo at $APP_DIR (git clone it there first)"; exit 1
fi
cd "$APP_DIR"

echo "▸ packages"
apt-get update -qq && apt-get install -y -qq python3-venv python3-pip git > /dev/null

echo "▸ service user"
id -u oos &>/dev/null || useradd -r -m -d /var/lib/oos -s /usr/sbin/nologin oos

echo "▸ python venv + dependencies"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt

echo "▸ config files"
[ -f watchlist.json ] || { cp watchlist.example.json watchlist.json; echo "  created watchlist.json — edit it!"; }
if [ ! -f .env ]; then
  cp .env.example .env
  echo ""
  echo "  ✗ created $APP_DIR/.env — fill in your keys (nano $APP_DIR/.env), then re-run this script."
  exit 1
fi

mkdir -p data
chown -R oos:oos "$APP_DIR"
chmod 600 .env

echo "▸ live-check (with your .env)"
sudo -u oos bash -c "set -a; source $APP_DIR/.env; set +a; $APP_DIR/.venv/bin/python $APP_DIR/run.py live-check" || true

echo "▸ systemd units"
cp deploy/opportunity-os.service deploy/opportunity-os-brief.service deploy/opportunity-os-brief.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now opportunity-os.service
systemctl enable --now opportunity-os-brief.timer

echo ""
echo "✓ Opportunity OS is running (live mode, cycle every 30 min)."
echo "  status   : systemctl status opportunity-os"
echo "  logs     : journalctl -u opportunity-os -f"
echo "  dashboard: ssh -L 8000:localhost:8000 <you>@<droplet-ip>   →  http://localhost:8000"
echo "  briefing : fires daily 07:00 Asia/Bangkok (00:00 UTC) → Telegram"

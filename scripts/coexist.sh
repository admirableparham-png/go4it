#!/usr/bin/env bash
# go4it CO-EXIST deploy — run go4it beside an existing Caddy reverse proxy (e.g. tradesitter) on the SAME
# server, routing a subdomain to go4it WITHOUT touching the other site.
#
#   bash coexist.sh go4it.your-domain.com
#
# Safety: go4it runs as its OWN isolated containers (can't affect the other app). The only shared edit is
# ONE appended block in the Caddyfile, done with: backup -> validate -> graceful `caddy reload`. If the new
# config is invalid, Caddy keeps the OLD one running, so the existing site cannot go down. Idempotent.
set -euo pipefail

DOMAIN="${1:-}"
[ -n "$DOMAIN" ] || { echo "usage: bash coexist.sh <domain>   e.g.  bash coexist.sh go4it.tradesitter.vip"; exit 1; }

REPO="https://github.com/admirableparham-png/go4it.git"
DIR="/opt/go4it"
NET="go4it-net"
CFG="/etc/caddy/Caddyfile"

echo "==> Locating the reverse proxy (container publishing :443)…"
CADDY=$(docker ps --filter "publish=443" --format '{{.ID}}' | head -1)
[ -n "$CADDY" ] || { echo "!! No container publishes :443 — is your Caddy running?"; exit 1; }
echo "   proxy container: $CADDY"

HOSTCF=$(docker inspect "$CADDY" --format "{{range .Mounts}}{{if eq .Destination \"$CFG\"}}{{.Source}}{{end}}{{end}}")
[ -n "$HOSTCF" ] && [ -f "$HOSTCF" ] || { echo "!! Could not find the Caddyfile on the host (expected mount at $CFG)."; exit 1; }
echo "   Caddyfile on host: $HOSTCF"

if [ -f "$DIR/docker-compose.coexist.yml" ]; then
  echo "==> Using go4it code already in $DIR (transferred/cloned)."
elif [ -d "$DIR/.git" ]; then
  echo "==> Updating go4it in $DIR…"; git -C "$DIR" pull --ff-only
else
  echo "==> Cloning go4it into $DIR…"; git clone "$REPO" "$DIR"
fi
cd "$DIR"

if [ ! -f .env ]; then
  echo "==> Writing .env (generating secrets)…"
  {
    echo "SECRET_KEY=$(openssl rand -base64 48 | tr -d '\n')"
    echo "GO4IT_INGEST_KEY=$(openssl rand -hex 24)"
    echo "BASE_URL=https://$DOMAIN"
    echo "DOMAIN=$DOMAIN"
    echo "CORS_ORIGINS=https://$DOMAIN"
    echo "WEB_CONCURRENCY=2"
  } > .env
else
  echo "==> .env already present — leaving it untouched."
fi

echo "==> Building + starting go4it (app + worker, isolated)…"
docker compose -f docker-compose.coexist.yml up -d --build

echo "==> Attaching the proxy to go4it's network so it can reach go4it-app…"
docker network connect "$NET" "$CADDY" 2>/dev/null && echo "   connected" || echo "   already connected"

echo "==> Adding the go4it route to Caddy (backup -> validate -> graceful reload)…"
BK="${HOSTCF}.bak.$(date +%s)"
cp "$HOSTCF" "$BK"
echo "   backup saved: $BK"

if grep -qF "$DOMAIN {" "$HOSTCF"; then
  echo "   route for $DOMAIN already present — leaving config as-is."
else
  printf '\n%s {\n\tencode zstd gzip\n\treverse_proxy go4it-app:8400\n}\n' "$DOMAIN" >> "$HOSTCF"
  echo "   appended $DOMAIN -> go4it-app:8400"
fi

echo "==> Validating the new Caddy config…"
if docker exec "$CADDY" caddy validate --config "$CFG" --adapter caddyfile >/dev/null 2>&1; then
  docker exec "$CADDY" caddy reload --config "$CFG" --adapter caddyfile
  echo ""
  echo "======================================================================"
  echo " DONE — go4it is running behind Caddy."
  echo ""
  echo " Finish it (2 steps):"
  echo "  1) DNS: add an A record  $DOMAIN  ->  167.233.138.214"
  echo "     in the same place you manage this server's other domain."
  echo "     (Match its proxy setting — if the other one is Cloudflare DNS-only/grey, use grey here too,"
  echo "      so Caddy can issue the HTTPS cert.)"
  echo ""
  echo "  2) Create your admin login (pick your own email + password):"
  echo "     docker exec go4it-app python scripts/create_admin.py you@example.com 'YourStrongPassword'"
  echo ""
  echo " Then open:  https://$DOMAIN"
  echo "======================================================================"
else
  echo "!! New Caddy config is INVALID — restoring backup. The existing site is UNTOUCHED."
  cp "$BK" "$HOSTCF"
  exit 1
fi

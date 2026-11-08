#!/usr/bin/env bash
# AVAND ERP — HTTPS with Let's Encrypt + automatic renewal (nginx on the host).   usage: avand-ssl
# Safe to re-run. Settings from /opt/avand/.env: AVAND_DOMAIN, AVAND_HTTPS_PORT (70), AVAND_EMAIL, APP_PORT (8000).
# Needs public port 80 -> this host (Let's Encrypt HTTP check) and AVAND_HTTPS_PORT -> this host.
set -euo pipefail
APP="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
[ -f "$APP/.env" ] || APP=/opt/avand
set -a; . "$APP/.env"; set +a
D="${AVAND_DOMAIN:-jti.pigroup.ir}"; PORT="${AVAND_HTTPS_PORT:-70}"; UP="${APP_PORT:-8000}"; MAIL="${AVAND_EMAIL:-}"
V6=""; [ -s /proc/net/if_inet6 ] && V6=1   # IPv6 listeners only when the host has IPv6
WEB=/var/www/certbot; SITE=/etc/nginx/sites-available/avand; LE="/etc/letsencrypt/live/$D"
log(){ echo -e "\e[1;34m==>\e[0m $*"; }
die(){ echo -e "\e[1;31mERROR:\e[0m $*"; exit 1; }

command -v nginx >/dev/null && command -v certbot >/dev/null || { log "installing nginx + certbot"; apt-get update -qq; apt-get install -y -qq nginx certbot >/dev/null; }
mkdir -p "$WEB/.well-known/acme-challenge" /etc/nginx/sites-enabled

# 1) retire old site files for this domain / port (backup kept)
BK="/root/nginx-backup-$(date +%Y%m%d-%H%M%S)"
for f in /etc/nginx/sites-enabled/* /etc/nginx/conf.d/*.conf; do
  [ -e "$f" ] || continue
  [ "$(readlink -f "$f")" = "$SITE" ] && continue
  if grep -Eqs "server_name[^;]*\b${D//./\\.}\b|listen[^;]*\b${PORT}\b" "$f"; then
    mkdir -p "$BK"; cp -aL "$f" "$BK/"; rm -f "$f"; log "old nginx config moved to $BK/$(basename "$f")"
  fi
done

http_block(){ cat << EOF
server {
    listen 80;${V6:+
    listen [::]:80;}
    server_name $D;
    location ^~ /.well-known/acme-challenge/ { root $WEB; default_type text/plain; }
    location / { return 301 https://$D:$PORT\$request_uri; }
}
EOF
}

# http2: nginx >= 1.25.1 uses "http2 on;", older (Ubuntu 24.04 = 1.24) uses "listen ... ssl http2"
NV=$(nginx -v 2>&1 | grep -oP '[0-9]+\.[0-9]+\.[0-9]+' | head -1)
if [ "$(printf '%s\n1.25.1\n' "$NV" | sort -V | head -1)" = "1.25.1" ]; then H2L=""; H2="
      http2 on;"; else H2L=" http2"; H2=""; fi
full_site(){
  { http_block; cat << EOF

  server {
      listen $PORT ssl$H2L;${V6:+
      listen [::]:$PORT ssl$H2L;}$H2
      server_name $D;

      ssl_certificate     $LE/fullchain.pem;
      ssl_certificate_key $LE/privkey.pem;
      ssl_protocols TLSv1.2 TLSv1.3;
      ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305;
      ssl_prefer_server_ciphers off;
      ssl_session_cache shared:AVAND:10m;
      ssl_session_timeout 1d;
      ssl_session_tickets off;
      error_page 497 =301 https://$D:$PORT\$request_uri;   # plain http sent to the https port

      add_header Strict-Transport-Security "max-age=31536000" always;
      add_header X-Content-Type-Options nosniff always;
      add_header X-Frame-Options SAMEORIGIN always;
      add_header Referrer-Policy strict-origin-when-cross-origin always;

      client_max_body_size 25m;
      gzip on; gzip_types text/css application/javascript application/json image/svg+xml;

      location / {
          proxy_pass http://127.0.0.1:$UP;
          proxy_set_header Host \$http_host;
          proxy_set_header X-Real-IP \$remote_addr;
          proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
          proxy_set_header X-Forwarded-Proto https;
          proxy_set_header X-Forwarded-Host \$http_host;
          proxy_read_timeout 300s;
      }
  }
EOF
  } > "$SITE"
  nginx -t -q || die "nginx config test failed (see above)."
}
apply(){ nginx -t -q || die "nginx config test failed (see above)."; systemctl enable -q nginx; systemctl reload nginx 2>/dev/null || systemctl restart nginx; }

# 2) keep the site up: full config if a certificate exists, otherwise port 80 only (enough for the certificate check)
ln -sf "$SITE" /etc/nginx/sites-enabled/avand
if [ -f "$LE/fullchain.pem" ]; then full_site; else http_block > "$SITE"; fi
apply

# 3) certificate: obtain, or switch an existing one to the webroot method (renews while nginx runs)
ARGS=(--webroot -w "$WEB" -d "$D" --non-interactive --agree-tos)
[ -n "$MAIL" ] && ARGS+=(-m "$MAIL") || ARGS+=(--register-unsafely-without-email)
REN="/etc/letsencrypt/renewal/$D.conf"
if [ ! -f "$LE/fullchain.pem" ]; then
  log "requesting certificate for $D"
  certbot certonly "${ARGS[@]}" || die "Let's Encrypt could not reach http://$D/.well-known/... — check port 80 forwarding to this host and DNS of $D."
elif ! grep -q "authenticator = webroot" "$REN" 2>/dev/null || ! openssl x509 -checkend $((30*86400)) -noout -in "$LE/cert.pem" >/dev/null; then
  log "renewing certificate with webroot method"
  certbot certonly "${ARGS[@]}" --cert-name "$D" --force-renewal || die "renewal failed — check port 80 forwarding to this host."
else
  log "certificate valid: $(openssl x509 -enddate -noout -in "$LE/cert.pem")"
fi

# 4) full site: http -> https, TLS 1.2/1.3, full chain, security headers, proxy to the app
full_site; apply

# 5) automatic renewal: certbot timer + reload nginx after each renewal
mkdir -p /etc/letsencrypt/renewal-hooks/deploy
printf '#!/bin/sh\nsystemctl reload nginx\n' > /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
chmod 755 /etc/letsencrypt/renewal-hooks/deploy/reload-nginx.sh
if systemctl list-unit-files certbot.timer >/dev/null 2>&1; then
  systemctl enable -q --now certbot.timer
else
  echo "17 3,15 * * * root certbot renew -q" > /etc/cron.d/avand-certbot
fi
log "renewal dry-run"; certbot renew --dry-run -q --cert-name "$D" && log "auto-renew OK" || die "renewal dry-run failed — check port 80 forwarding."

# 6) check
sleep 1
curl -fsS -o /dev/null --resolve "$D:$PORT:127.0.0.1" "https://$D:$PORT/login" && log "OK — https://$D:$PORT ($(openssl x509 -enddate -noout -in "$LE/cert.pem"))" \
  || die "https check failed (app down? run: docker compose -f $APP/docker-compose.yml ps)"

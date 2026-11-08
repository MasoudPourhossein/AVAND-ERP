#!/usr/bin/env bash
# Update AVAND ERP from GitHub and restart:  avand-update
set -euo pipefail
cd /opt/avand
echo "==> git pull"; git pull --ff-only
install -m 755 deploy/update.sh /usr/local/bin/avand-update
install -m 755 deploy/ssl.sh /usr/local/bin/avand-ssl
docker compose up -d --build
for i in $(seq 1 40); do
  if curl -fs -o /dev/null "http://127.0.0.1:$(grep -oP '^APP_PORT=\K\d+' .env || echo 8000)/health"; then
    echo "==> OK: $(git log -1 --format='%h %s')"; docker image prune -f >/dev/null; exit 0; fi
  sleep 2
done
docker compose logs --tail 60 app; exit 1

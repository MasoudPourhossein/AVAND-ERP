#!/usr/bin/env bash
# AVAND ERP — first install (Ubuntu 24.04, run as root from the cloned repo):
#   bash /opt/avand/deploy/install.sh
# Safe to re-run. Imports the old app (/var/lib/anbar/anbar.db) once, then retires its container.
set -euo pipefail
APP="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP"
OLD_DB=/var/lib/anbar/anbar.db
log(){ echo -e "\e[1;34m==>\e[0m $*"; }

command -v docker >/dev/null || { apt-get update -qq; apt-get install -y -qq docker.io docker-compose-v2; }
apt-get install -y -qq curl cron >/dev/null 2>&1 || true
if [ ! -f /etc/docker/daemon.json ]; then
  mkdir -p /etc/docker
  echo '{ "registry-mirrors": ["https://docker.arvancloud.ir", "https://docker.iranserver.com"] }' > /etc/docker/daemon.json
  systemctl restart docker
fi
systemctl enable -q --now docker cron

if [ ! -f .env ]; then
  log "creating .env"
  sed "s/^PG_PASSWORD=.*/PG_PASSWORD=$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9')/" .env.example > .env
  chmod 600 .env
fi
set -a; . ./.env; set +a
H="${AVAND_HOME:-/var/lib/avand}"
mkdir -p "$H/pg" "$H/app/backups" "$H/backups"
chown -R 10001:10001 "$H/app"
chmod 700 "$APP/.git" 2>/dev/null || true

log "building image (pip mirror: ${PIP_INDEX_URL:-pypi.org})"
docker compose build
docker compose up -d db
for i in $(seq 1 60); do docker compose exec -T db pg_isready -U avand -d avand >/dev/null 2>&1 && break; sleep 2; done

if [ -f "$OLD_DB" ] && [ ! -f "$H/app/.migrated" ]; then
  log "importing old app data from $OLD_DB"
  if docker ps -a --format '{{.Names}}' | grep -qx anbar; then docker stop anbar >/dev/null && docker rm anbar >/dev/null; fi
  systemctl disable --now anbar >/dev/null 2>&1 || true
  cp -f /var/lib/anbar/logo.png "$H/app/logo.png" 2>/dev/null && chown 10001:10001 "$H/app/logo.png" || true
  cp -f "$OLD_DB" "$H/app/anbar-sqlite-backup.db"
  docker compose run --rm --no-deps -v /var/lib/anbar:/old:ro app python migrate_sqlite.py /old/anbar.db
  touch "$H/app/.migrated"
fi

docker compose up -d
install -m 755 "$APP/deploy/update.sh" /usr/local/bin/avand-update
cat > /etc/cron.d/avand-backup << CRON
# daily 02:30 PostgreSQL backup, keep 30 days
30 2 * * * root docker exec avand-db pg_dump -U avand -Fc -f /backups/auto-\$(date +\%Y\%m\%d).dump avand && find $H/backups -name 'auto-*.dump' -mtime +30 -delete
CRON
rm -f /etc/cron.d/anbar-backup

for i in $(seq 1 40); do
  if curl -fs -o /dev/null "http://127.0.0.1:${APP_PORT:-8000}/health"; then
    log "OK — AVAND ERP is up (nginx: https://<domain>:70). Update later with: avand-update"; exit 0; fi
  sleep 2
done
docker compose logs --tail 60 app; exit 1

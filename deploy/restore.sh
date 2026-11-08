#!/usr/bin/env bash
# Restore AVAND ERP database from a pg_dump (-Fc) file, from the shell (works even if the app is down).
#   avand-restore /path/to/file.dump        (list backups: avand-restore)
set -euo pipefail
cd /opt/avand
set -a; . ./.env; set +a
H="${AVAND_HOME:-/var/lib/avand}"
if [ $# -lt 1 ]; then
  echo "backups:"; ls -1t "$H/backups"/*.dump "$H/app/backups"/*.dump 2>/dev/null | head -40; echo; echo "usage: avand-restore FILE.dump"; exit 1
fi
F="$(readlink -f "$1")"; [ -f "$F" ] || { echo "file not found: $1"; exit 1; }
docker compose up -d db >/dev/null
for i in $(seq 1 30); do docker compose exec -T db pg_isready -U avand -d avand >/dev/null 2>&1 && break; sleep 2; done
docker cp "$F" avand-db:/tmp/restore.dump
docker exec avand-db pg_restore -l /tmp/restore.dump | grep -q "TABLE DATA" || { echo "not a valid pg_dump -Fc file"; exit 1; }
read -r -p "ALL current data will be replaced by $(basename "$F"). Type yes: " a; [ "$a" = "yes" ] || exit 1
SAFE="pre-restore-$(date +%Y%m%d-%H%M%S).dump"
echo "==> safety backup: $H/backups/$SAFE"
docker exec avand-db pg_dump -U avand -Fc -f "/backups/$SAFE" avand
docker compose stop app >/dev/null
load(){
  docker exec avand-db psql -q -U avand -d avand -c "SET client_min_messages=warning; DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;" >/dev/null
  docker exec avand-db sh -c "pg_restore -l $1 | grep -vE '(SCHEMA - public|COMMENT - SCHEMA public|ACL - SCHEMA public) ' > /tmp/restore.toc"
  docker exec avand-db pg_restore --no-owner --no-privileges --exit-on-error -L /tmp/restore.toc -U avand -d avand "$1"
}
if load /tmp/restore.dump; then echo "==> restored $(basename "$F")"
else echo "!! restore failed, loading safety backup"; load "/backups/$SAFE"; fi
docker exec avand-db rm -f /tmp/restore.dump
docker compose up -d app
for i in $(seq 1 40); do curl -fs -o /dev/null "http://127.0.0.1:${APP_PORT:-8000}/health" && { echo "==> OK (app upgraded the schema if the backup was older)"; exit 0; }; sleep 2; done
docker compose logs --tail 40 app; exit 1

#!/usr/bin/env bash
set -euo pipefail
APP_DIR="${APP_DIR:-/opt/failover-monitor}"
COMPOSE_FILES="-f docker-compose.yml -f docker-compose.prod.yml"

echo "==> деплой образа: ${MONITOR_IMAGE:?нужно задать MONITOR_IMAGE}"

cd "$APP_DIR"
export MONITOR_IMAGE
touch .env
grep -v '^MONITOR_IMAGE=' .env > .env.tmp 2>/dev/null || true
echo "MONITOR_IMAGE=$MONITOR_IMAGE" >> .env.tmp
mv .env.tmp .env

echo "==> затягиваю свежие образы..."
docker compose $COMPOSE_FILES pull

echo "==> запускаю/обновляю контейнеры..."
docker compose $COMPOSE_FILES up -d --remove-orphans

echo "==> чищу неиспользуемые образы на сервере..."
docker image prune -f || true

echo "==> проверяю healthcheck монитора..."
for i in $(seq 1 15); do
  status="$(docker inspect --format '{{.State.Health.Status}}' monitor 2>/dev/null || echo "none")"
  if [ "$status" = "healthy" ]; then
    echo "OK: контейнер monitor в состоянии healthy!"
    break
  fi
  echo "жду готовности monitor (статус: $status)... ($i)"
  sleep 2
  if [ "$i" = "15" ]; then
    echo "ERROR: monitor не стал healthy за отведенное время!!" >&2
    docker compose $COMPOSE_FILES logs monitor || true
    exit 1
  fi
done

echo "==> деплой завершен успешно!!!"
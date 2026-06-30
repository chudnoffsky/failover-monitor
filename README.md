# Failover Monitor: мониторинг приложений с выбором мастера

Сервис на Python мониторит несколько приложений по URL с токеном,
считает неудачные проверки и при падении текущего лидера
автоматически выбирает нового мастера, запоминая его между перезапусками.
Упаковано в Docker, разворачивается через `docker compose`, есть CI/CD
и автоматическая доставка на сервер.

## Состав

| Компонент      | Назначение                                                        |
|----------------|-------------------------------------------------------------------|
| `monitor`      | Приложение мониторинга/failover (Python). Сервер статуса на :8080 |
| `app1/2/3`     | Тестовые приложения: nginx, проверяют токен на `/health`       |
| `cadvisor`     | Сервис мониторинга состояния сервера и контейнеров (UI на :8081)  |

## 1. Как запустить все локально

```bash
# 1. Подготовить переменные окружения
cp .env.example .env

# 2. Поднять весь стек
docker compose up --build -d
# или:  make up

# 3. Посмотреть текущего лидера и состояние узлов
curl -s http://localhost:8080/status | python3 -m json.tool
# или:  make status

# 4. Логи монитора
docker compose logs -f monitor

# 5. Сервис мониторинга (cAdvisor)
open http://localhost:8081
```

### Проверка failover

```bash
make test-failover
```

Команда останавливает текущего лидера `app1`, ждет и показывает, как монитор
выбрал нового мастера. Затем возвращает `app1`. Текущий лидер сохраняется в
именованном томе `monitor-state`, поэтому переживает перезапуск контейнера.

## 2. Переменные окружения

| Переменная        | По умолчанию                                  | Описание                                   |
|-------------------|-----------------------------------------------|--------------------------------------------|
| `CHECK_ADDRESSES` | `http://app1:80,http://app2:80,http://app3:80`| список адресов для проверки                |
| `CHECK_URL`       | `/health`                                     | путь к адресу; пусто = адрес считается целым URL |
| `MONITOR_TOKEN`   | `secret-token`                                | токен (заголовок),                         |
| `TOKEN_HEADER`    | `X-Auth-Token`                                | имя заголовка с токеном                    |
| `CHECK_INTERVAL`  | `5`                                           | частота проверки, сек                      |
| `FAIL_THRESHOLD`  | `3`                                           | кол-во неудач подряд до failover           |
| `HTTP_TIMEOUT`    | `3`                                           | таймаут запроса, сек                       |
| `STATE_FILE`      | `/data/state.json`                            | файл хранения текущего лидера              |
| `STATUS_PORT`     | `8080`                                        | порт сервера статуса монитора              |
| `LOG_LEVEL`       | `INFO`                                        | уровень логирования                        |
| `CADVISOR_PORT`   | `8081`                                        | порт UI cAdvisor                           |
| `MONITOR_IMAGE`   | `failover-monitor:local`                      | образ монитора (на проде: тег из реестра)  |

Эндпоинты монитора: `GET /healthz` для healthcheck и `GET /status`
JSON с лидером и состоянием узлов.

## 3. CI/CD (GitLab)

Файл `.gitlab-ci.yml`. Запуск:

- `development`: Continuous Deployment: сборка, далее пуш в реестр, потом авто-деплой
- тег `vX.Y.Z`: Continuous Delivery: сборка, далее пуш, потом деплой вручную (`when: manual`)
  Прод-образ собирается один раз на теге и деплоится без пересборки.
- `feature/*` и прочее, только `lint`, образ не собирается

Стадии: `lint - build - verify - deploy - cleanup`
- `build` — успешная сборка и пуш образа в реестр GitLab (локальный хаб)
- `verify` — поднимает стек через `docker compose` и ждет, пока контейнеры станут `healthy` (nginx и монитор отвечают)
- `cleanup` — `docker system prune` на раннере + очистка тегов в реестре

Нужно задать переменные CI/CD проекта: `DEPLOY_SSH_KEY`, `DEPLOY_HOST`,
`DEPLOY_USER` (development) и `PROD_HOST`, `PROD_USER` (production)

Создать пример feature-ветки:
```bash
git checkout -b feature/example
git push -u origin feature/example   # запустится только lint
```

Выпуск версии на прод:
```bash
git tag v1.0.0 && git push origin v1.0.0
```

## 4. Деплой на сервер (автоматизация)

Скрипт `deploy/deploy.sh` (запускается на вм, по ssh из пайплайна):
1. затягивает свежий образ (`docker compose pull`)
2. запускает/обновляет стек (`docker compose up -d`)
3. обеспечивает перезапуск (политики `restart: always` + systemd-unit)
4. ждет, пока контейнер `monitor` станет `healthy`

Подготовка вм:
```bash
sudo mkdir -p /opt/failover-monitor
# скопировать docker-compose.yml, docker-compose.prod.yml, nginx/, .env
sudo cp deploy/failover-monitor.service /etc/systemd/system/
sudo systemctl enable --now failover-monitor.service
```

## 5. Мониторинг и ресурсы

Сервис `cadvisor` следит за состоянием сервера и контейнеров (CPU, память, сеть)
с веб-интерфейсом на `:8081`. Все сервисы ограничены по ресурсам через
`deploy.resources.limits` (CPU `0.5`, память `256M`), применяется и при
обычном `docker compose up`.

## 6. Схема взаимодействия

```
                         GitLab CI/CD
       development ──► build ─► push(registry) ─► verify ─► auto-deploy
       tag vX.Y.Z  ──► build ─► push(registry) ─► verify ─► manual-deploy
                                    │                          │
                                    ▼                          ▼ (ssh + deploy.sh)
                            [ Container Registry ] ──pull──►  vm (Ubuntu 22.04)
                                                                  │
                                                       docker compose up -d
                                                                  │
        ┌─────────────────────────────────────────────────────────────────┐
        │  appnet (bridge)                                                  │
        │                                                                   │
        │   ┌─────────┐   проверка /health + токен   ┌──────────────────┐   │
        │   │ monitor │ ───────────────────────────► │ app1 app2 app3   │   │
        │   │ :8080   │ ◄─── статус лидера ────────── │ (nginx-бэкенды)  │   │
        │   └────┬────┘                               └──────────────────┘   │
        │        │ помнит лидера                                            │
        │        ▼                                                          │
        │   [volume: monitor-state]        ┌──────────┐  метрики ресурсов  │
        │                                  │ cadvisor │  хоста/контейнеров │
        │                                  │ :8081    │                    │
        │                                  └──────────┘                    │
        └───────────────────────────────────────────────────────────────────┘
```

Монитор опрашивает бэкенды по `CHECK_URL` с токеном. При `FAIL_THRESHOLD`
неудачах подряд у лидера выбирает нового мастера из живых узлов и сохраняет
его в том `monitor-state`. cAdvisor наблюдает за ресурсами. CI собирает образ
один раз и доставляет его на вм через `deploy.sh`.
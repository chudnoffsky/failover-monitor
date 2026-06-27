# запуск: make up / make down / make logs / make test-failover

COMPOSE := docker compose

.PHONY: help up down build logs status test-failover clean

help: ## показать список команд
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

up: ## собрать и поднять весь стек
	$(COMPOSE) up --build -d

down: ## остановить и удалить контейнеры
	$(COMPOSE) down

build: ## только собрать образ монитора
	$(COMPOSE) build monitor

logs: ## смотреть логи монитора
	$(COMPOSE) logs -f monitor

status: ## показать текущего лидера и статус узлов
	@curl -s http://localhost:8080/status | python3 -m json.tool

test-failover: ## имуляция падение лидера, останавливаем app1, и смотрим failover
	@echo "Останавливаю app1 — монитор должен выбрать нового лидера..."
	$(COMPOSE) stop app1
	@sleep 20
	@echo "Текущий статус:"
	@curl -s http://localhost:8080/status | python3 -m json.tool
	@echo "Возвращаю app1 обратно..."
	$(COMPOSE) start app1

clean: ## полная очистка
	$(COMPOSE) down -v --remove-orphans

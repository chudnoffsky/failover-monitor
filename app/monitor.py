"""
приложение мониторинга и failover: проверяет доступность узлов, хранит текущего лидера
также автоматически переключает лидера при сбое (failover)
и отдает статус через HTTP (/healthz, /status)
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests


def _env_str(name, default):
    value = os.getenv(name, default)
    return value.strip()


def _env_int(name, default):
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        logging.warning("%s='%s' не является числом, беру %s", name, raw, default)
        return default


class Config:
    def __init__(self):
        self.check_url = _env_str("CHECK_URL", "/health")
        self.token = _env_str("CHECK_TOKEN", "")
        self.token_header = _env_str("TOKEN_HEADER", "X-Auth-Token")
        self.interval = _env_int("CHECK_INTERVAL", 5)
        self.fail_threshold = _env_int("FAIL_THRESHOLD", 3)
        self.timeout = _env_int("HTTP_TIMEOUT", 3)
        self.nodes_raw = _env_str("CHECK_ADDRESSES", "")
        self.state_file = _env_str("STATE_FILE", "/data/state.json")
        self.status_host = _env_str("STATUS_HOST", "0.0.0.0")
        self.status_port = _env_int("STATUS_PORT", 8080)
        self.on_failover_hook = _env_str("ON_FAILOVER_HOOK", "")
        self.nodes = [addr.strip() for addr in self.nodes_raw.split(",") if addr.strip()]


class NodeState:
    def __init__(self, address):
        self.address = address
        self.healthy = False
        self.consecutive_fails = 0
        self.last_status = "unknown"
        self.last_checked = None


class AppState:
    def __init__(self):
        self.leader = None
        self.nodes = {}
        self.last_failover = None
        self.lock = threading.Lock()


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


class StateStore:
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def load_leader(self):
        if not os.path.exists(self.path):
            return None
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data.get("leader")
        except (json.JSONDecodeError, OSError) as exc:
            logging.warning("не удалось прочитать файл состояния %s: %s", self.path, exc)
            return None

    def save_leader(self, leader):
        payload = {"leader": leader, "updated_at": _now_iso()}
        tmp_path = f"{self.path}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.path)
        except OSError as exc:
            logging.error("не удалось сохранить файл состояния %s: %s", self.path, exc)


class StatusHandler(BaseHTTPRequestHandler):
    state = None

    def log_message(self, *args, **kwargs):
        return

    def _send_json(self, code, body):
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        state = StatusHandler.state
        if self.path == "/healthz":
            self._send_json(200, {"status": "ok"})
            return
        if self.path == "/status":
            with state.lock:
                body = {
                    "leader": state.leader,
                    "last_failover": state.last_failover,
                    "nodes": {
                        addr: {
                            "healthy": ns.healthy,
                            "consecutive_fails": ns.consecutive_fails,
                            "last_status": ns.last_status,
                            "last_checked": ns.last_checked,
                        }
                        for addr, ns in state.nodes.items()
                    },
                }
            self._send_json(200, body)
            return
        self._send_json(404, {"error": "not found"})


def start_status_server(cfg, state):
    StatusHandler.state = state
    httpd = ThreadingHTTPServer((cfg.status_host, cfg.status_port), StatusHandler)
    thread = threading.Thread(target=httpd.serve_forever, name="status-server", daemon=True)
    thread.start()
    logging.info("HTTP сервер статуса слушает на %s:%s", cfg.status_host, cfg.status_port)
    return httpd


class Monitor:
    def __init__(self, cfg):
        self.cfg = cfg
        self.store = StateStore(cfg.state_file)
        self.state = AppState()
        self._stop = threading.Event()
        for addr in cfg.nodes:
            self.state.nodes[addr] = NodeState(addr)
        saved = self.store.load_leader()
        if saved and saved in self.state.nodes:
            self.state.leader = saved
            logging.info("восстановлен сохраненный лидер: %s", saved)
        else:
            self.state.leader = None

    def request_stop(self, *_args):
        logging.info("Получен сигнал остановки, завершаю работу...")
        self._stop.set()

    def check_node(self, address):
        """
        есть 2 способа задания адресов:
        первый это база + путь: CHECK_ADDRESSES="http://192.168.1.10:8080,..."
        и CHECK_URL="/health" запрашивается http://192.168.1.10:8080/health
         второй это целый URL: в CHECK_ADDRESSES уже полные адреса (http://192.168.1.10:8080/health), а
         а CHECK_URL="" ну и адрес используется как есть
        """
        path = self.cfg.check_url.strip()
        if path:
            url = address.rstrip("/") + "/" + path.lstrip("/")
        else:
            url = address
        headers = {}
        if self.cfg.token:
            headers[self.cfg.token_header] = self.cfg.token
        try:
            resp = requests.get(url, headers=headers, timeout=self.cfg.timeout)
            if 200 <= resp.status_code < 300:
                return True, f"HTTP {resp.status_code}"
            return False, f"HTTP {resp.status_code}"
        except requests.RequestException as exc:
            return False, f"ERROR: ошибка соединения: {exc.__class__.__name__}"

    def elect_leader(self):
        """
        приоритет: порядок в списке CHECK_ADDRESSES, первый живой становится мастером
        """
        for addr in self.cfg.nodes:
            node = self.state.nodes.get(addr)
            if node and node.healthy:
                return addr
        return None

    def run_failover_hook(self, new_leader):
        if not self.cfg.on_failover_hook:
            return
        logging.info("делаю failover хук для нового лидера %s", new_leader)
        env = os.environ.copy()
        env["NEW_LEADER"] = new_leader
        os.system(self.cfg.on_failover_hook)

    def tick(self):
        results = {}
        for addr in list(self.state.nodes.keys()):
            results[addr] = self.check_node(addr)
        with self.state.lock:
            for addr, (ok, detail) in results.items():
                node = self.state.nodes[addr]
                node.last_checked = _now_iso()
                node.last_status = detail
                if ok:
                    if not node.healthy and node.consecutive_fails > 0:
                        logging.info("узел %s восстановился (%s)", addr, detail)
                    node.healthy = True
                    node.consecutive_fails = 0
                else:
                    node.consecutive_fails += 1
                    node.healthy = False
                    logging.warning(
                        "узел %s недоступен (%s), неудач подряд: %d/%d",
                        addr, detail, node.consecutive_fails, self.cfg.fail_threshold,
                    )

            self._maybe_failover()

    def _maybe_failover(self):
        leader = self.state.leader
        if leader is None:
            new_leader = self.elect_leader()
            if new_leader:
                self._set_leader(new_leader, reason="первичный выбор лидера")
            return
        leader_node = self.state.nodes.get(leader)
        if leader_node is None:
            new_leader = self.elect_leader()
            if new_leader:
                self._set_leader(new_leader, reason="прежний лидер удален из списка")
            return
        if leader_node.consecutive_fails >= self.cfg.fail_threshold:
            new_leader = self.elect_leader()
            if new_leader and new_leader != leader:
                self._set_leader(
                    new_leader,
                    reason=f"лидер {leader} упал ({leader_node.consecutive_fails} неудач подряд)",
                )
            elif new_leader is None:
                logging.critical(
                    "лидер %s упал, но ни один узел не доступен, нового мастера нет!!", leader
                )

    def _set_leader(self, new_leader, reason):
        old = self.state.leader
        self.state.leader = new_leader
        self.state.last_failover = _now_iso()
        self.store.save_leader(new_leader)
        logging.info("СМЕНА ЛИДЕРА: %s -> %s. Причина: %s", old, new_leader, reason)
        self.run_failover_hook(new_leader)

    def run(self):
        if not self.cfg.nodes:
            logging.error("список адресов (CHECK_ADDRESSES) пуст, нечего мониторить. выход(")
            sys.exit(1)

        logging.info(
            "старт мониторинга!! узлы: %s | URL: %s | интервал: %ds | порог неудач: %d",
            self.cfg.nodes, self.cfg.check_url, self.cfg.interval, self.cfg.fail_threshold,
        )
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)

        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as exc:
                logging.exception("ERROR: непредвиденная ошибка в цикле проверки: %s", exc)
            self._stop.wait(self.cfg.interval)

        logging.info("мониторинг был остановлен!")


def main():
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    cfg = Config()
    monitor = Monitor(cfg)
    start_status_server(cfg, monitor.state)
    monitor.run()


if __name__ == "__main__":
    main()
